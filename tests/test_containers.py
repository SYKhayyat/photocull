"""Container parsing: every format the tool claims to open, actually opened.

The premise of these tests is finding #4 of the design review: ``RAW_SUFFIXES``
advertised twelve extensions, three of which could not be opened at all, and the
failure was silent -- a full, well-formatted report of nothing. The module that
broke had 263 lines of binary header parsing and no tests, which is one fact and
not two.

So the central test here is the boring one: for every suffix in the advertised
list, assert the loader either produces an image or fails with an intended
message. It is fifteen lines and it would have caught the original fault at
authoring time.
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

import pytest
from PIL import Image

from photocull import containers, exif
from photocull.errors import UnreadableImage
from photocull.loading import RAW_SUFFIXES, RawPreviewLoader, build_registry
from photocull.tiffreader import (
    TiffReader,
    ValueLocation,
    find_largest_preview,
    frame_size,
    read_directories,
    read_preview_bytes,
)

from . import fixtures as F

# The three that were broken, and how each is built.
BUILDERS = {".rw2": F.write_rw2, ".raf": F.write_raf, ".cr3": F.write_cr3}

# Real camera files, if this machine happens to have any. Synthetic headers
# prove the arithmetic; only a real file proves the camera writes what the
# documentation says -- and on the first contact with real files they did not.
# Canon put eight bytes of padding where the fixture had none, which had every
# CR3 measured on its 160x120 thumbnail; Fujifilm kept the frame dimensions in a
# structure the fixture did not model at all.
#
# Samples are CC0 and come from https://raw.pixls.us. Skipped when absent, so
# the suite stays runnable on a machine with no camera files on it.
_DOWNLOADS = Path.home() / "Downloads"


def _samples(*patterns: str) -> list[Path]:
    if not _DOWNLOADS.is_dir():
        return []
    found: list[Path] = []
    for pattern in patterns:
        found += sorted(_DOWNLOADS.glob(pattern))
    return found


REAL_RW2 = _samples("*.RW2", "rawsamples/*.RW2")
REAL_RAF = _samples("*.RAF", "rawsamples/*.RAF")
REAL_CR3 = _samples("*.CR3", "rawsamples/*.CR3")


def _decode(path: Path) -> Image.Image:
    location, _ = find_largest_preview(path)
    assert location is not None, f"no preview found in {path.name}"
    return Image.open(io.BytesIO(read_preview_bytes(path, location)))


@pytest.mark.parametrize("suffix", sorted(BUILDERS))
def test_previously_unopenable_formats_now_decode(tmp_path, suffix):
    """CR3, RAF and RW2 produce a decodable preview rather than an error row."""
    path = BUILDERS[suffix](tmp_path / f"frame{suffix}")
    assert _decode(path).format == "JPEG"


@pytest.mark.parametrize("suffix", sorted(RAW_SUFFIXES))
def test_every_advertised_suffix_is_reachable(tmp_path, suffix):
    """No entry in RAW_SUFFIXES may fail merely because of its container.

    The nine formats not built explicitly are ordinary TIFF containers, so a
    plain TIFF standing in for each proves the reader accepts the shape. The
    point is the absence of a silent gap: a suffix nobody can open must not sit
    in the advertised list unnoticed.
    """
    builder = BUILDERS.get(suffix)
    if builder is not None:
        path = builder(tmp_path / f"frame{suffix}")
    else:
        preview = F.jpeg_bytes((800, 600))
        path = tmp_path / f"frame{suffix}"
        path.write_bytes(
            F.build_tiff(
                {
                    0x0100: (F.TYPE_LONG, 800),
                    0x0101: (F.TYPE_LONG, 600),
                    0x0103: (F.TYPE_SHORT, 6),  # JPEG compression
                    0x0111: (F.TYPE_LONG, 0),  # patched below
                    0x0117: (F.TYPE_LONG, len(preview)),
                },
                trailing=preview,
            )
        )
        # The strip offset must point at the appended JPEG, which only exists
        # once the directory has been laid out.
        raw = bytearray(path.read_bytes())
        offset = len(raw) - len(preview)
        index = raw.find(struct.pack("<HHI", 0x0111, F.TYPE_LONG, 1))
        struct.pack_into("<I", raw, index + 8, offset)
        path.write_bytes(bytes(raw))

    loaded = RawPreviewLoader().load(path, working_edge=256, thumbnail_edge=64)
    assert loaded.luma.size > 0


def test_rw2_magic_85_is_accepted(tmp_path):
    """The exact fault: magic 85 raised 'unexpected TIFF magic 85'."""
    path = F.write_rw2(tmp_path / "frame.rw2")
    with path.open("rb") as stream:
        TiffReader(stream)  # must not raise


def test_rw2_reports_the_frame_size_not_the_preview_size(tmp_path):
    """A 1920-wide preview inside a 4008-wide photograph is not its dimensions."""
    path = F.write_rw2(tmp_path / "frame.rw2")
    loaded = RawPreviewLoader().load(path, working_edge=256, thumbnail_edge=64)
    assert (loaded.original_width, loaded.original_height) == (4008, 3008)


def test_panasonic_strip_offset_sentinel_is_not_followed(tmp_path):
    """StripOffsets of all-ones is a sentinel; seeking to it reads garbage."""
    path = F.write_rw2(tmp_path / "frame.rw2")
    location, _ = find_largest_preview(path)
    assert location is not None
    assert location.offset < path.stat().st_size


def test_large_opaque_values_are_located_rather_than_read(tmp_path):
    """A 750 KB preview must not be hauled into the tag dictionary.

    Reading it costs a copy per photograph across every worker process, and the
    only thing anyone wants from it is where it starts.
    """
    preview = F.jpeg_bytes((1920, 1440))
    path = F.write_rw2(tmp_path / "frame.rw2", preview=preview)
    directories = read_directories(path)
    value = directories[0][0x002E]
    assert isinstance(value, ValueLocation)
    assert value.length == len(preview)


def test_panasonic_iso_is_read_from_its_private_tag(tmp_path):
    """RW2 carries no standard ISO tag; without the fallback it reports none."""
    path = F.write_rw2(tmp_path / "frame.rw2")
    _, directories = find_largest_preview(path)
    assert exif.extract(directories, {}).iso == 400


def test_raf_exif_comes_from_the_embedded_jpeg(tmp_path):
    """RAF has no TIFF block of its own -- its metadata is inside the JPEG."""
    path = F.write_raf(tmp_path / "frame.raf")
    _, directories = find_largest_preview(path)
    assert exif.extract(directories, {}).camera == "FUJIFILM X-T4"


def test_raf_header_offsets_are_absolute(tmp_path):
    """The JPEG sits at a declared offset; reading from zero yields nothing."""
    path = F.write_raf(tmp_path / "frame.raf")
    plan = containers.layout(path, "raf")
    assert plan.previews[0].offset == 148
    assert _decode(path).size == (1440, 960)


def test_raf_frame_size_comes_from_the_cfa_header(tmp_path):
    """The embedded JPEG is a downsized preview, not the photograph.

    A RAF has no TIFF directory stating the real dimensions; they are in the CFA
    header. Without reading it a 102MP GFX frame is reported as 12MP.
    """
    path = F.write_raf(tmp_path / "frame.raf")
    loaded = RawPreviewLoader().load(path, working_edge=256, thumbnail_edge=64)
    assert (loaded.original_width, loaded.original_height) == (6240, 4160)


def test_cr3_prefers_the_preview_over_the_thumbnail(tmp_path):
    """Canon ships both; picking THMB gives you a 160x120 postage stamp.

    Nothing errors when this is wrong -- the measurements come out plausible and
    meaningless, which is the worst way for it to be wrong.
    """
    path = F.write_cr3(tmp_path / "frame.cr3")
    assert _decode(path).size == (1620, 1080)


def test_cr3_padding_before_the_first_child_box_is_skipped(tmp_path):
    """Canon writes eight bytes between the preview UUID and PRVW.

    Read as a box header, that padding hides the preview entirely.
    """
    path = F.write_cr3(tmp_path / "frame.cr3")
    names = {block.length for block in containers.layout(path, "cr3").previews}
    assert len(names) == 2  # both THMB and PRVW were located


def test_cr3_box_walk_finds_metadata_and_preview(tmp_path):
    """CR3 is ISO base media, not TIFF; both wanted parts are nested in uuids."""
    path = F.write_cr3(tmp_path / "frame.cr3")
    _, directories = find_largest_preview(path)
    assert exif.extract(directories, {}).camera == "Canon EOS R5"
    assert frame_size(path, directories) == (8192, 5464)
    assert _decode(path).size == (1620, 1080)


def test_sniff_leaves_ordinary_tiff_alone(tmp_path):
    """The common case must cost sixteen bytes and no dispatch."""
    path = tmp_path / "frame.nef"
    path.write_bytes(F.build_tiff({0x010F: (F.TYPE_ASCII, "NIKON")}))
    assert containers.sniff(path) is None


@pytest.mark.parametrize("kind,head", [("raf", F.write_raf), ("cr3", F.write_cr3)])
def test_sniff_identifies_wrapped_containers(tmp_path, kind, head):
    assert containers.sniff(head(tmp_path / f"frame.{kind}")) == kind


def test_truncated_container_fails_with_a_stated_reason(tmp_path):
    """A short or corrupt file becomes a named error, never a crash or a hang."""
    path = tmp_path / "frame.cr3"
    path.write_bytes(b"\x00\x00\x00\x18ftypcrx ")  # a header and nothing after it
    with pytest.raises(UnreadableImage):
        RawPreviewLoader().load(path, working_edge=256, thumbnail_edge=64)


@pytest.mark.parametrize("suffix", sorted(RAW_SUFFIXES))
def test_an_empty_file_fails_cleanly(tmp_path, suffix):
    """Not hypothetical: a silently-failed download leaves exactly this.

    Every branch of the container dispatch has to survive being handed nothing,
    because a zero-byte file with a camera extension is a thing that happens.
    """
    path = tmp_path / f"frame{suffix}"
    path.write_bytes(b"")
    with pytest.raises(UnreadableImage):
        RawPreviewLoader().load(path, working_edge=256, thumbnail_edge=64)


def test_garbage_with_a_raw_suffix_is_a_named_failure(tmp_path):
    path = tmp_path / "frame.nef"
    path.write_bytes(b"not a camera file at all, just some bytes" * 10)
    with pytest.raises(UnreadableImage) as caught:
        build_registry().load(path, working_edge=256, thumbnail_edge=64)
    assert "not a readable raw container" in str(caught.value)


@pytest.mark.skipif(not REAL_RW2, reason="no real Panasonic RW2 available on this machine")
def test_real_panasonic_file_analyses_end_to_end():
    """The synthetic headers prove the arithmetic; this proves the camera."""
    loaded = RawPreviewLoader().load(REAL_RW2[0], working_edge=512, thumbnail_edge=64)
    assert loaded.original_width >= 4000
    capture = exif.extract(loaded.directories, {})
    assert capture.camera and capture.iso and capture.shutter_seconds


@pytest.mark.skipif(not REAL_RAF, reason="no real Fujifilm RAF available on this machine")
@pytest.mark.parametrize("index", range(3))
def test_real_fujifilm_files_analyse_end_to_end(index):
    if index >= len(REAL_RAF):
        pytest.skip("fewer samples than parametrised")
    path = REAL_RAF[index]
    loaded = RawPreviewLoader().load(path, working_edge=512, thumbnail_edge=64)
    capture = exif.extract(loaded.directories, {})
    assert capture.camera and capture.camera.upper().startswith("FUJIFILM")
    assert capture.lens and capture.iso and capture.shutter_seconds
    # The frame, not the preview: every Fujifilm body writes a downsized one.
    assert loaded.original_width > loaded.luma.shape[1]
    assert loaded.original_width >= 6000


@pytest.mark.skipif(not REAL_CR3, reason="no real Canon CR3 available on this machine")
@pytest.mark.parametrize("index", range(3))
def test_real_canon_files_analyse_end_to_end(index):
    if index >= len(REAL_CR3):
        pytest.skip("fewer samples than parametrised")
    path = REAL_CR3[index]
    location, directories = find_largest_preview(path)
    assert location is not None
    # 1620x1080, not the 160x120 thumbnail sitting beside it in the same file.
    assert Image.open(io.BytesIO(read_preview_bytes(path, location))).size == (1620, 1080)
    capture = exif.extract(directories, {})
    assert capture.camera and capture.camera.startswith("Canon")
    assert frame_size(path, directories)[0] >= 5000
