"""Compose detectors into an ordered fallback chain.

The chain is itself a detector -- same interface, so it can be nested or swapped
for a single detector anywhere. What it adds is bookkeeping: when the preferred
detector cannot run or finds nothing, the report says which one answered instead
and why the earlier ones did not.
"""

from __future__ import annotations

from typing import Sequence

from ..models import Detection
from .base import DetectionContext, SubjectDetector, not_found


class DetectorChain:
    """Try each detector in order; the first to find a subject wins."""

    name = "chain"

    def __init__(self, detectors: Sequence[SubjectDetector]) -> None:
        if not detectors:
            raise ValueError("a detector chain needs at least one detector")
        self._detectors = list(detectors)
        self._availability: dict[str, tuple[bool, str]] = {}

    @staticmethod
    def _ask_available(detector: SubjectDetector) -> tuple[bool, str]:
        """Probe a detector without letting it break the run.

        A dependency that is installed but incompatible -- a different major
        version, a renamed API, a corrupt model file -- raises from inside
        ``available()``. That is exactly the situation a fallback chain exists to
        survive, so it is recorded as a reason and treated as unavailable.
        """
        try:
            return detector.available()
        except Exception as error:
            return False, f"{type(error).__name__}: {error}"

    @property
    def members(self) -> list[str]:
        return [detector.name for detector in self._detectors]

    def availability_report(self) -> list[tuple[str, bool, str]]:
        """Per-detector ``(name, usable, reason)``, computed once and cached.

        Surfaced by the CLI at startup so a missing dependency is one clear line
        before the run rather than a mystery in eight hundred rows after it.
        """
        report = []
        for detector in self._detectors:
            if detector.name not in self._availability:
                self._availability[detector.name] = self._ask_available(detector)
            usable, reason = self._availability[detector.name]
            report.append((detector.name, usable, reason))
        return report

    def available(self) -> tuple[bool, str]:
        if any(usable for _, usable, _ in self.availability_report()):
            return True, ""
        return False, "no detector in the chain can run"

    def detect(self, context: DetectionContext) -> Detection:
        skipped: list[str] = []
        for detector in self._detectors:
            if detector.name not in self._availability:
                self._availability[detector.name] = self._ask_available(detector)
            usable, reason = self._availability[detector.name]
            if not usable:
                skipped.append(f"{detector.name} unavailable ({reason})")
                continue

            try:
                detection = detector.detect(context)
            except Exception as error:
                # One detector failing on one awkward frame must not lose the
                # other measurements for that frame.
                skipped.append(f"{detector.name} failed: {type(error).__name__}: {error}")
                continue
            if detection.found:
                if skipped:
                    trail = "; ".join(skipped)
                    note = f"{detection.note} [after {trail}]" if detection.note else f"after {trail}"
                    return Detection(detection.box, detection.source, detection.confidence, note)
                return detection
            skipped.append(f"{detector.name}: {detection.note or 'nothing found'}")

        return not_found("none", "; ".join(skipped) if skipped else "no detector produced a subject")
