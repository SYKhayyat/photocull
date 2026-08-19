"""The layer that takes file paths: discovery, the pool, and every writer.

These modules were the untested half of the project, and the split was not
random -- the tested modules took NumPy arrays and dictionaries, and these take
paths. That is also why the container reader had a fault in it for three whole
formats: nothing here ever opened a file.

Generated JPEGs rather than committed fixtures. A folder of them is a few
kilobytes, builds in milliseconds, and can be made to contain a deliberate
near-duplicate pair, which is the thing grouping has to get right.
"""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path

import pytest

from photocull.config import DEFAULT_RULES, Config, RatingRule
from photocull.default_config import DEFAULT_CONFIG_TOML
from photocull.detect import AFPointDetector, AFScan
from photocull.errors import ExpressionError
from photocull.models import (
    BlurMetrics,
    Box,
    CaptureInfo,
    Confidence,
    Detection,
    ExposureMetrics,
    PhotoReport,
    SharpnessMetrics,
)
from photocull.outputs import build_writers
from photocull.outputs.contactsheet import ContactSheetWriter
from photocull.outputs.machine import (
    CsvWriter,
    JsonWriter,
    KeeperListWriter,
    RejectListWriter,
    XmpWriter,
)
from photocull.pipeline import _scan_autofocus, discover, run, summarise
from photocull.rating import Rater
from photocull.rating import validate as validate_rules

from . import fixtures as F


@pytest.fixture
def shoot(tmp_path):
    """A small folder with a deliberate near-duplicate pair inside it."""
    folder = tmp_path / "shoot"
    folder.mkdir()
    for index, colour in enumerate(["red", "green", "blue", "orange"]):
        (folder / f"frame{index}.jpg").write_bytes(F.jpeg_bytes((480, 320), colour=colour))
    # Two frames of the same scene: what grouping exists to notice.
    (folder / "burst-a.jpg").write_bytes(F.jpeg_bytes((480, 320), colour="purple"))
    (folder / "burst-b.jpg").write_bytes(F.jpeg_bytes((480, 320), colour="purple"))
    return folder


@pytest.fixture
def reports(shoot):
    return run(shoot, Config(), workers=1)


# --------------------------------------------------------------------------
# Discovery and the run itself


def test_discover_finds_images_and_ignores_everything_else(shoot):
    (shoot / "notes.txt").write_text("not a photograph")
    (shoot / "sub").mkdir()
    (shoot / "sub" / "nested.jpg").write_bytes(F.jpeg_bytes((100, 100)))

    found = discover(shoot, Config())
    assert all(path.suffix.lower() == ".jpg" for path in found)
    assert (shoot / "sub" / "nested.jpg") in found  # recursive is the default
    assert found == sorted(found)  # stable output, not filesystem order


def test_discover_can_be_told_not_to_recurse(shoot):
    (shoot / "sub").mkdir()
    (shoot / "sub" / "nested.jpg").write_bytes(F.jpeg_bytes((100, 100)))
    config = Config()
    config = replace(config, input=replace(config.input, recursive=False))
    assert (shoot / "sub" / "nested.jpg") not in discover(shoot, config)


def test_run_produces_one_complete_report_per_file(shoot, reports):
    assert len(reports) == len(discover(shoot, Config()))
    assert all(report.error is None for report in reports)
    assert all(report.rating is not None for report in reports)
    assert all(report.width > 0 and report.height > 0 for report in reports)


def test_run_groups_near_duplicates_and_ranks_within_the_group(reports):
    burst = [r for r in reports if r.filename.startswith("burst-")]
    assert len(burst) == 2
    assert burst[0].group_id is not None
    assert burst[0].group_id == burst[1].group_id
    assert {r.group_rank for r in burst} == {0, 1}
    assert sum(r.is_group_best for r in burst) == 1


def test_run_over_a_worker_pool_agrees_with_the_single_process_path(shoot, reports):
    """The pool is an optimisation, so it must not change a single number."""
    pooled = run(shoot, Config(), workers=2)
    assert [r.filename for r in pooled] == [r.filename for r in reports]
    assert [r.rating for r in pooled] == [r.rating for r in reports]
    assert [round(r.sharpness.max_local_acutance, 6) for r in pooled] == [
        round(r.sharpness.max_local_acutance, 6) for r in reports
    ]


