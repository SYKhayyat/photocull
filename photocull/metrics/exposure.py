"""Tonal measurements taken from the luma histogram.

Reported, never scored. Clipped highlights are a fact; whether they matter
depends on whether the photograph is a backlit portrait or a product shot, and
that is the photographer's call, not this tool's.
"""

from __future__ import annotations

import numpy as np

from ..models import ExposureMetrics

# A pixel within this distance of the ends of the range counts as clipped.
# Sensors and raw converters rarely produce exact 0.0 or 1.0, so testing for the
# literal endpoints under-reports clipping badly.
_CLIP_MARGIN = 2.0 / 255.0


def measure(luma: np.ndarray, percentile: float = 0.5) -> ExposureMetrics:
    """Measure tonal health of a normalised luma image (float, 0..1).

    ``dynamic_range`` is taken from percentiles rather than min/max: a single
    hot pixel or one specular highlight should not define the tonal range of an
    entire photograph. How far in to sample is ``[exposure].percentile`` in the
    config file -- reachable, rather than a parameter only this docstring knew
    about.

    Clipping is deliberately not measured through it. A clipped pixel is one at
    the end of the range, so moving the sample inwards would measure something
    else and go on calling it clipping.
    """
    if luma.size == 0:
        return ExposureMetrics(0.0, 0.0, 0.0, 0.0, 0.0)

    flat = luma.reshape(-1)
    highlight = float((flat >= 1.0 - _CLIP_MARGIN).mean())
    shadow = float((flat <= _CLIP_MARGIN).mean())

    low, high = np.percentile(flat, [percentile, 100.0 - percentile])
    return ExposureMetrics(
        highlight_clipped=highlight,
        shadow_clipped=shadow,
        dynamic_range=float(high - low),
        mean_luma=float(flat.mean()),
        contrast=float(flat.std()),
    )
