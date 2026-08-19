"""Analysing one photograph: the composition root for all the measurements.

This module is the only place that knows the order of operations -- load, find
the subject, measure sharpness, measure exposure, characterise blur, hash for
grouping. Each of those lives behind its own interface, so this file stays a
short piece of orchestration rather than a place where logic accumulates.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from . import exif
from .config import Config
from .detect import AFScan, DetectionContext, SubjectDetector, build_chain
from .errors import PhotocullError
from .grouping import difference_hash
from .loading import LoaderRegistry, build_registry
from .metrics import blur, exposure, sharpness
from .models import (
    BlurMetrics,
    CaptureInfo,
    Confidence,
    Detection,
    ExposureMetrics,
    PhotoReport,
    SharpnessMetrics,
)


@dataclass(slots=True)
class AnalysisResult:
    """A finished report plus the hash grouping needs, which is not report data."""

    report: PhotoReport
    fingerprint: np.ndarray | None


def _encode_thumbnail(image: Image.Image) -> str:
    """Encode a thumbnail as a data URI for the self-contained contact sheet."""
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=78, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _failed_report(path: Path, message: str) -> PhotoReport:
    """A placeholder report for a file that could not be analysed.

    Failures become rows rather than exceptions: one unreadable file in a folder
    of eight hundred should be visible in the report, not fatal to the run.
    """
    empty_sharpness = SharpnessMetrics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, None, None, None)
    return PhotoReport(
        path=str(path),
        filename=path.name,
        width=0,
        height=0,
        sharpness=empty_sharpness,
        exposure=ExposureMetrics(0.0, 0.0, 0.0, 0.0, 0.0),
        blur=BlurMetrics(0.0, 0.0, "unknown"),
        detection=Detection(None, "none", Confidence.NONE, "file not analysed"),
        capture=CaptureInfo(),
        error=message,
    )


def build_detector_chain(
    config: Config, root: Path | None = None, af_scan: AFScan | None = None
) -> SubjectDetector:
    """Construct the subject chain a config asks for.

    One place assembles these arguments, so the analyser, the parallel pipeline
    and ``doctor`` cannot drift into building slightly different chains from the
    same configuration file.
    """
    return build_chain(
        config.subject.detectors,
        root=root or Path.cwd(),
        sidecar=Path(config.subject.sidecar),
        zone=config.subject.zone,
        prefer_eyes=config.subject.prefer_eyes,
        face_score=config.subject.face_score,
        face_min_size=config.subject.face_min_size,
        af_scan=af_scan,
    )


class Analyzer:
    """Turns one file into one :class:`AnalysisResult`.

    Holds the loader registry and detector chain so both are constructed once
    per process rather than once per photograph -- which matters, because
    building an OpenCV cascade is far more expensive than using it.
    """

    def __init__(
        self,
        config: Config,
        registry: LoaderRegistry | None = None,
        detector: SubjectDetector | None = None,
        root: Path | None = None,
        af_scan: AFScan | None = None,
    ) -> None:
        self._config = config
        self._registry = registry or build_registry(config.input.prefer_raw_decode)
        self._detector = detector or build_detector_chain(config, root, af_scan)

    @property
    def detector(self) -> SubjectDetector:
        return self._detector

    def analyse(self, path: Path, want_thumbnail: bool = True) -> AnalysisResult:
        try:
            return self._analyse(path, want_thumbnail)
        except PhotocullError as error:
            return AnalysisResult(_failed_report(path, str(error)), None)
        except Exception as error:  # a corrupt file should not end the run
            return AnalysisResult(_failed_report(path, f"unexpected failure: {error!r}"), None)

    def _analyse(self, path: Path, want_thumbnail: bool) -> AnalysisResult:
        config = self._config
        loaded = self._registry.load(path, config.input.working_edge, config.input.thumbnail_edge)

        context = DetectionContext(
            luma=loaded.luma,
            path=str(path),
            directories=tuple(loaded.directories),
            orientation=loaded.orientation,
        )
        detection = self._detector.detect(context)

        sharpness_map = sharpness.build_map(loaded.luma, config.sharpness.grid_long_edge)
        sharpness_metrics = sharpness.measure(
            sharpness_map,
            detection.box,
            config.sharpness.sharp_fraction_threshold,
            config.sharpness.min_background_acutance,
        )
        exposure_metrics = exposure.measure(loaded.luma, config.exposure.percentile)
        blur_metrics = blur.measure(
            loaded.luma,
            sharpness_metrics.max_local_acutance,
            config.sharpness.sharp_acutance,
        )
        capture = exif.extract(loaded.directories, loaded.exif)

        report = PhotoReport(
            path=str(path),
            filename=path.name,
            width=loaded.original_width,
            height=loaded.original_height,
            sharpness=sharpness_metrics,
            exposure=exposure_metrics,
            blur=blur_metrics,
            detection=detection,
            capture=capture,
            tile_map=sharpness_map.as_list() if config.output.include_tile_map else None,
            thumbnail_uri=_encode_thumbnail(loaded.thumbnail) if want_thumbnail else None,
            reasons=(f"loaded via {loaded.loader}",),
        )

        fingerprint = (
            difference_hash(loaded.luma, config.grouping.hash_size)
            if config.grouping.enabled
            else None
        )
        return AnalysisResult(report, fingerprint)
