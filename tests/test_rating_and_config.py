"""Tests for the rule engine, configuration validation and grouping."""

from __future__ import annotations

import numpy as np
import pytest

from photocull.config import Config, RatingRule
from photocull.errors import ConfigError, ExpressionError
from photocull.grouping import difference_hash, group_by_similarity, hamming_distance
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
from photocull.rating import Expression, Rater

NAMES = ["max_local_acutance", "subject_acutance", "highlight_clipped", "subject_found", "group_size"]


def report(**overrides) -> PhotoReport:
    """A report with plausible values, overridable per test."""
    sharp = SharpnessMetrics(
        global_acutance=overrides.pop("global_acutance", 20.0),
        max_local_acutance=overrides.pop("max_local_acutance", 50.0),
        median_acutance=15.0,
        sharp_fraction=0.3,
        focus_x=0.5,
        focus_y=0.5,
        subject_acutance=overrides.pop("subject_acutance", 55.0),
        background_acutance=overrides.pop("background_acutance", 10.0),
        subject_background_ratio=overrides.pop("subject_background_ratio", 5.5),
    )
    detection = Detection(
        Box(0.4, 0.4, 0.2, 0.2) if overrides.pop("subject_found", True) else None,
        overrides.pop("source", "face+eyes"),
        Confidence.HIGH,
    )
    return PhotoReport(
        path="/tmp/x.nef",
        filename="x.nef",
        width=6000,
        height=4000,
        sharpness=sharp,
        exposure=ExposureMetrics(
            highlight_clipped=overrides.pop("highlight_clipped", 0.001),
            shadow_clipped=0.0,
            dynamic_range=0.9,
            mean_luma=0.45,
            contrast=0.2,
        ),
        blur=BlurMetrics(0.1, 0.0, "sharp"),
        detection=detection,
        capture=CaptureInfo(**overrides.pop("capture", {})),
        **overrides,
    )


class TestExpression:
    def test_unknown_name_is_rejected_at_compile_time(self) -> None:
        with pytest.raises(ExpressionError, match="unknown measurement"):
            Expression("subject_sharpnes > 3", NAMES)

    def test_typo_gets_a_suggestion(self) -> None:
        with pytest.raises(ExpressionError, match="did you mean 'subject_acutance'"):
            Expression("subject_acutence > 3", NAMES)

    def test_attribute_access_is_refused(self) -> None:
        with pytest.raises(ExpressionError):
            Expression("subject_acutance.__class__", NAMES)

    def test_arbitrary_calls_are_refused(self) -> None:
        with pytest.raises(ExpressionError, match="may only call"):
            Expression("open('/etc/passwd')", NAMES)

    def test_missing_measurement_compares_false_instead_of_raising(self) -> None:
        expression = Expression("subject_acutance > 10", NAMES)
        assert expression.matches({"subject_acutance": 50.0}) is True
        assert expression.matches({"subject_acutance": None}) is False

    def test_identity_checks_still_see_none(self) -> None:
        assert Expression("subject_acutance is None", NAMES).matches({"subject_acutance": None})

    def test_arithmetic_and_functions(self) -> None:
        expression = Expression("max(subject_acutance, 10) / 2 >= 25", NAMES)
        assert expression.matches({"subject_acutance": 50.0})

    def test_chained_comparison(self) -> None:
        assert Expression("10 < max_local_acutance < 100", NAMES).matches(
            {"max_local_acutance": 50.0}
        )


class TestRater:
    def test_first_matching_rule_wins(self) -> None:
        rules = [
            RatingRule(when="max_local_acutance < 12", stars=1, reason="nothing sharp"),
            RatingRule(when="True", stars=3, reason="fallback"),
        ]
        rater = Rater(rules, sorted(report().flat_metrics()))
        assert rater.apply(report(max_local_acutance=5.0)).rating == 1
        assert rater.apply(report(max_local_acutance=80.0)).rating == 3

    def test_the_reason_is_recorded(self) -> None:
        rater = Rater([RatingRule(when="True", stars=4, reason="because")], sorted(report().flat_metrics()))
        assert "because" in rater.apply(report()).reasons

    def test_no_matching_rule_leaves_the_rating_unset(self) -> None:
        rater = Rater([RatingRule(when="max_local_acutance > 1000", stars=5)], sorted(report().flat_metrics()))
        assert rater.apply(report()).rating is None


