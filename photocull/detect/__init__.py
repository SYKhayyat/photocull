"""Subject detectors, and the factory that builds them from configuration names.

The registry is a plain mapping of name to builder. A third-party detector
becomes available by inserting one entry; nothing in the pipeline, the CLI or
the reporting layer needs to know it exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Sequence

from ..errors import ConfigError
from .afpoint import AFPointDetector
from .base import DetectionContext, SubjectDetector, not_found
from .chain import DetectorChain
from .face import FaceDetector
from .saliency import SaliencyDetector
from .simple import ZONES, ManualDetector, NullDetector, ZoneDetector

__all__ = [
    "AFPointDetector",
    "DETECTOR_NAMES",
    "DetectionContext",
    "DetectorChain",
    "FaceDetector",
    "ManualDetector",
    "NullDetector",
    "SaliencyDetector",
    "SubjectDetector",
    "ZONES",
    "ZoneDetector",
    "build_chain",
    "not_found",
]


class DetectorOptions:
    """Inputs a builder may need that are not part of the detector's name."""

    def __init__(
        self,
        root: Path,
        sidecar: Path,
        zone: str,
        prefer_eyes: bool,
        face_score: float = 0.9,
        face_min_size: float = 0.05,
    ) -> None:
        self.root = root
        self.sidecar = sidecar
        self.zone = zone
        self.prefer_eyes = prefer_eyes
        self.face_score = face_score
        self.face_min_size = face_min_size


Builder = Callable[[DetectorOptions], SubjectDetector]

_REGISTRY: dict[str, Builder] = {
    "af-point": lambda o: AFPointDetector(o.root),
    "face": lambda o: FaceDetector(
        prefer_eyes=o.prefer_eyes, min_size_fraction=o.face_min_size, score=o.face_score
    ),
    "saliency": lambda o: SaliencyDetector(),
    "zone": lambda o: ZoneDetector(o.zone),
    "manual": lambda o: ManualDetector(o.sidecar),
    "none": lambda o: NullDetector(),
}

DETECTOR_NAMES = tuple(_REGISTRY)


def build_chain(
    names: Sequence[str],
    root: Path,
    sidecar: Path,
    zone: str = "center",
    prefer_eyes: bool = True,
    face_score: float = 0.9,
    face_min_size: float = 0.05,
) -> DetectorChain:
    """Construct a fallback chain from an ordered list of detector names."""
    if not names:
        raise ConfigError("subject.detectors must list at least one detector")

    options = DetectorOptions(
        root=root,
        sidecar=sidecar,
        zone=zone,
        prefer_eyes=prefer_eyes,
        face_score=face_score,
        face_min_size=face_min_size,
    )
    detectors: list[SubjectDetector] = []
    for name in names:
        builder = _REGISTRY.get(name)
        if builder is None:
            raise ConfigError(f"unknown detector '{name}'; known: {', '.join(DETECTOR_NAMES)}")
        detectors.append(builder(options))
    return DetectorChain(detectors)
