"""Face detection, narrowed to the eyes wherever the detector can manage it.

Two backends, chosen by what the installed OpenCV actually offers:

* **YuNet** (OpenCV 5, and 4.5.4+) -- a small DNN that returns five facial
  landmarks with every box. The two eye landmarks give an exact eye region, so
  acutance is measured on the part of a portrait a viewer actually checks.
* **Haar cascade** (OpenCV 4 and earlier) -- kept as a fallback because those
  installations ship the cascade XML inside the wheel and work with no model
  file at all.

Why the eye region rather than the face: a face box contains hair, cheek and
collar, any of which can be crisp while the eyes are soft. Scoring the whole box
turns the single most important portrait failure -- focus on the ear, not the eye
-- into a passing grade.

YuNet needs a model file, which is fetched once with ``photocull fetch-models``
and never touched again. Until it exists this detector reports itself
unavailable, with the reason, and the chain falls through to saliency.
"""

from __future__ import annotations

import numpy as np

from ..assets import YUNET
from ..models import Box, Confidence, Detection
from .base import DetectionContext, not_found

# YuNet's raw rows are [x, y, w, h, then five landmark xy pairs, then score].
_LANDMARK_OFFSET = 4
_RIGHT_EYE = _LANDMARK_OFFSET
_LEFT_EYE = _LANDMARK_OFFSET + 2
_SCORE = 14

# Padding around the eye pair, as a fraction of the distance between the eyes.
# Enough to cover both eyes and the bridge of the nose without reaching hair.
_EYE_PAD = 0.35


class FaceDetector:
    """Largest-face subject detection, preferring the eye region."""

    name = "face"

    def __init__(self, prefer_eyes: bool = True, min_size_fraction: float = 0.05, score: float = 0.9) -> None:
        self._prefer_eyes = prefer_eyes
        self._min_size_fraction = min_size_fraction
        self._score = score
        self._backend: str | None = None
        self._detector = None
        self._reason = ""

    # -- availability ----------------------------------------------------

    def available(self) -> tuple[bool, str]:
        if self._backend is not None:
            return True, ""
        if self._reason:
            return False, self._reason
        try:
            import cv2
        except ImportError:
            self._reason = "opencv-python is not installed"
            return False, self._reason

        # OpenCV chatters about backend selection on every model load. It is not
        # actionable and it would print once per worker process.
        try:
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
        except AttributeError:
            pass

        if hasattr(cv2, "FaceDetectorYN"):
            if not YUNET.present():
                self._reason = (
                    f"{YUNET.filename} not downloaded yet - run 'photocull fetch-models' "
                    f"(one file, {YUNET.size_bytes // 1024} KB, then never again)"
                )
                return False, self._reason
            self._backend = "yunet"
            return True, ""

        if hasattr(cv2, "CascadeClassifier"):
            path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            cascade = cv2.CascadeClassifier(path)
            if cascade.empty():
                self._reason = f"cascade file missing or unreadable: {path}"
                return False, self._reason
            self._detector = cascade
            self._backend = "haar"
            return True, ""

        self._reason = f"opencv {cv2.__version__} exposes no usable face detector"
        return False, self._reason

    # -- backends --------------------------------------------------------

    def _detect_yunet(self, gray: np.ndarray) -> np.ndarray:
        import cv2

        height, width = gray.shape
        if self._detector is None:
            self._detector = cv2.FaceDetectorYN.create(
                str(YUNET.path()), "", (width, height), self._score, 0.3, 5000
            )
        self._detector.setInputSize((width, height))
        # YuNet expects three channels; the analysis works in luma, so the grey
        # frame is replicated rather than reloading and re-decoding the file.
        bgr = np.repeat(gray[:, :, None], 3, axis=2)
        _, faces = self._detector.detect(bgr)
        return faces if faces is not None else np.empty((0, 15), dtype=np.float32)

    def _eye_box_from_landmarks(self, row: np.ndarray, width: int, height: int) -> Box | None:
        right = (float(row[_RIGHT_EYE]), float(row[_RIGHT_EYE + 1]))
        left = (float(row[_LEFT_EYE]), float(row[_LEFT_EYE + 1]))
        separation = float(np.hypot(left[0] - right[0], left[1] - right[1]))
        if separation <= 1.0:
            return None

        pad = separation * _EYE_PAD
        x0 = min(right[0], left[0]) - pad
        x1 = max(right[0], left[0]) + pad
        y0 = min(right[1], left[1]) - pad
        y1 = max(right[1], left[1]) + pad
        return Box(x0 / width, y0 / height, (x1 - x0) / width, (y1 - y0) / height).clipped()

    def _detect_haar(self, gray: np.ndarray) -> list[tuple[int, int, int, int]]:
        height, width = gray.shape
        minimum = max(int(min(width, height) * self._min_size_fraction), 20)
        found = self._detector.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(minimum, minimum)
        )
        return [tuple(int(v) for v in face) for face in found]

    # -- interface -------------------------------------------------------

    def detect(self, context: DetectionContext) -> Detection:
        usable, reason = self.available()
        if not usable:
            return not_found(self.name, reason)

        gray = (np.clip(context.luma, 0, 1) * 255).astype(np.uint8)
        height, width = gray.shape

        if self._backend == "yunet":
            faces = self._detect_yunet(gray)
            if len(faces) == 0:
                return not_found(self.name, "no face found")

            # Largest face: the subject in a portrait, the nearest person in a
            # group shot -- which is the one focus was meant for either way.
            row = max(faces, key=lambda f: float(f[2]) * float(f[3]))
            if float(row[2]) < width * self._min_size_fraction:
                return not_found(self.name, "only faces too small to judge")

            if self._prefer_eyes:
                eyes = self._eye_box_from_landmarks(row, width, height)
                if eyes is not None:
                    return Detection(
                        box=eyes,
                        source="face+eyes",
                        confidence=Confidence.HIGH,
                        note=f"eye region from landmarks; largest of {len(faces)} face(s), "
                        f"score {float(row[_SCORE]):.2f}",
                    )

            box = Box(
                float(row[0]) / width, float(row[1]) / height,
                float(row[2]) / width, float(row[3]) / height,
            ).clipped()
            return Detection(
                box=box,
                source=self.name,
                confidence=Confidence.MEDIUM,
                note=f"largest of {len(faces)} face(s); eye landmarks unusable",
            )

        faces = self._detect_haar(gray)
        if not faces:
            return not_found(self.name, "no face found")
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        box = Box(x / width, y / height, w / width, h / height).clipped()
        return Detection(
            box=box,
            source=self.name,
            confidence=Confidence.MEDIUM,
            note=f"largest of {len(faces)} face(s) (haar cascade; no landmarks)",
        )
