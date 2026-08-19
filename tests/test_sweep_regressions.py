"""One test per finding in the 2026-08-18 code sweep.

Kept together rather than scattered into the topic files, because what these
have in common is not a module -- it is that each was reproduced by running the
code and each would come back silently. A regression that reappears without a
test to name it is a regression nobody notices twice.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from photocull import exif
from photocull.compare import compare, format_comparison
from photocull.config import Config
from photocull.detect import AFPointDetector, AFScan, DetectionContext, ManualDetector
from photocull.errors import ConfigError
from photocull.loading import PlainImageLoader
from photocull.models import Box
from photocull.outputs import build_writers
from photocull.outputs.naming import indexed_names, mirrored_names
from photocull.pipeline import run

from .fixtures import jpeg_bytes

# A complete Exif sub-IFD, as a camera writes it. Every one of these lives at
# 0x8769, which is the whole point of finding 1.
FULL_SUB_IFD = {
    0x829A: (1, 500),               # ExposureTime
    0x829D: (28, 10),               # FNumber
    0x8827: 400,                    # ISO
    0x9003: "2026:08:18 10:11:12",  # DateTimeOriginal
    0x920A: (85, 1),                # FocalLength
    0xA434: "85mm f/1.8G",          # LensModel
}


def _write(path: Path, **kwargs) -> Path:
    path.write_bytes(jpeg_bytes(**kwargs))
    return path


# --------------------------------------------------------------------------
# 1. Plain images got almost no EXIF at all.


def test_a_jpeg_yields_more_than_its_camera_name(tmp_path):
    """getexif() returns IFD0 only, and IFD0 holds none of this."""
    path = _write(
        tmp_path / "a.jpg", camera=("NIKON CORPORATION", "NIKON D750"), sub_ifd=FULL_SUB_IFD
    )
    loaded = PlainImageLoader().load(path, 256, 64)
    capture = exif.extract(loaded.directories, loaded.exif)

    assert capture.camera == "NIKON D750"
    assert capture.lens == "85mm f/1.8G"
    assert capture.iso == 400
    assert capture.aperture == pytest.approx(2.8)
    assert capture.shutter_seconds == pytest.approx(0.002)
    assert capture.focal_length == pytest.approx(85.0)
    assert capture.timestamp == "2026:08:18 10:11:12"


def test_the_handholding_figure_is_reachable_from_a_jpeg(tmp_path):
    """It needs shutter and focal length, both of which live in the sub-IFD."""
    path = _write(tmp_path / "a.jpg", camera=("NIKON", "D750"), sub_ifd=FULL_SUB_IFD)
    loaded = PlainImageLoader().load(path, 256, 64)
    assert exif.extract(loaded.directories, loaded.exif).reciprocal_margin is not None


def test_time_gap_grouping_is_not_a_no_op_on_jpegs(tmp_path):
    """A documented knob that quietly does nothing on JPEGs is worse than none."""
    for index in range(2):
        _write(
            tmp_path / f"{index}.jpg",
            camera=("NIKON", "D750"),
            sub_ifd={**FULL_SUB_IFD, 0x9003: f"2026:08:18 10:{index * 30:02d}:00"},
        )
    config = replace(Config(), grouping=replace(Config().grouping, max_time_gap_seconds=60))
    reports = run(tmp_path, config, workers=1)
    assert all(r.capture.timestamp for r in reports)
    # Half an hour apart, so the gap constraint has something to act on.
    assert len({r.group_id for r in reports}) == 2


# --------------------------------------------------------------------------
# 2. XMP sidecars collided on duplicate filenames.


def test_two_folders_one_filename_keep_both_verdicts(tmp_path):
    for folder in ("a", "b"):
        (tmp_path / folder).mkdir()
        _write(tmp_path / folder / "DSC_0001.jpg", colour="grey" if folder == "a" else "navy")
    _write(tmp_path / "flat.jpg", colour="olive")

    config = replace(Config(), output=replace(Config().output, formats=("xmp",)))
    reports = run(tmp_path, config, workers=1)
    out = tmp_path / "out"
    out.mkdir()
    manifest = build_writers(("xmp",))[0].write(reports, out, config)

    on_disk = sorted(p.relative_to(out / "xmp").as_posix() for p in (out / "xmp").rglob("*.xmp"))
    assert on_disk == ["a/DSC_0001.jpg.xmp", "b/DSC_0001.jpg.xmp", "flat.jpg.xmp"]
    # The manifest used to claim three while the disk held two.
    assert f"wrote {len(on_disk)} sidecar(s)" in manifest.read_text(encoding="utf-8")


def test_a_sidecar_is_still_named_after_the_file_it_describes():
    """An index would resolve the collision and break the format."""
    names = mirrored_names(["/shoot/a/DSC_0001.NEF", "/shoot/b/DSC_0001.NEF"], ".xmp")
    assert names == ["a/DSC_0001.NEF.xmp", "b/DSC_0001.NEF.xmp"]


def test_mirrored_names_survive_paths_with_no_common_root():
    names = mirrored_names(["C:/one/x.jpg", "D:/two/x.jpg"], ".xmp")
    assert len(set(names)) == 2


def test_indexed_names_are_unique_even_when_the_filenames_are_not():
    assert indexed_names(["x.jpg", "x.jpg"], ".jpg") == ["00000-x.jpg", "00001-x.jpg"]


def test_autofocus_boxes_are_filed_by_path_not_basename(tmp_path):
    """One folder's focus point must not be applied to another folder's frame."""
    left, right = tmp_path / "a" / "DSC_0001.nef", tmp_path / "b" / "DSC_0001.nef"
    scan = AFScan(
        {
            str(left.resolve().as_posix()).lower(): Box(0.1, 0.1, 0.1, 0.1),
            str(right.resolve().as_posix()).lower(): Box(0.8, 0.8, 0.1, 0.1),
        }
    )
    detector = AFPointDetector(tmp_path, preloaded=scan)
    luma = np.zeros((8, 8), dtype=np.float32)
    assert detector.detect(DetectionContext(luma=luma, path=str(left))).box.x == pytest.approx(0.1)
    assert detector.detect(DetectionContext(luma=luma, path=str(right))).box.x == pytest.approx(0.8)


def test_a_manual_box_can_name_its_folder_when_the_filename_is_not_enough(tmp_path):
    sidecar = tmp_path / "subjects.json"
    sidecar.write_text(
        json.dumps(
            {
                "DSC_0001.jpg": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1},
                "b/DSC_0001.jpg": {"x": 0.7, "y": 0.7, "w": 0.1, "h": 0.1},
            }
        ),
        encoding="utf-8",
    )
    detector = ManualDetector(sidecar)
    luma = np.zeros((8, 8), dtype=np.float32)
    # The longest match wins, so adding the specific key leaves the general one
    # working for every other folder.
    assert detector.detect(
        DetectionContext(luma=luma, path=str(tmp_path / "b" / "DSC_0001.jpg"))
    ).box.x == pytest.approx(0.7)
    assert detector.detect(
        DetectionContext(luma=luma, path=str(tmp_path / "a" / "DSC_0001.jpg"))
    ).box.x == pytest.approx(0.1)


# --------------------------------------------------------------------------
# 3. A config typo became a BrokenProcessPool at the default worker count.


@pytest.mark.parametrize(
    "section, message",
    [
        ({"subject": {"zone": "nowhere"}}, "zone"),
        ({"subject": {"detectors": ["saliency", "nosuch"]}}, "detector"),
    ],
)
def test_a_bad_subject_value_is_a_config_error(section, message):
    with pytest.raises(ConfigError) as caught:
        Config.from_dict(section)
    assert message in str(caught.value)


def test_a_bad_zone_fails_the_same_way_at_every_worker_count(tmp_path):
    """It used to be a sentence at -j1 and a BrokenProcessPool at -j2."""
    _write(tmp_path / "a.jpg")
    config = Config()
    # Past the config layer on purpose: this is the pipeline's own guard, which
    # has to cover the construction errors a future detector will invent.
    broken = replace(config, subject=replace(config.subject, detectors=("nosuch",)))
    for workers in (1, 2):
        with pytest.raises(ConfigError):
            run(tmp_path, broken, workers=workers)


# --------------------------------------------------------------------------
# 4. Dimensions were transposed for rotated frames, and so was the AF box.


def test_a_rotated_frame_reports_the_dimensions_it_was_measured_at(tmp_path):
    path = _write(tmp_path / "portrait.jpg", size=(400, 200), orientation=6)
    loaded = PlainImageLoader().load(path, 256, 64)

    height, width = loaded.luma.shape
    assert loaded.orientation == 6
    # Reported landscape while the analysis ran on a portrait frame.
    assert loaded.original_width < loaded.original_height
    assert loaded.original_width / loaded.original_height == pytest.approx(width / height, rel=0.05)


def test_an_unrotated_frame_is_left_alone(tmp_path):
    path = _write(tmp_path / "landscape.jpg", size=(400, 200))
    loaded = PlainImageLoader().load(path, 256, 64)
    assert (loaded.original_width, loaded.original_height) == (400, 200)


@pytest.mark.parametrize(
    "orientation, expected",
    [
        (1, (0.0, 0.0)),
        (6, (0.8, 0.0)),  # quarter turn clockwise: stored top-left -> viewed top-right
        (8, (0.0, 0.8)),
        (3, (0.8, 0.8)),
    ],
)
def test_a_sensor_space_box_turns_with_the_image(orientation, expected):
    turned = Box(0.0, 0.0, 0.2, 0.2).reoriented(orientation)
    assert (turned.x, turned.y) == pytest.approx(expected)
    assert (turned.w, turned.h) == pytest.approx((0.2, 0.2))


def test_a_rotated_af_area_swaps_its_normalised_sides():
    """The case a real portrait frame produces, worked end to end.

    A body records the AF area in sensor pixels -- 984x816 on a 6016x4016
    sensor. Normalising divides the two axes by different denominators, so the
    box is 0.1636 wide by 0.2032 tall; a quarter turn has to swap exactly those
    two and move the corner to match. Checked against the arithmetic rather than
    against a remembered constant, so the test says why the numbers are what
    they are.
    """
    from photocull.detect.afpoint import AFPointDetector

    record = {
        "AFAreaXPosition": 3000,
        "AFAreaYPosition": 2000,
        "AFAreaWidth": 984,
        "AFAreaHeight": 816,
        "ImageWidth": 6016,
        "ImageHeight": 4016,
    }
    box = AFPointDetector(Path("."))._box_from_record(record)
    assert (box.w, box.h) == pytest.approx((984 / 6016, 816 / 4016))

    turned = box.reoriented(6)
    assert (turned.w, turned.h) == pytest.approx((816 / 4016, 984 / 6016))

    # The box centre follows the same quarter turn the pixels did: orientation 6
    # maps a stored point (x, y) to (1 - y, x). Note this AF point is near the
    # frame centre but not on it -- 3000 of 6016 is half a pixel off -- so
    # asserting "the centre stays the centre" would pass for the wrong reason.
    centre = (3000 / 6016, 2000 / 4016)
    assert (turned.x + turned.w / 2, turned.y + turned.h / 2) == pytest.approx(
        (1 - centre[1], centre[0]), abs=1e-6
    )


def test_the_autofocus_detector_turns_its_box(tmp_path):
    """The box is sensor-space; the luma it will be measured against is not."""
    path = tmp_path / "portrait.nef"
    scan = AFScan({str(path.resolve().as_posix()).lower(): Box(0.0, 0.0, 0.2, 0.2)})
    detector = AFPointDetector(tmp_path, preloaded=scan)
    context = DetectionContext(
        luma=np.zeros((8, 8), dtype=np.float32), path=str(path), orientation=6
    )
    assert detector.detect(context).box.x == pytest.approx(0.8)


# --------------------------------------------------------------------------
# 5. The contact sheet interpolated untrusted strings into innerHTML.


def test_every_text_bearing_interpolation_is_escaped():
    from photocull.outputs.contactsheet import _PAGE

    script = _PAGE.split("<script>")[1]
    assert "const esc =" in script
    for field in (
        "${esc(photo.filename)}",
        "${esc(photo.detection.source)}",
        "${esc(photo.error)}",
        "${esc(photo.blur.likely_cause)}",
    ):
        assert field in script, field
    # The bare forms are what made a filename with a tag in it executable.
    assert "${photo.filename}" not in script
    assert "${photo.error}" not in script


# --------------------------------------------------------------------------
# 8. include_suffixes raised the wrong exception type.


@pytest.mark.parametrize("value", [[5], 7, [""], [None]])
def test_a_malformed_include_suffixes_names_the_key(value):
    with pytest.raises(ConfigError) as caught:
        Config.from_dict({"input": {"include_suffixes": value}})
    assert "include_suffixes" in str(caught.value)


def test_a_section_that_is_not_a_table_names_the_section():
    with pytest.raises(ConfigError) as caught:
        Config.from_dict({"input": 5})
    assert "[input]" in str(caught.value)


def test_suffixes_are_normalised_with_or_without_the_dot():
    config = Config.from_dict({"input": {"include_suffixes": ["NEF", " .jpg "]}})
    assert config.input.include_suffixes == (".nef", ".jpg")


# --------------------------------------------------------------------------
# 9. exposure.measure's percentile was unreachable from config.


def test_the_exposure_percentile_reaches_the_measurement(tmp_path):
    _write(tmp_path / "a.jpg")
    base = Config()
    wide = run(tmp_path, replace(base, exposure=replace(base.exposure, percentile=0.0)), workers=1)
    narrow = run(tmp_path, replace(base, exposure=replace(base.exposure, percentile=20.0)), workers=1)
    # Sampling further in can only narrow the range, never widen it.
    assert narrow[0].exposure.dynamic_range < wide[0].exposure.dynamic_range


def test_a_bad_exposure_percentile_is_a_config_error():
    with pytest.raises(ConfigError):
        Config.from_dict({"exposure": {"percentile": 60}})


# --------------------------------------------------------------------------
# Missing features: explain applies the rules, and two runs can be compared.


def test_explain_prints_the_verdict(tmp_path, capsys):
    from photocull.cli import main

    _write(tmp_path / "a.jpg")
    assert main(["explain", str(tmp_path / "a.jpg")]) == 0
    printed = capsys.readouterr().out
    assert "verdict" in printed
    assert "rating" in printed


def test_compare_reports_what_crossed_the_keeper_line():
    before = {"photos": [{"path": "/s/a.jpg", "filename": "a.jpg", "rating": 3, "reasons": []}]}
    after = {"photos": [{"path": "/s/a.jpg", "filename": "a.jpg", "rating": 5, "reasons": ["looser"]}]}

    result = compare(before, after)
    assert result["changed"] == 1
    assert result["movement"] == {"middle -> keeper": 1}
    assert result["buckets"]["after"]["keeper"] == 1
    assert "a.jpg" in format_comparison(result)


def test_compare_says_nothing_moved_when_nothing_moved():
    same = {"photos": [{"path": "/s/a.jpg", "filename": "a.jpg", "rating": 3}]}
    assert "no frame changed its rating" in format_comparison(compare(same, same))


def test_compare_notices_frames_present_in_only_one_run():
    before = {"photos": [{"path": "/s/a.jpg", "filename": "a.jpg", "rating": 3}]}
    after = {"photos": [{"path": "/s/b.jpg", "filename": "b.jpg", "rating": 3}]}
    result = compare(before, after)
    assert result["only_in_before"] == ["/s/a.jpg"]
    assert result["only_in_after"] == ["/s/b.jpg"]
