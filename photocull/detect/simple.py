"""The two detectors that need no cleverness: a fixed zone, and your own box.

Neither is sophisticated and both are useful. The zone detector is honest about
being an assumption rather than a measurement. The manual detector is the escape
hatch for everything automation gets wrong -- and since it reads a plain JSON
sidecar, the contact sheet can write that file and the next run simply obeys it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from ..errors import ConfigError
from ..models import Box, Confidence, Detection
from .base import DetectionContext, not_found

# Named zones as (x, y, w, h) in normalised coordinates. The thirds entries
# target the intersections a photographer composing on a grid would have used.
ZONES: Mapping[str, tuple[float, float, float, float]] = {
    "center": (0.30, 0.30, 0.40, 0.40),
    "center-small": (0.40, 0.40, 0.20, 0.20),
    "center-wide": (0.20, 0.20, 0.60, 0.60),
    "upper-third": (0.25, 0.08, 0.50, 0.42),
    "lower-third": (0.25, 0.50, 0.50, 0.42),
    "left-third": (0.08, 0.25, 0.42, 0.50),
    "right-third": (0.50, 0.25, 0.42, 0.50),
}


class ZoneDetector:
    """Assume the subject occupies a fixed region of the frame."""

    name = "zone"

    def __init__(self, zone: str = "center") -> None:
        if zone not in ZONES:
            raise ConfigError(f"unknown zone '{zone}'; choose from {sorted(ZONES)}")
        self._zone = zone
        self._box = Box(*ZONES[zone])

    def available(self) -> tuple[bool, str]:
        return True, ""

    def detect(self, context: DetectionContext) -> Detection:
        return Detection(
            box=self._box,
            source=f"{self.name}:{self._zone}",
            confidence=Confidence.LOW,
            note="fixed region; an assumption about composition, not a measurement",
        )


class ManualDetector:
    """Read subject boxes you drew yourself, from a JSON sidecar.

    Format is a flat object mapping filename to a box in normalised
    coordinates::

        {"DSC_2204.NEF": {"x": 0.41, "y": 0.22, "w": 0.18, "h": 0.24}}

    Keyed by filename rather than full path so the file survives the folder
    being moved or renamed, which is the common case for a photo library.
    """

    name = "manual"

    def __init__(self, sidecar: Path) -> None:
        self._sidecar = Path(sidecar)
        self._boxes: dict[str, Box] = {}
        self._loaded = False
        self._reason = ""

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._sidecar.exists():
            self._reason = f"no sidecar at {self._sidecar}"
            return
        try:
            raw = json.loads(self._sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"cannot read subject sidecar {self._sidecar}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"subject sidecar {self._sidecar} must contain a JSON object")

        for key, value in raw.items():
            try:
                self._boxes[Path(key).name] = Box(
                    float(value["x"]), float(value["y"]), float(value["w"]), float(value["h"])
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ConfigError(f"bad box for '{key}' in {self._sidecar}: {exc}") from exc

    def available(self) -> tuple[bool, str]:
        self._load()
        if self._boxes:
            return True, ""
        return False, self._reason or f"sidecar {self._sidecar} contains no boxes"

    def detect(self, context: DetectionContext) -> Detection:
        self._load()
        box = self._boxes.get(Path(context.path).name)
        if box is None:
            return not_found(self.name, "no hand-drawn box for this file")
        return Detection(
            box=box.clipped(),
            source=self.name,
            confidence=Confidence.HIGH,
            note="drawn by hand",
        )


class NullDetector:
    """Explicitly decline to locate a subject.

    Not a no-op for its own sake. With subject detection off, the report falls
    back to whole-frame figures -- peak local acutance and focus position -- which
    remain meaningful precisely because they never assumed a subject in the first
    place.
    """

    name = "none"

    def available(self) -> tuple[bool, str]:
        return True, ""

    def detect(self, context: DetectionContext) -> Detection:
        return not_found(self.name, "subject detection disabled")
