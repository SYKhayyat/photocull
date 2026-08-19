"""Raw files that are not TIFF containers, and how to find the TIFF inside them.

Most raw formats are TIFF wearing a different extension, and
:mod:`photocull.tiffreader` reads those directly. Two are not, and both are
common enough that declining them means declining whole camera systems:

* **Fujifilm RAF** -- an ``FUJIFILMCCD-RAW`` header whose fields point at an
  ordinary JPEG elsewhere in the file. Every X and GFX body writes these.
* **Canon CR3** -- ISO base media format, the same box structure as an MP4.
  Every EOS R body writes these.

Neither is decoded here. All this module does is answer "where in this file does
something we already know how to read begin", and hand back byte offsets. That
keeps the TIFF reader honest about being a TIFF reader, and it means a new
container is a new function here rather than a new special case in there.

Offsets returned are absolute within the file. That matters more than it looks:
a TIFF's internal offsets are relative to the start of its own header, so a
block embedded at byte 300,000 produces locations that are wrong by exactly that
amount unless the base is carried alongside.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

RAF_MAGIC = b"FUJIFILMCCD-RAW "

# Canon writes two UUID boxes. The first carries the metadata boxes (CMT1..CMT4,
# which are plain TIFF blocks); the second carries the preview image.
CR3_METADATA_UUID = bytes.fromhex("85c0b687820f11e08111f4ce462b6a48")
CR3_PREVIEW_UUID = bytes.fromhex("eaf42b5e1c984b88b9fbb7dc406e4d16")

# Depth and count limits. A malformed or truncated file must terminate the walk
# rather than loop; these are far above anything a real camera writes.
_MAX_BOXES = 4096
_MAX_DEPTH = 8

# A JPEG always begins with these three bytes. The preview boxes carry a short
# fixed header before the image, and rather than hard-code a layout that varies
# by firmware, the marker is located within that short header -- bounded to a
# few dozen bytes, so this is a lookup, not the whole-file marker scan that
# reading offsets directly exists to avoid.
_SOI = b"\xff\xd8\xff"
_HEADER_SEARCH_BYTES = 64

# Some container boxes carry a few bytes of version or padding before their
# first child rather than starting with one. Canon writes eight such bytes
# inside the preview UUID, ahead of PRVW. Reading that padding as a box header
# yields a nameless box and the real preview is never seen -- which is not
# hypothetical: it silently downgraded every CR3 to its 160x120 thumbnail, and
# the measurements taken off that came out plausible and meaningless.
_MAX_BOX_PREFIX = 16


@dataclass(frozen=True, slots=True)
class EmbeddedBlock:
    """A byte range inside a container file."""

    offset: int
    length: int


@dataclass(frozen=True, slots=True)
class ContainerLayout:
    """What a non-TIFF container has to offer.

    ``tiff_bases`` are absolute offsets at which a complete TIFF block (byte
    order mark and all) begins, in the order they should be trusted. ``previews``
    are JPEG byte ranges, largest-first is not assumed -- the caller ranks them.
    """

    tiff_bases: tuple[int, ...] = ()
    previews: tuple[EmbeddedBlock, ...] = ()
    jpeg_with_exif: tuple[EmbeddedBlock, ...] = ()
    # The photograph's own dimensions, when the container states them somewhere
    # the TIFF directories do not. ``None`` means "ask the directories".
    frame_size: tuple[int, int] | None = None


def sniff(path: Path) -> str | None:
    """Name the container format from its first bytes, or ``None`` for TIFF.

    Cheap enough to do on every file: sixteen bytes off the front. Returning
    ``None`` means "no wrapper, hand it to the TIFF reader as-is", which is the
    common case and deliberately the one that costs least.
    """
    with path.open("rb") as stream:
        head = stream.read(16)
    if head.startswith(RAF_MAGIC):
        return "raf"
    # ISO base media: a 4-byte big-endian box length followed by 'ftyp'.
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "cr3"
    return None


def layout(path: Path, kind: str) -> ContainerLayout:
    """Describe a container of the given ``kind``."""
    if kind == "raf":
        return _raf_layout(path)
    if kind == "cr3":
        return _cr3_layout(path)
    raise ValueError(f"unknown container kind {kind!r}")


# --------------------------------------------------------------------------
# Fujifilm RAF


# Field positions in the RAF header. Fixed since the format appeared; the
# numbers are the whole specification anyone needs for reading a preview.
_RAF_JPEG_OFFSET = 84
_RAF_CFA_HEADER_OFFSET = 92
_RAF_HEADER_BYTES = 108

# Fujifilm's CFA header is a simple tag list, and two of its entries give sizes.
# 0x0100 is the full sensor including the calibration border; 0x0111 is the
# image after that border is cropped away, which is the photograph. Both are
# stored (height, width), which is the reverse of every other convention here.
_RAF_TAG_FULL_SIZE = 0x0100
_RAF_TAG_CROPPED_SIZE = 0x0111
_RAF_MAX_CFA_ENTRIES = 256


def _raf_layout(path: Path) -> ContainerLayout:
    """Read the RAF header's pointer to its embedded JPEG.

    The JPEG is a complete file with its own APP1/Exif segment, so it serves as
    both the preview and the metadata source -- there is no separate TIFF block
    to find, only the one nested inside that JPEG.
    """
    size = path.stat().st_size
    with path.open("rb") as stream:
        header = stream.read(_RAF_HEADER_BYTES)
    if len(header) < _RAF_HEADER_BYTES:
        raise ValueError("RAF header is truncated")

    offset, length = struct.unpack_from(">II", header, _RAF_JPEG_OFFSET)
    if not offset or not length or offset + length > size:
        raise ValueError("RAF header does not point at a usable JPEG")

    # The embedded JPEG is not the full frame -- a 102MP GFX writes a 4000-wide
    # preview -- and RAF has no TIFF directory stating the real dimensions. They
    # live in the CFA header instead, so without this a report would describe a
    # 102-megapixel photograph as a 12-megapixel one.
    cfa_offset, cfa_length = struct.unpack_from(">II", header, _RAF_CFA_HEADER_OFFSET)
    frame = _raf_frame_size(path, cfa_offset, cfa_length, size)

    block = EmbeddedBlock(offset, length)
    return ContainerLayout(previews=(block,), jpeg_with_exif=(block,), frame_size=frame)


def _raf_frame_size(
    path: Path, offset: int, length: int, file_size: int
) -> tuple[int, int] | None:
    """Read the frame's real dimensions out of the RAF CFA header tag list."""
    if not offset or not length or offset + length > file_size:
        return None
    with path.open("rb") as stream:
        stream.seek(offset)
        blob = stream.read(length)
    if len(blob) < 4:
        return None

    (count,) = struct.unpack_from(">I", blob, 0)
    found: dict[int, tuple[int, int]] = {}
    position = 4
    for _ in range(min(count, _RAF_MAX_CFA_ENTRIES)):
        if position + 4 > len(blob):
            break
        tag, size = struct.unpack_from(">HH", blob, position)
        position += 4
        if tag in (_RAF_TAG_FULL_SIZE, _RAF_TAG_CROPPED_SIZE) and size == 4:
            height, width = struct.unpack_from(">HH", blob, position)
            if width and height:
                found[tag] = (width, height)
        position += size

    return found.get(_RAF_TAG_CROPPED_SIZE) or found.get(_RAF_TAG_FULL_SIZE)


