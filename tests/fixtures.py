"""Synthetic camera files, built byte by byte.

Real raw files are the wrong thing to commit: a single NEF is 25 MB, and a
repository that ships a gigabyte of camera output to test a header parser has
solved the wrong problem. But the thing that breaks in a header parser is a
header, and a header is sixteen bytes anyone can construct.

So these builders write the container structure exactly as the manufacturers
document it, wrapped around a real JPEG that Pillow generates. That is enough to
prove the byte order marks, magic numbers, box walks and offset arithmetic are
right -- which is precisely the class of fault that had CR3, RAF and RW2
reporting "not a readable raw container" for every frame.

What they cannot prove is that a real camera writes what its documentation says.
Only a real file does that, and where one was available -- Panasonic -- the
suite uses it.
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

import numpy as np
from PIL import Image, ImageColor

# TIFF type codes used by the builders below.
TYPE_SHORT = 3
TYPE_LONG = 4
TYPE_ASCII = 2
TYPE_UNDEFINED = 7

_TYPE_WIDTH = {TYPE_ASCII: 1, TYPE_SHORT: 2, TYPE_LONG: 4, TYPE_UNDEFINED: 1}


def jpeg_bytes(
    size: tuple[int, int] = (640, 480),
    colour: str = "grey",
    camera: tuple[str, str] | None = None,
    orientation: int | None = None,
    sub_ifd: dict[int, object] | None = None,
) -> bytes:
    """A real, decodable JPEG. Textured, so it is not uniformly zero acutance.

    Passing ``camera`` gives it a genuine APP1/Exif segment, which is what a
    container that wraps a whole JPEG -- Fujifilm RAF -- carries its metadata in.

    ``sub_ifd`` writes tags into the Exif sub-directory at 0x8769, which is where
    a real camera puts everything interesting: shutter, aperture, ISO, capture
    time, focal length, lens. Reading a JPEG's metadata without following that
    pointer gets you the camera name and nothing else, so a fixture that cannot
    write there cannot test the difference.
    """
    # A four-pixel grid of white, laid down with NumPy. A per-pixel loop over a
    # 1920x1440 fixture is fine once and intolerable across a parametrised suite.
    array = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    array[:] = np.array(ImageColor.getrgb(colour), dtype=np.uint8)
    array[::4, ::4] = 255
    image = Image.fromarray(array)
    buffer = io.BytesIO()
    if camera is not None or orientation is not None or sub_ifd:
        exif = Image.Exif()
        if camera is not None:
            exif[0x010F], exif[0x0110] = camera
        exif[0x0100], exif[0x0101] = size
        if orientation is not None:
            exif[0x0112] = orientation
        if sub_ifd:
            exif.get_ifd(0x8769).update(sub_ifd)
        image.save(buffer, format="JPEG", quality=90, exif=exif)
    else:
        image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def build_tiff(
    entries: dict[int, tuple[int, object]],
    byte_order: bytes = b"II",
    magic: int = 42,
    trailing: bytes = b"",
) -> bytes:
    """Assemble a one-IFD TIFF block from ``{tag: (type_code, value)}``.

    Values too large for an entry's four inline bytes are appended after the
    directory and referenced by offset, which is the arrangement that makes
    offset arithmetic worth testing in the first place.
    """
    endian = "<" if byte_order == b"II" else ">"
    header = byte_order + struct.pack(endian + "HI", magic, 8)

    count = len(entries)
    directory_bytes = 2 + count * 12 + 4
    overflow_start = 8 + directory_bytes

    directory = struct.pack(endian + "H", count)
    overflow = b""
    for tag in sorted(entries):
        type_code, value = entries[tag]
        if type_code == TYPE_ASCII:
            payload = value.encode("ascii") + b"\x00" if isinstance(value, str) else value
            item_count = len(payload)
        elif type_code == TYPE_UNDEFINED:
            payload = value
            item_count = len(payload)
        else:
            values = value if isinstance(value, (list, tuple)) else [value]
            item_count = len(values)
            fmt = "H" if type_code == TYPE_SHORT else "I"
            payload = struct.pack(endian + fmt * item_count, *values)

        total = _TYPE_WIDTH[type_code] * item_count
        if total > 4:
            directory += struct.pack(
                endian + "HHII", tag, type_code, item_count, overflow_start + len(overflow)
            )
            overflow += payload
        else:
            directory += struct.pack(endian + "HHI", tag, type_code, item_count)
            directory += payload.ljust(4, b"\x00")
    directory += struct.pack(endian + "I", 0)  # no next IFD

    return header + directory + overflow + trailing


def write_rw2(path: Path, preview: bytes | None = None) -> Path:
    """A Panasonic RW2: little-endian TIFF, magic 85, preview under tag 0x002E.

    The bogus ``StripOffsets`` of all-ones is not an embellishment -- real files
    carry it, and following it seeks four gigabytes past the end.
    """
    preview = preview if preview is not None else jpeg_bytes((1920, 1440))
    path.write_bytes(
        build_tiff(
            {
                0x0006: (TYPE_SHORT, 3008),  # Panasonic image height
                0x0007: (TYPE_SHORT, 4008),  # Panasonic image width
                0x0017: (TYPE_SHORT, 400),  # Panasonic ISO
                0x002E: (TYPE_UNDEFINED, preview),  # JpgFromRaw
                0x010F: (TYPE_ASCII, "Panasonic"),
                0x0110: (TYPE_ASCII, "DMC-FZ200"),
                0x0111: (TYPE_LONG, 0xFFFFFFFF),  # StripOffsets sentinel
                0x0117: (TYPE_LONG, 0),
            },
            magic=85,
        )
    )
    return path


def write_raf(path: Path, preview: bytes | None = None) -> Path:
    """A Fujifilm RAF: a text magic, then big-endian pointers to a real JPEG.

    Includes the CFA header tag list, because that is the only place a RAF
    states the photograph's real dimensions -- the embedded JPEG is a downsized
    preview, and a 102MP GFX frame carries a 4000-wide one.
    """
    preview = preview if preview is not None else jpeg_bytes(
        (1440, 960), camera=("FUJIFILM", "X-T4")
    )

    # Fujifilm's CFA header: a count, then (tag, size, data) entries. Sizes are
    # stored height-first, which is the reverse of every other convention here.
    entries = b""
    for tag, (height, width) in ((0x0100, (4182, 6384)), (0x0111, (4160, 6240))):
        entries += struct.pack(">HHHH", tag, 4, height, width)
    cfa_header = struct.pack(">I", 2) + entries

    jpeg_offset = 148
    cfa_offset = jpeg_offset + len(preview)

    header = bytearray(bytes(jpeg_offset))
    header[0:16] = b"FUJIFILMCCD-RAW "
    header[16:20] = b"0201"
    header[28:60] = b"X-T4".ljust(32, bytes(1))
    struct.pack_into(
        ">IIIIII",
        header,
        84,
        jpeg_offset,
        len(preview),
        cfa_offset,
        len(cfa_header),
        cfa_offset + len(cfa_header),
        0,
    )

    path.write_bytes(bytes(header) + preview + cfa_header)
    return path


def _box(name: bytes, payload: bytes) -> bytes:
    """One ISO base media box: a big-endian length, a four-character type."""
    return struct.pack(">I", len(payload) + 8) + name + payload


# The two UUIDs Canon uses, mirrored from the container module so a change in
# one is caught by a failing test rather than by silence.
_CR3_METADATA_UUID = bytes.fromhex("85c0b687820f11e08111f4ce462b6a48")
_CR3_PREVIEW_UUID = bytes.fromhex("eaf42b5e1c984b88b9fbb7dc406e4d16")


def write_cr3(path: Path, preview: bytes | None = None) -> Path:
    """A Canon CR3: ISO base media boxes, CMT1 metadata, THMB and PRVW images.

    Both image boxes are present, because a real file has both and the reader
    has to prefer the large one. Getting that wrong silently downgraded every
    Canon frame to a 160x120 thumbnail, and the measurements taken off it came
    out plausible and meaningless.
    """
    preview = preview if preview is not None else jpeg_bytes((1620, 1080))
    thumbnail = jpeg_bytes((160, 120))

    cmt1 = build_tiff(
        {
            0x010F: (TYPE_ASCII, "Canon"),
            0x0110: (TYPE_ASCII, "Canon EOS R5"),
            0x0100: (TYPE_LONG, 8192),
            0x0101: (TYPE_LONG, 5464),
        }
    )

    ftyp = _box(b"ftyp", b"crx " + struct.pack(">I", 1) + b"crx isom")
    thmb = _box(b"THMB", struct.pack(">IHHI", 0, 160, 120, len(thumbnail)) + thumbnail)
    moov = _box(b"moov", _box(b"uuid", _CR3_METADATA_UUID + _box(b"CMT1", cmt1) + thmb))

    # Canon writes eight bytes of padding between the UUID and the first child
    # box. A fixture without it describes a container Canon does not produce --
    # and that omission is exactly what hid the thumbnail fault.
    prvw = _box(
        b"PRVW", struct.pack(">IHHHH", 0, 1, 1620, 1080, 1) + struct.pack(">I", len(preview)) + preview
    )
    preview_box = _box(b"uuid", _CR3_PREVIEW_UUID + bytes(8) + prvw)

    path.write_bytes(ftyp + moov + preview_box)
    return path
