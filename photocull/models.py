"""Value objects shared across the pipeline.

Everything here is immutable and free of behaviour that touches the filesystem,
so metrics, detectors and writers can all depend on these types without
depending on each other.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Sequence

import numpy as np


class Confidence(str, Enum):
    """How much weight a consumer should put on a detector's answer.

    Deliberately coarse. A detector that reports a float it cannot justify is
    worse than one that admits to a bucket.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class Box:
    """A rectangle in normalised image coordinates (0..1, origin top-left).

    Normalised rather than pixel-based so one box stays valid across the
    working-resolution copy, the thumbnail and the original file at once.
    """

    x: float
    y: float
    w: float
    h: float

    def __post_init__(self) -> None:
        if self.w <= 0 or self.h <= 0:
            raise ValueError(f"box must have positive extent, got {self.w}x{self.h}")

    def clipped(self) -> "Box":
        """Return a copy guaranteed to lie inside the unit square."""
        x = min(max(self.x, 0.0), 1.0)
        y = min(max(self.y, 0.0), 1.0)
        w = min(self.w, 1.0 - x)
        h = min(self.h, 1.0 - y)
        return Box(x, y, max(w, 1e-6), max(h, 1e-6))

    def to_pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        """Return ``(left, top, right, bottom)`` for an image of this size."""
        c = self.clipped()
        left = int(round(c.x * width))
        top = int(round(c.y * height))
        right = max(left + 1, int(round((c.x + c.w) * width)))
        bottom = max(top + 1, int(round((c.y + c.h) * height)))
        return left, top, min(right, width), min(bottom, height)

    @property
    def area(self) -> float:
        return self.w * self.h

    def as_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}


@dataclass(frozen=True, slots=True)
class Detection:
    """A subject region plus the provenance needed to trust or distrust it.

    ``source`` is what makes this reportable rather than magical: the contact
    sheet shows which detector fired, so a wrong subject is visible instead of
    silently poisoning every number downstream.
    """

    box: Box | None
    source: str
    confidence: Confidence
    note: str = ""

    @property
    def found(self) -> bool:
        return self.box is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "box": self.box.as_dict() if self.box else None,
            "source": self.source,
            "confidence": self.confidence.value,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class SharpnessMap:
    """A grid of per-tile acutance values, and the queries over it.

    This is the one expensive computation in the pipeline. Every sharpness
    figure the tool reports -- global, subject, background, focus location,
    depth-of-field spread -- is a cheap query against this array rather than a
    separate pass over the pixels.
    """

    tiles: np.ndarray = field(repr=False)
    tile_rows: int
    tile_cols: int

    @property
    def max(self) -> float:
        """Peak local acutance: 'is anything in this frame critically sharp?'

        Immune to bokeh, which is the whole point. A frame that is mostly out of
        focus by design still scores high here when the subject landed.
        """
        return float(self.tiles.max())

    @property
    def mean(self) -> float:
        return float(self.tiles.mean())

    @property
    def median(self) -> float:
        return float(np.median(self.tiles))

    def sharp_fraction(self, threshold_ratio: float) -> float:
        """Fraction of tiles at least ``threshold_ratio`` as sharp as the peak.

        Descriptive, never a quality score: high means deep depth of field, low
        means shallow. Neither is better; they are different photographs.
        """
        peak = self.max
        if peak <= 0:
            return 0.0
        return float((self.tiles >= peak * threshold_ratio).mean())

    def focus_point(self) -> tuple[float, float]:
        """Normalised (x, y) centre of the sharpest tile."""
        idx = int(np.argmax(self.tiles))
        row, col = divmod(idx, self.tile_cols)
        return ((col + 0.5) / self.tile_cols, (row + 0.5) / self.tile_rows)

    def _mask_for(self, box: Box) -> np.ndarray:
        """Boolean tile mask covering ``box``; always selects at least one tile."""
        c = box.clipped()
        col0 = int(math.floor(c.x * self.tile_cols))
        col1 = int(math.ceil((c.x + c.w) * self.tile_cols))
        row0 = int(math.floor(c.y * self.tile_rows))
        row1 = int(math.ceil((c.y + c.h) * self.tile_rows))
        col0 = min(max(col0, 0), self.tile_cols - 1)
        row0 = min(max(row0, 0), self.tile_rows - 1)
        col1 = min(max(col1, col0 + 1), self.tile_cols)
        row1 = min(max(row1, row0 + 1), self.tile_rows)
        mask = np.zeros(self.tiles.shape, dtype=bool)
        mask[row0:row1, col0:col1] = True
        return mask

    def within(self, box: Box) -> float:
        """Peak acutance inside ``box``.

        Peak rather than mean: a face box contains cheeks and hair as well as an
        eye, and averaging the soft parts in understates a shot that put focus
        exactly where it mattered.
        """
        return float(self.tiles[self._mask_for(box)].max())

    def outside(self, box: Box) -> float:
        """Median acutance outside ``box``.

        Median rather than peak: one sharp twig in a corner should not
        masquerade as a sharp background and sink an otherwise clean figure for
        subject isolation.
        """
        mask = ~self._mask_for(box)
        if not mask.any():
            return 0.0
        return float(np.median(self.tiles[mask]))

    def as_list(self) -> list[list[float]]:
        return [[round(float(v), 4) for v in row] for row in self.tiles.tolist()]


