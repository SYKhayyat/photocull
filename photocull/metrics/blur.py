"""Distinguishing directional blur (motion) from isotropic blur (missed focus).

Motion blur smears detail along one axis and leaves detail perpendicular to it
intact, so the gradient energy of a shaken frame is lopsided. Defocus spreads
detail equally in every direction, so its gradient energy is balanced. The
structure tensor measures exactly that lopsidedness.

The honest limit, stated here because the report repeats it: gradient anisotropy
also responds to *content*. A picket fence, a skyline, a page of text all have
strongly directional gradients while being perfectly sharp. Anisotropy therefore
only diagnoses anything on a frame that is already soft, which is why
``likely_cause`` is gated on the acutance of the frame and reports "sharp" -- not
a blur cause -- whenever there is no blur to explain.
"""

from __future__ import annotations

import math

import numpy as np

from ..models import BlurMetrics


def _structure_tensor(luma: np.ndarray) -> tuple[float, float, float]:
    """Return the summed ``(Jxx, Jyy, Jxy)`` components of the structure tensor."""
    gy, gx = np.gradient(luma.astype(np.float32, copy=False))
    return float((gx * gx).mean()), float((gy * gy).mean()), float((gx * gy).mean())


def measure(luma: np.ndarray, max_local_acutance: float, sharp_threshold: float) -> BlurMetrics:
    """Characterise the blur in a normalised luma image.

    ``max_local_acutance`` and ``sharp_threshold`` come from the sharpness pass
    and decide whether a diagnosis is warranted at all.
    """
    if luma.size == 0:
        return BlurMetrics(0.0, 0.0, "unknown")

    jxx, jyy, jxy = _structure_tensor(luma)
    trace = jxx + jyy
    if trace <= 1e-12:
        # A featureless frame: no gradients at all, so nothing to decompose.
        return BlurMetrics(0.0, 0.0, "featureless")

    # Closed-form eigenvalues of the symmetric 2x2 tensor. Cheaper and more
    # numerically predictable here than a general eigensolver.
    spread = math.sqrt(max((jxx - jyy) ** 2 + 4.0 * jxy * jxy, 0.0))
    lambda_major = (trace + spread) / 2.0
    lambda_minor = (trace - spread) / 2.0
    anisotropy = float((lambda_major - lambda_minor) / max(lambda_major + lambda_minor, 1e-12))

    # Orientation of strongest gradient variation. Detail survives across the
    # motion axis and is destroyed along it, so the smear runs perpendicular to
    # the dominant gradient -- hence the 90 degree turn.
    gradient_angle = 0.5 * math.atan2(2.0 * jxy, jxx - jyy)
    motion_axis = math.degrees(gradient_angle) + 90.0
    motion_axis = motion_axis % 180.0

    if max_local_acutance >= sharp_threshold:
        cause = "sharp"
    elif anisotropy >= 0.35:
        cause = "motion"
    else:
        cause = "defocus"

    return BlurMetrics(
        anisotropy=anisotropy,
        dominant_axis_degrees=float(motion_axis),
        likely_cause=cause,
    )