class TestConfig:
    def test_unknown_key_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="unknown key"):
            Config.from_dict({"input": {"working_edg": 512}})

    def test_unknown_section_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="unknown key"):
            Config.from_dict({"sharpnes": {}})

    def test_out_of_range_values_are_rejected(self) -> None:
        with pytest.raises(ConfigError, match="between 0 and 1"):
            Config.from_dict({"sharpness": {"sharp_fraction_threshold": 4}})
        with pytest.raises(ConfigError, match="positive integer"):
            Config.from_dict({"input": {"working_edge": 0}})

    def test_defaults_survive_a_partial_file(self) -> None:
        config = Config.from_dict({"input": {"working_edge": 512}})
        assert config.input.working_edge == 512
        assert config.input.thumbnail_edge == 320
        assert config.rating.rules  # defaults are not wiped out by an empty section

    def test_suffixes_are_normalised(self) -> None:
        config = Config.from_dict({"input": {"include_suffixes": ["NEF", ".Jpg"]}})
        assert config.input.include_suffixes == (".nef", ".jpg")

    def test_the_shipped_default_config_parses(self) -> None:
        """The file `photocull init` writes must actually load."""
        import tomllib

        from photocull.default_config import DEFAULT_CONFIG_TOML

        Config.from_dict(tomllib.loads(DEFAULT_CONFIG_TOML))

    def test_shipped_rules_compile_against_real_measurements(self) -> None:
        import tomllib

        from photocull.default_config import DEFAULT_CONFIG_TOML

        config = Config.from_dict(tomllib.loads(DEFAULT_CONFIG_TOML))
        Rater(config.rating.rules, sorted(report().flat_metrics()))


class TestGrouping:
    def test_identical_images_hash_identically(self) -> None:
        image = np.random.default_rng(1).random((128, 128)).astype(np.float32)
        assert hamming_distance(difference_hash(image), difference_hash(image)) == 0

    def test_a_brightened_copy_still_matches(self) -> None:
        """A difference hash keeps gradient signs, so exposure shifts survive."""
        image = np.random.default_rng(2).random((128, 128)).astype(np.float32) * 0.6
        assert hamming_distance(difference_hash(image), difference_hash(image + 0.2)) <= 2

    def test_different_images_do_not_group(self) -> None:
        rng = np.random.default_rng(3)
        first, second = rng.random((128, 128)), rng.random((128, 128))
        hashes = [difference_hash(first.astype(np.float32)), difference_hash(second.astype(np.float32))]
        assert group_by_similarity(hashes, max_distance=8) == [0, 1]

    def test_similar_images_group_together(self) -> None:
        image = np.random.default_rng(4).random((128, 128)).astype(np.float32)
        hashes = [difference_hash(image), difference_hash(image + 0.01), difference_hash(image)]
        assert len(set(group_by_similarity(hashes, max_distance=8))) == 1

    def test_grouping_is_transitive(self) -> None:
        """A slow pan makes a chain where neighbours match and the ends do not;
        that is still one decision to make."""
        rng = np.random.default_rng(5)
        base = rng.random((128, 128)).astype(np.float32)
        frames = [base + rng.normal(0, 0.02, base.shape).astype(np.float32) for _ in range(4)]
        ids = group_by_similarity([difference_hash(f) for f in frames], max_distance=20)
        assert len(set(ids)) == 1

    def test_empty_input(self) -> None:
        assert group_by_similarity([], max_distance=10) == []
