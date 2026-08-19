"""A minimal TIFF/EXIF directory reader, standard library only.

Raw files from every major manufacturer are TIFF containers. Reading their
directory structure directly buys two things that matter here:

* the byte offset and length of the embedded JPEG preview, so a 25 MB NEF is
  opened by reading a few hundred kilobytes from a known position rather than
  scanning the whole file for JPEG markers
* the EXIF tags, without depending on a raw decoding library being installed

This is deliberately not a general TIFF implementation. It reads directories and
scalar values, and it declines to guess about anything else.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Sequence

from . import containers

# TIFF type code -> (struct format character, byte width)
_TYPES: dict[int, tuple[str, int]] = {
    1: ("B", 1),  # BYTE
    2: ("s", 1),  # ASCII
    3: ("H", 2),  # SHORT
    4: ("I", 4),  # LONG
    5: ("II", 8),  # RATIONAL
    6: ("b", 1),  # SBYTE
    7: ("s", 1),  # UNDEFINED
    8: ("h", 2),  # SSHORT
    9: ("i", 4),  # SLONG
    10: ("ii", 8),  # SRATIONAL
    11: ("f", 4),  # FLOAT
    12: ("d", 8),  # DOUBLE
}

# Panasonic keeps the full JPEG preview in its own tag rather than in the
# standard preview pair, and reports the frame size in private tags too. RW2 has
# no ImageWidth/ImageLength in IFD0 at all.
TAG_PANASONIC_JPEG_FROM_RAW = 0x002E
TAG_PANASONIC_IMAGE_HEIGHT = 0x0006
TAG_PANASONIC_IMAGE_WIDTH = 0x0007
TAG_PANASONIC_ISO = 0x0017

TAG_NEW_SUBFILE_TYPE = 0x00FE
TAG_IMAGE_WIDTH = 0x0100
TAG_IMAGE_LENGTH = 0x0101
TAG_COMPRESSION = 0x0103
TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_STRIP_OFFSETS = 0x0111
TAG_ORIENTATION = 0x0112
TAG_STRIP_BYTE_COUNTS = 0x0117
TAG_SUB_IFDS = 0x014A
TAG_JPEG_OFFSET = 0x0201
TAG_JPEG_LENGTH = 0x0202
TAG_EXIF_IFD = 0x8769
TAG_EXPOSURE_TIME = 0x829A
TAG_F_NUMBER = 0x829D
TAG_ISO = 0x8827
TAG_DATETIME_ORIGINAL = 0x9003
TAG_FOCAL_LENGTH = 0x920A
TAG_MAKER_NOTE = 0x927C
TAG_FOCAL_LENGTH_35MM = 0xA405
TAG_LENS_MODEL = 0xA434

_COMPRESSION_JPEG = {6, 7}

# 42 is the standard. The others are manufacturers who kept TIFF's structure and
# changed its identifier: Olympus ORF ("OR"/"SR" as a little-endian short), and
# Panasonic RW2, which is an ordinary little-endian TIFF wearing an 85.
_TIFF_MAGIC = frozenset({42, 85, 0x4F52, 0x5352})

# A TIFF offset of all-ones is a sentinel, not a location. Panasonic writes it
# into StripOffsets on every RW2; following it seeks four gigabytes into a
# fifteen-megabyte file.
_OFFSET_SENTINEL = 0xFFFFFFFF

# Values longer than this are recorded as a location rather than read. The
# motivating cases are large: a Panasonic embedded preview is three quarters of
# a megabyte and a Nikon MakerNote is not much smaller, and reading either into
# a dictionary -- once per photograph, across eight worker processes -- buys
# nothing, because the only thing anyone wants from them is where they start.
_MAX_INLINE_BYTES = 4096

# A single directory should never contain this many entries; a larger count
# means we have followed a bad offset into arbitrary data and should stop
# rather than allocate against a garbage length.
_MAX_ENTRIES = 4096
_MAX_IFDS = 64


@dataclass(frozen=True, slots=True)
class ValueLocation:
    """Where a large tag value lives, for tags nobody wants the bytes of.

    ``offset`` is absolute within the file, so it stays correct for containers
    whose TIFF block does not start at byte zero.
    """

    offset: int
    length: int


@dataclass(frozen=True, slots=True)
class PreviewLocation:
    """Where an embedded JPEG lives inside a container file."""

    offset: int
    length: int
    width: int | None
    height: int | None

    @property
    def pixels(self) -> int:
        """Pixel count if known, else byte length as a stand-in for 'bigger'."""
        if self.width and self.height:
            return self.width * self.height
        return self.length


class TiffDirectory(dict):
    """One IFD: a mapping of tag number to decoded value."""

    def text(self, tag: int) -> str | None:
        value = self.get(tag)
        if isinstance(value, str):
            cleaned = value.strip().strip("\x00").strip()
            return cleaned or None
        return None

    def number(self, tag: int) -> float | None:
        value = self.get(tag)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, tuple) and len(value) == 2 and value[1]:
            return float(value[0]) / float(value[1])
        if isinstance(value, list) and value:
            return TiffDirectory({0: value[0]}).number(0)
        return None

    def integer(self, tag: int) -> int | None:
        number = self.number(tag)
        return int(number) if number is not None else None


class TiffReader:
    """Reads directories from an open TIFF-structured stream."""

    def __init__(self, stream: BinaryIO, base: int = 0) -> None:
        self._stream = stream
        self._base = base
        self._stream.seek(base)
        header = self._stream.read(8)
        if len(header) < 8:
            raise ValueError("file too short to be a TIFF container")
        if header[:2] == b"II":
            self._endian = "<"
        elif header[:2] == b"MM":
            self._endian = ">"
        else:
            raise ValueError(f"not a TIFF container: byte order mark {header[:2]!r}")
        magic, first_ifd = struct.unpack(self._endian + "HI", header[2:8])
        if magic not in _TIFF_MAGIC:
            raise ValueError(f"unexpected TIFF magic {magic}")
        self._first_ifd = first_ifd

    def _unpack(self, fmt: str, data: bytes) -> tuple[Any, ...]:
        return struct.unpack(self._endian + fmt, data)

    def _read_value(self, type_code: int, count: int, payload: bytes, offset: int) -> Any:
        if type_code not in _TYPES:
            return None
        fmt, width = _TYPES[type_code]
        total = width * count
        if total > 4:
            (pointer,) = self._unpack("I", payload)
            if pointer == _OFFSET_SENTINEL:
                return None
            absolute = self._base + pointer
            if type_code in (2, 7) and total > _MAX_INLINE_BYTES:
                # Opaque and large: record where it is instead of hauling it
                # around. Callers that genuinely want the bytes read them.
                return ValueLocation(absolute, total)
            self._stream.seek(absolute)
            raw = self._stream.read(total)
        else:
            raw = payload[:total]
        if len(raw) < total:
            return None

        if type_code in (2, 7):
            if type_code == 7:
                return raw
            return raw.split(b"\x00", 1)[0].decode("ascii", "replace")
        if type_code in (5, 10):
            item = "II" if type_code == 5 else "ii"
            values = [self._unpack(item, raw[i * 8 : i * 8 + 8]) for i in range(count)]
            return values[0] if count == 1 else values
        values = list(self._unpack(fmt * count, raw))
        return values[0] if count == 1 else values

    def read_directory(self, offset: int) -> tuple[TiffDirectory, int]:
        """Read the IFD at ``offset``; return it and the offset of the next one."""
        self._stream.seek(self._base + offset)
        header = self._stream.read(2)
        if len(header) < 2:
            return TiffDirectory(), 0
        (count,) = self._unpack("H", header)
        if count > _MAX_ENTRIES:
            return TiffDirectory(), 0

        entries = self._stream.read(count * 12)
        next_raw = self._stream.read(4)
        directory = TiffDirectory()
        for index in range(count):
            chunk = entries[index * 12 : index * 12 + 12]
            if len(chunk) < 12:
                break
            tag, type_code, item_count = self._unpack("HHI", chunk[:8])
            if item_count > 1 << 24:
                continue
            try:
                directory[tag] = self._read_value(type_code, item_count, chunk[8:12], offset)
            except (struct.error, OSError, ValueError):
                continue

        next_offset = self._unpack("I", next_raw)[0] if len(next_raw) == 4 else 0
        return directory, next_offset

    def directories(self) -> Iterator[TiffDirectory]:
        """Yield IFD0, its chain, and every SubIFD and EXIF IFD reachable from them."""
        seen: set[int] = set()
        pending = [self._first_ifd]
        produced = 0
        while pending and produced < _MAX_IFDS:
            offset = pending.pop(0)
            if offset <= 0 or offset in seen:
                continue
            seen.add(offset)
            try:
                directory, next_offset = self.read_directory(offset)
            except (struct.error, OSError, ValueError):
                continue
            produced += 1
            yield directory

            if next_offset:
                pending.append(next_offset)
            for pointer_tag in (TAG_SUB_IFDS, TAG_EXIF_IFD):
                value = directory.get(pointer_tag)
                if isinstance(value, int):
                    pending.append(value)
                elif isinstance(value, list):
                    pending.extend(v for v in value if isinstance(v, int))


def _preview_from_directory(directory: TiffDirectory, base: int = 0) -> PreviewLocation | None:
    """Extract a JPEG preview location from one IFD, if it holds one.

    ``base`` is where this IFD's TIFF block starts in the file. Offsets inside a
    TIFF are relative to that, and for a container that embeds its TIFF at a
    non-zero position -- Fujifilm RAF, Canon CR3 -- forgetting to add it seeks
    into unrelated bytes and produces a preview that will not decode.
    """
    width = directory.integer(TAG_IMAGE_WIDTH) or directory.integer(TAG_PANASONIC_IMAGE_WIDTH)
    height = directory.integer(TAG_IMAGE_LENGTH) or directory.integer(TAG_PANASONIC_IMAGE_HEIGHT)

    # Panasonic stores the full preview under a private tag as an opaque blob,
    # which the reader has already declined to load and handed back as a
    # location. That location is exactly what is wanted here.
    panasonic = directory.get(TAG_PANASONIC_JPEG_FROM_RAW)
    if isinstance(panasonic, ValueLocation) and panasonic.length > 0:
        return PreviewLocation(panasonic.offset, panasonic.length, None, None)

    offset = directory.get(TAG_JPEG_OFFSET)
    length = directory.get(TAG_JPEG_LENGTH)
    if _usable_offset(offset) and isinstance(length, int) and length > 0:
        return PreviewLocation(base + offset, length, width, height)

    # Some bodies store the full-size preview as a single JPEG-compressed strip
    # rather than in the dedicated preview tags.
    compression = directory.integer(TAG_COMPRESSION)
    strip_offsets = directory.get(TAG_STRIP_OFFSETS)
    strip_counts = directory.get(TAG_STRIP_BYTE_COUNTS)
    if (
        compression in _COMPRESSION_JPEG
        and _usable_offset(strip_offsets)
        and isinstance(strip_counts, int)
        and strip_counts > 0
    ):
        return PreviewLocation(base + strip_offsets, strip_counts, width, height)
    return None


def _usable_offset(value: object) -> bool:
    """An offset is usable when it is a positive integer and not the sentinel."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < _OFFSET_SENTINEL


