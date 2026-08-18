"""Spectral-residual saliency: a subject detector with no dependencies at all.

The observation behind it is that the log-amplitude spectra of natural images
are remarkably alike -- averaged over enough photographs they collapse onto the
same smooth curve. Whatever makes *this* photograph different from that average
is, by definition, the part that is not statistically ordinary, and the eye
lands on precisely that. So: take the log amplitude spectrum, subtract a
smoothed copy of itself, keep the residual, invert the transform. What comes
back is a map of the unusual.

It costs one small FFT and works on birds, buildings and plates of food, none of
which a face detector will help with. It is genuinely weaker than a trained
detector and is reported as ``low`` confidence for that reason.

Reference: Hou & Zhang, "Saliency Detection: A Spectral Residual Approach", 2007.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from ..models import Box, Confidence, Detection
from .base import DetectionContext, not_found

# The published method works at a very small size. That is not a shortcut: the
# spectral residual describes coarse structure, and fine detail only adds noise
# to it.
_WORK_SIZE = 64
_SMOOTH_RADIUS = 1


def _box_blur(values: np.ndarray, radius: int) -> np.ndarray:
    """Mean filter over a (2r+1) square, via a summed-area table."""
    padded = np.pad(values, radius + 1, mode="edge")
    integral = padded.cumsum(axis=0).cumsum(axis=1)
    size = 2 * radius + 1
    bottom_right = integral[size:, size:]
    top_right = integral[:-size, size:]
    bottom_left = integral[size:, :-size]
    top_left = integral[:-size, :-size]
    total = bottom_right - top_right - bottom_left + top_left
    return total[: values.shape[0], : values.shape[1]] / (size * size)


def saliency_map(luma: np.ndarray) -> np.ndarray:
    """Compute a normalised saliency map in 0..1 at the working size."""
    small = np.asarray(
        Image.fromarray((np.clip(luma, 0, 1) * 255).astype(np.uint8)).resize(
            (_WORK_SIZE, _WORK_SIZE), Image.Resampling.BOX
        ),
        dtype=np.float32,
    )

    spectrum = np.fft.fft2(small)
    amplitude = np.abs(spectrum)
    log_amplitude = np.log(amplitude + 1e-8)
    residual = log_amplitude - _box_blur(log_amplitude, _SMOOTH_RADIUS)

    reconstructed = np.fft.ifft2(np.exp(residual + 1j * np.angle(spectrum)))
    energy = np.abs(reconstructed) ** 2
    energy = _box_blur(energy, 2)

    peak = energy.max()
    return energy / peak if peak > 0 else energy


def _weighted_box(salience: np.ndarray, spread: float) -> Box | None:
    """Fit a box to the salient mass using its centroid and standard deviation.

    Moments rather than connected components: no labelling pass, no threshold
    that has to be tuned per image, and a subject split across a thin occlusion
    still produces one sensible box instead of two useless ones.
    """
    threshold = float(salience.mean() + salience.std())
    mask = salience >= threshold
    if not mask.any():
        return None

    weights = np.where(mask, salience, 0.0)
    total = weights.sum()
    if total <= 0:
        return None

    rows, cols = np.indices(salience.shape)
    centre_y = float((weights * rows).sum() / total) / salience.shape[0]
    centre_x = float((weights * cols).sum() / total) / salience.shape[1]
    var_y = float((weights * (rows / salience.shape[0] - centre_y) ** 2).sum() / total)
    var_x = float((weights * (cols / salience.shape[1] - centre_x) ** 2).sum() / total)

    half_w = max(spread * np.sqrt(var_x), 0.05)
    half_h = max(spread * np.sqrt(var_y), 0.05)
    return Box(centre_x - half_w, centre_y - half_h, 2 * half_w, 2 * half_h).clipped()


class SaliencyDetector:
    """Subject detection by spectral residual. Always available."""

    name = "saliency"

    def __init__(self, spread: float = 1.5) -> None:
        self._spread = spread

    def available(self) -> tuple[bool, str]:
        return True, ""

    def detect(self, context: DetectionContext) -> Detection:
        if context.luma.size == 0:
            return not_found(self.name, "empty image")

        box = _weighted_box(saliency_map(context.luma), self._spread)
        if box is None:
            return not_found(self.name, "no region stood out from the background")

        # A box covering most of the frame is not a subject, it is a shrug. Say
        # so rather than reporting a subject/background ratio computed against
        # almost nothing.
        if box.area > 0.75:
            return not_found(self.name, "salient region covers the whole frame")

        return Detection(
            box=box,
            source=self.name,
            confidence=Confidence.LOW,
            note="statistical salience, not a recognised object",
        )
