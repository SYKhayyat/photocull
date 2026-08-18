"""Subject location taken from the camera's own autofocus metadata.

This is the most interesting detector in the package and the one nothing else
offers, because it does not guess at your subject at all. The camera recorded
where you told it to focus. Measuring acutance at that exact spot answers the
only question that was ever really being asked: *did focus land where I aimed
it?*

The catch is that autofocus metadata lives in manufacturer maker-notes, which
are undocumented, mutually incompatible, and in places deliberately obfuscated.
Rather than reimplement two decades of reverse engineering badly, this detector
delegates to exiftool when it is installed and reports itself unavailable when
it is not. A detector that silently produced a wrong box here would poison the
one measurement users would trust most.

Even with exiftool, only cameras that record AF *coordinates* are usable. Bodies
that record a point *name* ("C6") need a per-model sensor-layout table to become
coordinates, and inventing that mapping would be a guess wearing a lab coat.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ..models import Box, Confidence, Detection
from .base import DetectionContext, not_found

# Requested in one batch call. Different manufacturers populate different
# subsets; whichever pair is present gets used.
_FIELDS = (
    "SourceFile",
    "AFAreaXPosition",
    "AFAreaYPosition",
    "AFAreaWidth",
    "AFAreaHeight",
    "AFAreaMode",
    "FocusPixel",
    "ExifImageWidth",
    "ExifImageHeight",
    "ImageWidth",
    "ImageHeight",
)

_BATCH_TIMEOUT_SECONDS = 300


class AFPointDetector:
    """Subject box centred on the camera's selected autofocus area.

    Metadata for the whole tree is fetched in a single exiftool invocation the
    first time it is needed. Spawning one process per file would cost more than
    the entire rest of the analysis on a session of any size.
    """

    name = "af-point"

    def __init__(self, root: Path, box_fraction: float = 0.12) -> None:
        self._root = Path(root)
        self._box_fraction = box_fraction
        self._boxes: dict[str, Box] | None = None
        self._reason = ""

    def available(self) -> tuple[bool, str]:
        if shutil.which("exiftool") is None:
            return False, "exiftool is not installed (needed to read autofocus maker-notes)"
        self._ensure_loaded()
        if not self._boxes:
            return False, self._reason or "no files carry autofocus coordinates"
        return True, ""

    def _ensure_loaded(self) -> None:
        if self._boxes is not None:
            return
        self._boxes = {}

        command = ["exiftool", "-j", "-n", "-q", "-r"]
        command += [f"-{field}" for field in _FIELDS]
        command.append(str(self._root))
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=_BATCH_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._reason = f"exiftool did not complete: {exc}"
            return

        if not completed.stdout.strip():
            self._reason = "exiftool returned no metadata"
            return
        try:
            records = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            self._reason = f"could not parse exiftool output: {exc}"
            return

        for record in records:
            box = self._box_from_record(record)
            if box is not None:
                self._boxes[Path(record["SourceFile"]).name] = box

        if not self._boxes:
            self._reason = "no files in this tree record autofocus coordinates"

    def _box_from_record(self, record: dict) -> Box | None:
        """Turn one exiftool record into a normalised box, if it has coordinates."""
        width = record.get("ExifImageWidth") or record.get("ImageWidth")
        height = record.get("ExifImageHeight") or record.get("ImageHeight")
        if not width or not height:
            return None

        x = record.get("AFAreaXPosition")
        y = record.get("AFAreaYPosition")
        if x is None or y is None:
            # Fujifilm records a single "FocusPixel" pair instead.
            focus_pixel = record.get("FocusPixel")
            if isinstance(focus_pixel, str) and " " in focus_pixel:
                parts = focus_pixel.split()
                try:
                    x, y = float(parts[0]), float(parts[1])
                except ValueError:
                    return None
            else:
                return None

        try:
            centre_x = float(x) / float(width)
            centre_y = float(y) / float(height)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
        if not (0.0 <= centre_x <= 1.0 and 0.0 <= centre_y <= 1.0):
            return None

        # Use the recorded AF area size when the body provides it; otherwise a
        # fixed fraction of the frame, which is roughly the size of a modern
        # single-point AF box.
        area_w = record.get("AFAreaWidth")
        area_h = record.get("AFAreaHeight")
        try:
            half_w = float(area_w) / float(width) / 2 if area_w else self._box_fraction / 2
            half_h = float(area_h) / float(height) / 2 if area_h else self._box_fraction / 2
        except (TypeError, ValueError):
            half_w = half_h = self._box_fraction / 2

        half_w = max(half_w, 0.02)
        half_h = max(half_h, 0.02)
        return Box(centre_x - half_w, centre_y - half_h, 2 * half_w, 2 * half_h).clipped()

    def detect(self, context: DetectionContext) -> Detection:
        usable, reason = self.available()
        if not usable:
            return not_found(self.name, reason)

        assert self._boxes is not None
        box = self._boxes.get(Path(context.path).name)
        if box is None:
            return not_found(self.name, "no autofocus coordinates recorded for this frame")

        return Detection(
            box=box,
            source=self.name,
            confidence=Confidence.HIGH,
            note="where the camera was told to focus",
        )