def exif_base_in_jpeg(path: Path, block: EmbeddedBlock) -> int | None:
    """Absolute offset of the TIFF header inside a JPEG's APP1/Exif segment.

    Walks the JPEG's marker segments, which are length-prefixed, so this reads a
    handful of bytes rather than searching. Returns ``None`` when the JPEG
    carries no Exif, which is legal and not an error.
    """
    with path.open("rb") as stream:
        stream.seek(block.offset)
        if stream.read(2) != b"\xff\xd8":
            return None
        position = block.offset + 2
        end = block.offset + block.length
        while position < end:
            stream.seek(position)
            marker = stream.read(2)
            if len(marker) < 2 or marker[0] != 0xFF:
                return None
            code = marker[1]
            # Standalone markers carry no length; start-of-scan means the
            # metadata is behind us and the entropy-coded data has begun.
            if code in (0xD8, 0xD9) or 0xD0 <= code <= 0xD7:
                position += 2
                continue
            if code == 0xDA:
                return None
            raw_length = stream.read(2)
            if len(raw_length) < 2:
                return None
            (segment_length,) = struct.unpack(">H", raw_length)
            if segment_length < 2:
                return None
            if code == 0xE1 and stream.read(6) == b"Exif\x00\x00":
                return position + 4 + 6
            position += 2 + segment_length
    return None


# --------------------------------------------------------------------------
# Canon CR3


