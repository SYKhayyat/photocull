"""Tiled acutance measurement -- the one expensive pass over the pixels.

The central idea of this package lives here. A single scalar "sharpness" for a
whole photograph is a broken measurement: Laplacian variance responds to
*texture*, so a brick wall outscores a portrait regardless of focus, and a
deliberately shallow depth of field is punished for being exactly what the
photographer wanted.

The fix is to stop producing a scalar. We cut the frame into tiles, measure each
one independently, and let every downstream figure be a query against that map:

* peak local acutance answers "did anything land?" and ignores bokeh entirely
* the sharpest tile's position answers "where did focus actually go?"
* subject and background acutance are two more queries, once a box is known
* the ratio between them is comparable *across* photographs, because the
  texture confound scales both halves and cancels

Everything is measured at a fixed working resolution. Acutance is scale
dependent -- the same photograph downsampled further reads as blurrier -- so a
consistent working size is what makes numbers from a 45MP body and a phone
comparable at all.
"""

from __future__ import annotations

import numpy as np

from ..models import Box, SharpnessMap, SharpnessMetrics

# Discrete Laplacian, 8-neighbour form. More isotropic than the 4-neighbour
# version, which matters because we later ask whether blur has a direction.
_LAPLACIAN_KERNEL = np.array(
    [[1.0, 1.0, 1.0], [1.0, -8.0, 1.0], [1.0, 1.0, 1.0]], dtype=np.float32
)

# Acutance values are tiny in normalised-luma units. This constant puts them in
# a readable range instead of forcing every config threshold into scientific
# notation. Calibrated against a real library of 24MP raws: peak local acutance
# runs about 6 at the 10th percentile, 40 at the median and 130 at the top, so a
# threshold like "40 is sharp" is a number a person can reason about.
ACUTANCE_SCALE = 100.0


def convolve3x3(image: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Apply a 3x3 kernel with 'valid' borders, using shifts rather than loops.

    Nine multiply-adds over whole arrays. NumPy does the work in C, so the
    Python overhead here is per-image, not per-pixel -- which is what keeps a
    pure-NumPy implementation fast enough to skip an OpenCV dependency.
    """
    if image.ndim != 2:
        raise ValueError(f"expected a 2-D array, got shape {image.shape}")
    if image.shape[0] < 3 or image.shape[1] < 3:
        return np.zeros((1, 1), dtype=np.float32)

    out = np.zeros((image.shape[0] - 2, image.shape[1] - 2), dtype=np.float32)
    for dy in range(3):
        for dx in range(3):
            weight = kernel[dy, dx]
            if weight != 0.0:
                out += weight * image[dy : dy + out.shape[0], dx : dx + out.shape[1]]
    return out


def _tile_reduce(values: np.ndarray, rows: int, cols: int) -> np.ndarray:
    """Reduce a 2-D array to a ``rows x cols`` grid of per-tile RMS values.

    Tiles are produced by even splitting rather than reshaping so that a grid
    which does not divide the image exactly still yields a full map instead of
    silently cropping the right and bottom edges.
    """
    row_edges = np.linspace(0, values.shape[0], rows + 1).astype(int)
    col_edges = np.linspace(0, values.shape[1], cols + 1).astype(int)
    out = np.zeros((rows, cols), dtype=np.float32)
    squared = values * values
    for r in range(rows):
        r0, r1 = row_edges[r], max(row_edges[r + 1], row_edges[r] + 1)
        band = squared[r0:r1]
        for c in range(cols):
            c0, c1 = col_edges[c], max(col_edges[c + 1], col_edges[c] + 1)
            out[r, c] = np.sqrt(band[:, c0:c1].mean())
    return out * ACUTANCE_SCALE


def build_map(luma: np.ndarray, grid_long_edge: int = 24) -> SharpnessMap:
    """Compute the tiled acutance map for a normalised luma image.

    ``luma`` must be float in 0..1. ``grid_long_edge`` sets the tile count along
    the longer side; the shorter side gets a proportional count so tiles stay
    roughly square and a subject box maps onto a sensible number of them.
    """
    if luma.ndim != 2:
        raise ValueError("luma must be a 2-D array")

    response = convolve3x3(luma.astype(np.float32, copy=False), _LAPLACIAN_KERNEL)
    height, width = response.shape
    if height >= width:
        rows = grid_long_edge
        cols = max(1, int(round(grid_long_edge * width / height)))
    else:
        cols = grid_long_edge
        rows = max(1, int(round(grid_long_edge * height / width)))

    rows = min(rows, max(1, height))
    cols = min(cols, max(1, width))
    return SharpnessMap(tiles=_tile_reduce(response, rows, cols), tile_rows=rows, tile_cols=cols)


def measure(
    sharpness_map: SharpnessMap,
    subject: Box | None,
    sharp_fraction_threshold: float = 0.5,
) -> SharpnessMetrics:
    """Turn the map plus an optional subject box into the reported figures.

    When no subject is known the subject-relative fields are ``None`` rather
    than a fabricated default. A missing measurement should read as missing, not
    as zero -- zero is a claim, and it is the wrong one.
    """
    focus_x, focus_y = sharpness_map.focus_point()

    subject_acutance: float | None = None
    background_acutance: float | None = None
    ratio: float | None = None
    relative: float | None = None
    if subject is not None:
        subject_acutance = sharpness_map.within(subject)
        background_acutance = sharpness_map.outside(subject)
        # Guarded so an entirely smooth background (sky, studio backdrop) yields
        # a large finite ratio instead of an infinity that breaks JSON and sorts.
        ratio = subject_acutance / max(background_acutance, 1e-6)
        ratio = float(min(ratio, 999.0))
        # Subject acutance against the sharpest thing anywhere in the same frame.
        # Raw subject acutance is the peak over the tiles a box happens to cover,
        # so a box spanning half the frame collects a far higher number than a
        # box over someone's eyes -- comparing the two across detectors is
        # meaningless. This ratio removes the box-size dependence: 1.0 means the
        # subject IS the sharpest thing in the photograph.
        peak = sharpness_map.max
        relative = float(min(subject_acutance / peak, 1.0)) if peak > 0 else None

    return SharpnessMetrics(
        global_acutance=sharpness_map.mean,
        max_local_acutance=sharpness_map.max,
        median_acutance=sharpness_map.median,
        sharp_fraction=sharpness_map.sharp_fraction(sharp_fraction_threshold),
        focus_x=focus_x,
        focus_y=focus_y,
        subject_acutance=subject_acutance,
        background_acutance=background_acutance,
        subject_background_ratio=ratio,
        subject_relative_acutance=relative,
    )
