"""Locating and fetching the optional model files some detectors need.

Downloads are never automatic. A tool that quietly reaches out to the network
the first time you run it is a tool that behaves differently on a train, in a
locked-down environment, or in three years when the URL has rotted -- and the
whole premise here is that this keeps working. So a missing model is reported as
a named, actionable reason ("run photocull fetch-models") and the detector chain
falls through to something that does work.

Once fetched, the file is cached and nothing touches the network again.
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .errors import PhotocullError

_ENV_OVERRIDE = "PHOTOCULL_MODEL_DIR"


@dataclass(frozen=True, slots=True)
class ModelAsset:
    """One downloadable model file."""

    key: str
    filename: str
    url: str
    sha256: str
    description: str
    size_bytes: int

    def path(self) -> Path:
        return model_directory() / self.filename

    def present(self) -> bool:
        return self.path().is_file()


# YuNet: the compact face detector shipped by the OpenCV model zoo. Returns five
# facial landmarks alongside each box, which is what lets the face detector
# measure acutance on the eyes exactly rather than approximately.
YUNET = ModelAsset(
    key="yunet",
    filename="face_detection_yunet_2023mar.onnx",
    url=(
        "https://github.com/opencv/opencv_zoo/raw/main/models/"
        "face_detection_yunet/face_detection_yunet_2023mar.onnx"
    ),
    sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    description="YuNet face detector with eye landmarks",
    size_bytes=232589,
)

ASSETS: tuple[ModelAsset, ...] = (YUNET,)


def model_directory() -> Path:
    """Where cached models live. Overridable, because forced paths are rude."""
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        return Path(override).expanduser()
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base).expanduser() / "photocull" / "models"
    return Path.home() / ".cache" / "photocull" / "models"


def fetch(asset: ModelAsset, force: bool = False) -> Path:
    """Download ``asset`` into the cache and verify it. Returns the local path."""
    target = asset.path()
    if target.is_file() and not force:
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(asset.url, timeout=60) as response:
            payload = response.read()
    except OSError as exc:
        raise PhotocullError(f"could not download {asset.filename}: {exc}") from exc

    digest = hashlib.sha256(payload).hexdigest()
    if asset.sha256 and digest != asset.sha256:
        raise PhotocullError(
            f"{asset.filename} failed its checksum (expected {asset.sha256}, got {digest}). "
            "Refusing to install it."
        )

    temporary.write_bytes(payload)
    temporary.replace(target)  # atomic, so an interrupted fetch leaves no half-file
    return target
