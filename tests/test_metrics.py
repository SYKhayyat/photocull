"""Tests for the measurement passes.

Synthetic images with known properties, because the only way to know a sharpness
metric works is to hand it something whose sharpness you already know. Real
photographs are useful for calibration and useless as assertions.
"""

from __future__ import annotations

import numpy as np
import pytest

from photocull.metrics import blur, exposure, sharpness
from photocull.models import Box


def checkerboard(size: int = 256, cell: int = 8) -> np.ndarray:
    """Maximum-contrast high-frequency detail: the sharpest image possible."""
    rows = (np.arange(size) // cell) % 2
    return (rows[:, None] ^ rows[None, :]).astype(np.float32)


def blurred(image: np.ndarray, passes: int = 4) -> np.ndarray:
    """Repeated 3x3 box blur, standing in for defocus.

    Padded each pass because convolve3x3 uses valid borders; without this the
    array shrinks by two pixels per pass and no longer matches its source.
    """
    kernel = np.ones((3, 3), dtype=np.float32) / 9.0
    out = image
    for _ in range(passes):
        out = sharpness.convolve3x3(np.pad(out, 1, mode="edge"), kernel)
    return out


def horizontally_smeared(image: np.ndarray, cell: int) -> np.ndarray:
    """Average along one axis only, standing in for camera shake.

    The window must span a whole number of periods of the pattern. Averaging 18
    samples of a 12-pixel period leaves half a period of structure standing, and
    the frame stays legibly detailed in the direction it was supposed to lose --
    which is a broken fixture, not a broken measurement.
    """
    window = 2 * cell
    padded = np.pad(image, ((0, 0), (window, window)), mode="wrap")
    stack = [padded[:, i : i + image.shape[1]] for i in range(window)]
    return np.mean(stack, axis=0).astype(np.float32)


class TestSharpness:
    def test_blurring_reduces_acutance(self) -> None:
        sharp = sharpness.build_map(checkerboard())
        soft = sharpness.build_map(blurred(checkerboard()))
        assert soft.max < sharp.max / 2

    def test_flat_image_has_no_acutance(self) -> None:
        flat = np.full((128, 128), 0.5, dtype=np.float32)
        assert sharpness.build_map(flat).max == pytest.approx(0.0, abs=1e-6)

    def test_focus_point_finds_the_sharp_region(self) -> None:
        # A soft frame with one crisp patch in a known corner.
        image = blurred(checkerboard(256, 4), passes=6)
        image[8:72, 8:72] = checkerboard(64, 4)
        x, y = sharpness.build_map(image).focus_point()
        assert x < 0.4 and y < 0.4

    def test_subject_ratio_survives_the_texture_confound(self) -> None:
        """The claim the whole tool rests on, stated as a test.

        Two frames whose subjects are equally sharp but whose backgrounds differ
        wildly in texture must produce similar subject/background ratios once the
        background is equally soft -- and a sharp subject on a soft background
        must always outscore the reverse.
        """
        sharp_subject = blurred(checkerboard(256, 4), passes=6)
        sharp_subject[96:160, 96:160] = checkerboard(64, 4)

        sharp_background = checkerboard(256, 4).copy()
        sharp_background[96:160, 96:160] = blurred(checkerboard(64, 4), passes=6)

        subject = Box(0.375, 0.375, 0.25, 0.25)
        good = sharpness.measure(sharpness.build_map(sharp_subject), subject)
        bad = sharpness.measure(sharpness.build_map(sharp_background), subject)

        assert good.subject_background_ratio > 1.0
        assert bad.subject_background_ratio < 1.0
        assert good.subject_background_ratio > bad.subject_background_ratio

    def test_no_subject_means_no_subject_metrics(self) -> None:
        metrics = sharpness.measure(sharpness.build_map(checkerboard()), None)
        assert metrics.subject_acutance is None
        assert metrics.background_acutance is None
        assert metrics.subject_background_ratio is None
        assert metrics.max_local_acutance > 0  # still meaningful without a subject

    def test_deep_and_shallow_depth_of_field_are_distinguished(self) -> None:
        everywhere = sharpness.build_map(checkerboard(256, 4))
        one_spot = blurred(checkerboard(256, 4), passes=6)
        one_spot[96:160, 96:160] = checkerboard(64, 4)
        assert everywhere.sharp_fraction(0.5) > sharpness.build_map(one_spot).sharp_fraction(0.5)

    def test_relative_acutance_does_not_depend_on_box_size(self) -> None:
        """Raw subject acutance is the peak over whichever tiles a box covers, so
        a big box collects a bigger number for free. That made a half-frame
        saliency box outscore an eye box on identical content. The relative
        figure divides by the frame's own peak and removes the dependence."""
        image = blurred(checkerboard(256, 4), passes=6)
        image[112:144, 112:144] = checkerboard(32, 4)  # one crisp patch, centred
        sharpness_map = sharpness.build_map(image)

        tight = sharpness.measure(sharpness_map, Box(0.44, 0.44, 0.12, 0.12))
        loose = sharpness.measure(sharpness_map, Box(0.25, 0.25, 0.50, 0.50))

        # Both boxes contain the sharpest region, so both are "the subject is the
        # sharpest thing here" -- regardless of how much slack the box has.
        assert tight.subject_relative_acutance == pytest.approx(1.0, abs=0.01)
        assert loose.subject_relative_acutance == pytest.approx(1.0, abs=0.01)

    def test_relative_acutance_falls_when_focus_is_elsewhere(self) -> None:
        image = blurred(checkerboard(256, 4), passes=6)
        image[16:48, 16:48] = checkerboard(32, 4)  # sharp corner, not the subject
        metrics = sharpness.measure(sharpness.build_map(image), Box(0.45, 0.45, 0.10, 0.10))
        assert metrics.subject_relative_acutance < 0.5

    def test_featureless_background_makes_the_ratio_undefined(self) -> None:
        """A night sky or blank wall has no texture to compare against, so the
        division is meaningless -- and a huge meaningless ratio sorts empty
        frames above real photographs. Undefined must read as undefined."""
        image = np.full((256, 256), 0.05, dtype=np.float32)  # near-black frame
        image[112:144, 112:144] = checkerboard(32, 4)  # one small lit subject
        metrics = sharpness.measure(
            sharpness.build_map(image), Box(0.42, 0.42, 0.16, 0.16), min_background_acutance=2.0
        )
        assert metrics.subject_acutance > 0
        assert metrics.subject_background_ratio is None

    def test_textured_background_still_yields_a_ratio(self) -> None:
        image = blurred(checkerboard(256, 4), passes=2)
        image[112:144, 112:144] = checkerboard(32, 4)
        metrics = sharpness.measure(
            sharpness.build_map(image), Box(0.42, 0.42, 0.16, 0.16), min_background_acutance=2.0
        )
        assert metrics.subject_background_ratio is not None
        assert metrics.subject_background_ratio > 1.0

    def test_grid_adapts_to_aspect_ratio(self) -> None:
        wide = sharpness.build_map(checkerboard(256)[:64, :], grid_long_edge=16)
        assert wide.tile_cols == 16
        assert wide.tile_rows < wide.tile_cols


class TestBlur:
    def test_directional_smear_reads_as_motion(self) -> None:
        metrics = blur.measure(horizontally_smeared(checkerboard(256, 6), cell=6), 1.0, 40.0)
        assert metrics.likely_cause == "motion"
        assert metrics.anisotropy > 0.35

    def test_uniform_blur_reads_as_defocus(self) -> None:
        metrics = blur.measure(blurred(checkerboard(256, 6), passes=6), 1.0, 40.0)
        assert metrics.likely_cause == "defocus"

    def test_a_sharp_frame_is_not_diagnosed(self) -> None:
        """Anisotropy also responds to content, so it must not speak when there
        is no blur to explain -- a picket fence is directional and perfectly
        sharp."""
        metrics = blur.measure(checkerboard(256, 6), 100.0, 40.0)
        assert metrics.likely_cause == "sharp"

    def test_featureless_frame_is_reported_as_such(self) -> None:
        flat = np.full((64, 64), 0.5, dtype=np.float32)
        assert blur.measure(flat, 0.0, 40.0).likely_cause == "featureless"


class TestExposure:
    def test_clipping_is_detected_at_both_ends(self) -> None:
        image = np.full((100, 100), 0.5, dtype=np.float32)
        image[:10, :] = 1.0
        image[10:20, :] = 0.0
        metrics = exposure.measure(image)
        assert metrics.highlight_clipped == pytest.approx(0.1, abs=0.01)
        assert metrics.shadow_clipped == pytest.approx(0.1, abs=0.01)

    def test_a_single_hot_pixel_does_not_define_dynamic_range(self) -> None:
        image = np.full((100, 100), 0.5, dtype=np.float32)
        image[0, 0] = 1.0
        assert exposure.measure(image).dynamic_range < 0.1

    def test_empty_input_is_survivable(self) -> None:
        assert exposure.measure(np.array([], dtype=np.float32)).mean_luma == 0.0