def test_an_unreadable_file_becomes_a_row_not_an_exception(shoot):
    (shoot / "broken.jpg").write_bytes(b"this is not a JPEG")
    results = run(shoot, Config(), workers=1)
    broken = next(r for r in results if r.filename == "broken.jpg")
    assert broken.error
    assert len(results) > 1  # the rest of the folder still analysed
    assert summarise(results)["failed"] == 1


def test_run_on_an_empty_folder_is_empty_not_an_error(tmp_path):
    assert run(tmp_path, Config(), workers=1) == []


# --------------------------------------------------------------------------
# Rule validation happens before the run, which is the whole point of the module


def test_a_typo_in_a_rule_raises_before_any_file_is_opened():
    """The docstring's own example: one transposed letter in a measurement name.

    Compiling in ``run()`` meant this surfaced after every photograph had been
    analysed. Validation has to be reachable without a report in hand.
    """
    with pytest.raises(ExpressionError) as caught:
        validate_rules((RatingRule(when="subject_acutence > 5"),), "subject_or_max_acutance")
    message = str(caught.value)
    assert "unknown measurement" in message
    assert "subject_acutance" in message  # and it suggests the correction


def test_a_bad_rank_expression_is_caught_too():
    with pytest.raises(ExpressionError):
        validate_rules(DEFAULT_RULES, "nonsense_metric")


def test_the_shipped_defaults_validate():
    validate_rules(DEFAULT_RULES, "subject_or_max_acutance")


def test_declared_metric_names_match_what_a_report_actually_produces(reports):
    """If these drift, every config error message starts lying."""
    assert set(reports[0].flat_metrics()) == set(PhotoReport.flat_metric_names())


def test_shipped_config_file_matches_the_built_in_defaults():
    """``photocull init`` writes the defaults out; it must write the real ones."""
    import tomllib

    parsed = Config.from_dict(tomllib.loads(DEFAULT_CONFIG_TOML))
    assert len(parsed.rating.rules) == len(DEFAULT_RULES)
    for from_file, built_in in zip(parsed.rating.rules, DEFAULT_RULES):
        assert (from_file.when, from_file.stars, from_file.label) == (
            built_in.when,
            built_in.stars,
            built_in.label,
        )


# --------------------------------------------------------------------------
# Rating: a measurement that is reported must not be able to reject on its own


def _report_with(**overrides) -> PhotoReport:
    sharp = SharpnessMetrics(
        global_acutance=20.0,
        max_local_acutance=overrides.pop("max_local_acutance", 40.0),
        median_acutance=15.0,
        sharp_fraction=0.2,
        focus_x=0.5,
        focus_y=0.5,
        subject_acutance=30.0,
        background_acutance=10.0,
        subject_background_ratio=overrides.pop("ratio", 3.5),
    )
    exposure = ExposureMetrics(
        highlight_clipped=overrides.pop("highlight_clipped", 0.0),
        shadow_clipped=0.0,
        dynamic_range=1.0,
        mean_luma=0.5,
        contrast=0.2,
    )
    return PhotoReport(
        path="/tmp/a.nef",
        filename="a.nef",
        width=6000,
        height=4000,
        sharpness=sharp,
        exposure=exposure,
        blur=BlurMetrics(0.1, 0.0, "sharp"),
        detection=Detection(Box(0.4, 0.4, 0.2, 0.2), "face", Confidence.HIGH),
        capture=CaptureInfo(),
        **overrides,
    )


def test_clipped_highlights_label_a_frame_without_rejecting_it():
    """The motivating case: a backlit portrait with a blown rim light.

    ``metrics/exposure.py`` says of clipping "Reported, never scored ... that is
    the photographer's call, not this tool's". Under the old ladder that rule
    fired third, scored 2 stars, and wrote the frame to rejects.txt.
    """
    rated = Rater(DEFAULT_RULES).apply(_report_with(highlight_clipped=0.30, ratio=3.5))
    assert rated.rating == 5
    assert rated.label == "green"  # the verdict owns the colour
    assert any("clipped" in reason for reason in rated.reasons)  # the flag survives