def _cr3_layout(path: Path) -> ContainerLayout:
    """Walk the CR3 box tree for its metadata TIFF blocks and preview images.

    Canon nests the useful parts two levels down, inside UUID boxes: the CMT
    boxes are complete TIFF blocks holding what other formats call IFD0 and the
    Exif IFD, and PRVW holds a JPEG large enough to measure at -- around
    1620x1080, comfortably above the working resolution.

    The full-resolution JPEG lives in ``mdat``, reachable only through the movie
    track tables. Not read here: the preview is already larger than anything the
    measurement uses, and following sample tables to get a bigger image nobody
    downsamples less would be work spent for nothing.
    """
    bases: list[int] = []
    previews: list[EmbeddedBlock] = []

    with path.open("rb") as stream:
        for name, start, length, depth in _walk_boxes(stream, 0, path.stat().st_size):
            if name in ("CMT1", "CMT2", "CMT3", "CMT4"):
                bases.append(start)
            elif name in ("PRVW", "THMB"):
                block = _jpeg_in_box(stream, start, length)
                if block is not None:
                    previews.append(block)

    if not bases and not previews:
        raise ValueError("no Canon metadata or preview boxes found in this CR3")
    return ContainerLayout(tiff_bases=tuple(bases), previews=tuple(previews))


def _is_box_type(raw: bytes) -> bool:
    """Box types are four printable characters. Padding is not."""
    return len(raw) == 4 and all(32 <= byte < 127 for byte in raw)


def _first_box_offset(stream: BinaryIO, start: int, end: int) -> int:
    """Skip any leading padding to where this container's first child begins.

    Bounded and four-byte aligned, so a box full of arbitrary data cannot turn
    this into a search. Falls back to ``start`` unchanged, which then fails the
    ordinary validity checks rather than inventing structure.
    """
    for offset in range(start, min(start + _MAX_BOX_PREFIX, end - 8) + 1, 4):
        stream.seek(offset)
        header = stream.read(8)
        if len(header) < 8:
            break
        size, raw_type = struct.unpack(">I4s", header)
        if _is_box_type(raw_type) and 8 <= size <= end - offset:
            return offset
    return start


def _walk_boxes(
    stream: BinaryIO, start: int, end: int, depth: int = 0
) -> list[tuple[str, int, int, int]]:
    """Yield ``(type, payload_offset, payload_length, depth)`` for every box.

    Recurses only into the containers that are known to hold something wanted,
    which keeps a deliberately malformed file from costing an unbounded walk.
    """
    found: list[tuple[str, int, int, int]] = []
    if depth > _MAX_DEPTH:
        return found

    position = start
    seen = 0
    while position < end and seen < _MAX_BOXES:
        seen += 1
        stream.seek(position)
        header = stream.read(8)
        if len(header) < 8:
            break
        size, raw_type = struct.unpack(">I4s", header)
        name = raw_type.decode("ascii", "replace")
        payload = position + 8

        if size == 1:
            extended = stream.read(8)
            if len(extended) < 8:
                break
            (size,) = struct.unpack(">Q", extended)
            payload = position + 16
        elif size == 0:
            size = end - position  # a final box running to the end of the file
        if size < 8 or position + size > end:
            break

        payload_length = position + size - payload

        if name == "uuid":
            identifier = stream.read(16)
            if identifier in (CR3_METADATA_UUID, CR3_PREVIEW_UUID):
                limit = position + size
                found += _walk_boxes(
                    stream, _first_box_offset(stream, payload + 16, limit), limit, depth + 1
                )
        elif name in ("moov", "trak", "mdia", "minf", "stbl", "CCTP"):
            limit = position + size
            found += _walk_boxes(
                stream, _first_box_offset(stream, payload, limit), limit, depth + 1
            )
        else:
            found.append((name, payload, payload_length, depth))

        position += size
    return found


def _jpeg_in_box(stream: BinaryIO, start: int, length: int) -> EmbeddedBlock | None:
    """Locate the JPEG inside a PRVW or THMB box.

    Both boxes prefix the image with a short record of dimensions and size. The
    exact field layout has moved between firmware generations, so the JPEG's own
    start-of-image marker is located inside that short prefix instead -- bounded
    to the first few dozen bytes of the box, which is a header read rather than a
    scan.
    """
    if length <= 0:
        return None
    stream.seek(start)
    head = stream.read(min(_HEADER_SEARCH_BYTES, length))
    marker = head.find(_SOI)
    if marker < 0:
        return None
    offset = start + marker
    return EmbeddedBlock(offset, length - marker)