def read_directories(path: Path, base: int = 0) -> list[TiffDirectory]:
    """Every IFD reachable from the TIFF block starting at ``base``."""
    with path.open("rb") as stream:
        return list(TiffReader(stream, base=base).directories())


def find_largest_preview(path: Path) -> tuple[PreviewLocation | None, list[TiffDirectory]]:
    """Locate the biggest embedded JPEG preview and return it with all directories.

    Returning the directories alongside costs nothing -- they are already parsed
    -- and saves a second open of the file when EXIF is wanted too.

    Formats that wrap their TIFF in something else are unwrapped first. That
    dispatch lives here rather than in the loader because "where does the image
    start" is a container question, and the loader should not have to know which
    manufacturers kept to the standard.
    """
    kind = containers.sniff(path)
    if kind is None:
        directories = read_directories(path)
        candidates = [p for p in (_preview_from_directory(d, 0) for d in directories) if p]
        return (max(candidates, key=lambda p: p.pixels) if candidates else None), directories

    plan = containers.layout(path, kind)

    bases = list(plan.tiff_bases)
    for block in plan.jpeg_with_exif:
        # A wrapper whose payload is a whole JPEG carries its TIFF inside that
        # JPEG's Exif segment rather than as a block of its own.
        nested = containers.exif_base_in_jpeg(path, block)
        if nested is not None:
            bases.append(nested)

    # Each directory is kept with the base it was read at. A nested block's
    # internal offsets are relative to its own header, so a thumbnail IFD inside
    # an embedded JPEG points somewhere entirely different if the two are mixed.
    found: list[tuple[int, TiffDirectory]] = []
    for base in bases:
        try:
            found += [(base, directory) for directory in read_directories(path, base)]
        except (OSError, ValueError, struct.error):
            continue  # one unreadable metadata block must not lose the preview
    directories = [directory for _, directory in found]

    candidates = [
        PreviewLocation(block.offset, block.length, None, None) for block in plan.previews
    ]
    candidates += [
        p for p in (_preview_from_directory(d, base) for base, d in found) if p
    ]
    if not candidates:
        return None, directories
    return max(candidates, key=lambda p: p.pixels), directories