def test_a_unique_well_focused_frame_can_still_be_a_keeper():
    """No group to win, and no absolute acutance bar to clear."""
    rated = Rater(DEFAULT_RULES).apply(
        _report_with(ratio=2.0, max_local_acutance=14.0, group_size=1)
    )
    assert rated.rating >= 4


def test_the_best_frame_of_a_group_outranks_its_near_duplicates():
    rater = Rater(DEFAULT_RULES)
    best = rater.apply(_report_with(ratio=2.0, group_size=3, group_rank=0))
    loser = rater.apply(_report_with(ratio=2.0, group_size=3, group_rank=2))
    assert best.rating > loser.rating


def test_focus_landing_behind_the_subject_is_still_a_reject():
    rated = Rater(DEFAULT_RULES).apply(_report_with(ratio=0.5))
    assert rated.rating == 2
    assert rated.label == "yellow"


def test_an_annotation_rule_does_not_end_the_ladder():
    rules = (
        RatingRule(when="True", label="green", reason="noted"),
        RatingRule(when="True", stars=3, reason="decided"),
    )
    rated = Rater(rules).apply(_report_with())
    assert rated.rating == 3
    assert rated.label == "green"
    assert list(rated.reasons)[-2:] == ["noted", "decided"]


# --------------------------------------------------------------------------
# Writers: every format is a projection, so every projection gets a round trip


def test_json_writer_round_trips(reports, tmp_path):
    target = JsonWriter().write(reports, tmp_path, Config())
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert len(payload["photos"]) == len(reports)
    assert payload["summary"]["analysed"] == len(reports)
    assert payload["photos"][0]["filename"] == reports[0].filename


def test_csv_writer_has_a_column_for_every_measurement(reports, tmp_path):
    target = CsvWriter().write(reports, tmp_path, Config())
    rows = list(csv.DictReader(target.read_text(encoding="utf-8").splitlines()))
    assert len(rows) == len(reports)
    # The promise the flat namespace exists to keep: config names are CSV columns.
    assert set(PhotoReport.flat_metric_names()) <= set(rows[0])


def test_csv_writer_handles_an_empty_run(tmp_path):
    assert CsvWriter().write([], tmp_path, Config()).read_text(encoding="utf-8") == ""


def test_keeper_and_reject_lists_are_plain_paths(reports, tmp_path):
    keepers = KeeperListWriter().write(reports, tmp_path, Config())
    rejects = RejectListWriter().write(reports, tmp_path, Config())

    kept = [line for line in keepers.read_text(encoding="utf-8").splitlines() if line]
    assert all(Path(line).exists() for line in kept)
    for line in rejects.read_text(encoding="utf-8").splitlines():
        if line:
            path, _, reason = line.partition("\t")
            assert Path(path).exists() and reason


def test_xmp_writer_never_overwrites_an_existing_sidecar(reports, tmp_path, shoot):
    config = Config()
    config = replace(config, output=replace(config.output, write_xmp_next_to_originals=True))

    victim = Path(reports[0].path).with_suffix(Path(reports[0].path).suffix + ".xmp")
    victim.write_text("real develop settings, not to be destroyed", encoding="utf-8")

    manifest = XmpWriter().write(reports, tmp_path, config)
    assert victim.read_text(encoding="utf-8").startswith("real develop settings")
    assert "skipped 1 existing sidecar" in manifest.read_text(encoding="utf-8")


def test_xmp_writer_emits_the_rating_it_was_given(reports, tmp_path):
    XmpWriter().write(reports, tmp_path, Config())
    written = sorted((tmp_path / "xmp").glob("*.xmp"))
    assert written
    text = written[0].read_text(encoding="utf-8")
    assert 'xmp:Rating="' in text and "<x:xmpmeta" in text