@dataclass(frozen=True, slots=True)
class SharpnessMetrics:
    """Everything the sharpness map has to say about one photograph."""

    global_acutance: float
    max_local_acutance: float
    median_acutance: float
    sharp_fraction: float
    focus_x: float
    focus_y: float
    subject_acutance: float | None
    background_acutance: float | None
    subject_background_ratio: float | None
    subject_relative_acutance: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExposureMetrics:
    """Tonal health, measured and not judged."""

    highlight_clipped: float
    shadow_clipped: float
    dynamic_range: float
    mean_luma: float
    contrast: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BlurMetrics:
    """Whether blur looks directional (camera shake) or isotropic (missed focus).

    ``anisotropy`` near zero means blur is equal in all directions, which points
    at focus. Near one means the smear has an axis, which points at motion --
    yours or the subject's. The distinction matters because the two mistakes
    have completely different remedies.
    """

    anisotropy: float
    dominant_axis_degrees: float
    likely_cause: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CaptureInfo:
    """Camera metadata, all optional because not every file carries it."""

    camera: str | None = None
    lens: str | None = None
    iso: int | None = None
    aperture: float | None = None
    shutter_seconds: float | None = None
    focal_length: float | None = None
    focal_length_35mm: float | None = None
    timestamp: str | None = None
    orientation: int | None = None

    @property
    def reciprocal_margin(self) -> float | None:
        """Stops of margin against the 1/focal-length handholding rule.

        Positive means the shutter was faster than the rule of thumb wants;
        negative means the frame was at risk of shake before the shutter opened.
        ``None`` when the file does not carry both numbers.
        """
        focal = self.focal_length_35mm or self.focal_length
        if not focal or not self.shutter_seconds or self.shutter_seconds <= 0:
            return None
        return float(math.log2((1.0 / focal) / self.shutter_seconds))

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["reciprocal_margin"] = self.reciprocal_margin
        return data


@dataclass(frozen=True, slots=True)
class PhotoReport:
    """The complete record for one photograph.

    This is the canonical output. Every writer -- CSV, XMP, HTML, the plain file
    list -- is a projection of this object, so adding a format never means
    touching the analysis.
    """

    path: str
    filename: str
    width: int
    height: int
    sharpness: SharpnessMetrics
    exposure: ExposureMetrics
    blur: BlurMetrics
    detection: Detection
    capture: CaptureInfo
    group_id: int | None = None
    group_rank: int | None = None
    group_size: int = 1
    rating: int | None = None
    label: str | None = None
    reasons: Sequence[str] = ()
    tile_map: Sequence[Sequence[float]] | None = None
    thumbnail_uri: str | None = field(default=None, repr=False)
    error: str | None = None

    @property
    def is_group_best(self) -> bool:
        return self.group_rank == 0 and self.group_size > 1

    def as_dict(self, include_tiles: bool = False, include_thumb: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "path": self.path,
            "filename": self.filename,
            "width": self.width,
            "height": self.height,
            "sharpness": self.sharpness.as_dict(),
            "exposure": self.exposure.as_dict(),
            "blur": self.blur.as_dict(),
            "detection": self.detection.as_dict(),
            "capture": self.capture.as_dict(),
            "group": {
                "id": self.group_id,
                "rank": self.group_rank,
                "size": self.group_size,
                "is_best": self.is_group_best,
            },
            "rating": self.rating,
            "label": self.label,
            "reasons": list(self.reasons),
            "error": self.error,
        }
        if include_tiles and self.tile_map is not None:
            data["tile_map"] = [list(r) for r in self.tile_map]
        if include_thumb and self.thumbnail_uri:
            data["thumbnail"] = self.thumbnail_uri
        return data

    def flat_metrics(self) -> dict[str, Any]:
        """A single flat namespace for rating expressions and CSV columns.

        Keeping this in one place guarantees the names a user writes in their
        config match the columns they see in the spreadsheet.
        """
        flat: dict[str, Any] = {
            "filename": self.filename,
            "width": self.width,
            "height": self.height,
            "group_id": self.group_id,
            "group_rank": self.group_rank,
            "group_size": self.group_size,
            "is_group_best": self.is_group_best,
            "subject_source": self.detection.source,
            "subject_found": self.detection.found,
            "subject_confidence": self.detection.confidence.value,
        }
        for metrics in (
            self.sharpness.as_dict(),
            self.exposure.as_dict(),
            self.blur.as_dict(),
            self.capture.as_dict(),
        ):
            flat.update(metrics)

        # A convenience the rules and the group ranking both want: the best
        # available answer to "how sharp is the part that matters", which is the
        # subject when one was found and the sharpest region otherwise.
        flat["subject_or_max_acutance"] = (
            self.sharpness.subject_acutance
            if self.sharpness.subject_acutance is not None
            else self.sharpness.max_local_acutance
        )
        return flat