def frame_size(path: Path, directories: Sequence[TiffDirectory]) -> tuple[int, int] | None:
    """The photograph's own pixel dimensions.

    The embedded preview is not always full size -- Panasonic writes a 1920-wide
    JPEG into a 4008-wide photograph, and Fujifilm a 4000-wide one into a
    102-megapixel frame -- so taking the loaded preview's size as the file's
    dimensions understates the frame by a large factor, in a field the report
    presents as a fact about the photograph.

    A container that states the size itself is believed first: RAF keeps it in
    the CFA header and has no TIFF directory to put it in. Otherwise the largest
    declared dimensions across the directories win, because the directory
    holding them describes the sensor image and the smaller ones are thumbnails.
    """
    kind = containers.sniff(path)
    if kind is not None:
        try:
            declared = containers.layout(path, kind).frame_size
        except (OSError, ValueError):
            declared = None
        if declared:
            return declared

    best: tuple[int, int] | None = None
    for directory in directories:
        width = directory.integer(TAG_IMAGE_WIDTH) or directory.integer(TAG_PANASONIC_IMAGE_WIDTH)
        height = directory.integer(TAG_IMAGE_LENGTH) or directory.integer(TAG_PANASONIC_IMAGE_HEIGHT)
        if not width or not height or width <= 0 or height <= 0:
            continue
        if best is None or width * height > best[0] * best[1]:
            best = (width, height)
    return best


def read_preview_bytes(path: Path, location: PreviewLocation) -> bytes:
    """Read exactly the preview's bytes from the container."""
    with path.open("rb") as stream:
        stream.seek(location.offset)
        return stream.read(location.length)