def test_contact_sheet_is_one_self_contained_file_by_default(reports, tmp_path):
    page = ContactSheetWriter().write(reports, tmp_path, Config())
    text = page.read_text(encoding="utf-8")
    assert "data:image/jpeg;base64," in text
    assert not (tmp_path / "thumbs").exists()


def test_contact_sheet_spills_thumbnails_above_the_threshold(reports, tmp_path):
    config = Config()
    config = replace(config, output=replace(config.output, self_contained_max_frames=2))

    page = ContactSheetWriter().write(reports, tmp_path, config)
    text = page.read_text(encoding="utf-8")

    thumbs = sorted((tmp_path / "thumbs").glob("*.jpg"))
    assert len(thumbs) == len(reports)
    assert "data:image/jpeg;base64," not in text
    assert "thumbs/" in text
    assert "keep the folder together" in text  # and the page says so


def test_spilled_thumbnail_names_cannot_collide(tmp_path):
    """Two folders in one shoot may each hold a DSC_0001.NEF."""
    config = Config()
    config = replace(config, output=replace(config.output, self_contained_max_frames=0))

    first = tmp_path / "a"
    second = tmp_path / "b"
    for folder in (first, second):
        folder.mkdir()
        (folder / "DSC_0001.jpg").write_bytes(F.jpeg_bytes((200, 150)))

    out = tmp_path / "report"
    out.mkdir()
    ContactSheetWriter().write(run(tmp_path, Config(), workers=1), out, config)
    assert len(list((out / "thumbs").glob("*.jpg"))) == 2


def test_the_script_payload_cannot_break_out_of_its_tag(reports, tmp_path):
    page = ContactSheetWriter().write(reports, tmp_path, Config())
    body = page.read_text(encoding="utf-8")
    # The payload is a single line, and it is the only part that is user data.
    payload = body.split("const DATA =", 1)[1].splitlines()[0]
    assert "</" not in payload


def test_every_named_writer_can_be_built_and_run(reports, tmp_path):
    for writer in build_writers(("json", "csv", "html", "keepers", "rejects", "xmp")):
        assert writer.write(reports, tmp_path, Config()).exists()


# --------------------------------------------------------------------------
# The autofocus scan happens once, in the parent


def test_autofocus_is_not_scanned_when_the_chain_does_not_ask_for_it(tmp_path):
    config = Config()
    config = replace(config, subject=replace(config.subject, detectors=("face", "saliency")))
    assert _scan_autofocus(tmp_path, config) is None


def test_a_preloaded_detector_never_shells_out(tmp_path, monkeypatch):
    """The point of hoisting: workers must not each run their own exiftool."""
    import photocull.detect.afpoint as module

    def explode(*args, **kwargs):
        raise AssertionError("exiftool was invoked in a worker")

    monkeypatch.setattr(module.subprocess, "run", explode)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/exiftool")

    detector = module.AFPointDetector(tmp_path, preloaded=AFScan({}, "already scanned"))
    usable, reason = detector.available()
    assert not usable and reason == "already scanned"


def test_a_preloaded_scan_is_used_for_detection(tmp_path, monkeypatch):
    from photocull.detect.base import DetectionContext
    from photocull.models import Box

    import photocull.detect.afpoint as module

    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("scanned"))
    )
    box = Box(0.4, 0.4, 0.2, 0.2)
    detector = AFPointDetector(tmp_path, preloaded=AFScan({"a.nef": box}))

    import numpy as np

    context = DetectionContext(
        luma=np.zeros((10, 10), dtype=np.float32),
        path=str(tmp_path / "a.nef"),
    )
    detection = detector.detect(context)
    assert detection.found and detection.source == "af-point"


def test_the_scan_crosses_a_process_boundary(tmp_path):
    """It rides in through the pool initialiser, so it has to pickle."""
    import pickle

    from photocull.models import Box

    scan = AFScan({"a.nef": Box(0.1, 0.2, 0.3, 0.4)}, "reason")
    restored = pickle.loads(pickle.dumps(scan))
    assert restored.boxes["a.nef"] == scan.boxes["a.nef"]
    assert restored.reason == "reason"
