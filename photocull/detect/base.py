"""The subject-detector interface.

Every detector answers one question -- "where is the subject in this frame?" --
and every detector is allowed to answer "I don't know" or "I can't run here".
Those two answers are distinct and both are recorded, because a report that
cannot say *why* it fell back to a weaker detector is a report you have to
second-guess, and a verdict you have to second-guess saves nobody any time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

from ..models import Detection, Confidence
from ..tiffreader import TiffDirectory


@dataclass(frozen=True, slots=True)
class DetectionContext:
    """Everything a detector may look at, gathered once per photograph.

    Passing a context object rather than a long argument list means adding a new
    input for one detector does not change the signature every other detector
    has to implement.
    """

    luma: np.ndarray
    path: str
    directories: tuple[TiffDirectory, ...] = ()
    # The rotation already applied to ``luma``. Only detectors reading
    # sensor-space metadata need it -- the autofocus box arrives in the
    # orientation the file was written in, and has to be turned to match the
    # pixels before it means anything. The stored pixel dimensions used to live
    # here too and no detector ever read them: face works off ``luma.shape``,
    # saliency and zone are normalised throughout, manual and af-point key off
    # ``path``.
    orientation: int | None = None


@runtime_checkable
class SubjectDetector(Protocol):
    """Strategy for locating the subject of a photograph."""

    name: str

    def available(self) -> tuple[bool, str]:
        """Return ``(usable_here, reason_if_not)``.

        Checked once per run rather than per photograph so a missing dependency
        produces one clear message instead of eight hundred identical ones.
        """
        ...

    def detect(self, context: DetectionContext) -> Detection:
        """Locate the subject, or return a detection with ``box=None``."""
        ...


def not_found(source: str, note: str) -> Detection:
    """Helper for the 'ran fine, found nothing' answer."""
    return Detection(box=None, source=source, confidence=Confidence.NONE, note=note)
