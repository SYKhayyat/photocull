"""Assembling :class:`CaptureInfo` from whichever metadata source is available.

Raw files come with their directories already parsed by the loader; ordinary
images come with Pillow's EXIF mapping. Both are reduced to the same value
object here so nothing downstream has to care which kind of file it came from.
"""

from __future__ import annotations

from typing import Any, Sequence

from . import tiffreader as T
from .models import CaptureInfo
from .tiffreader import TiffDirectory


def _rational(value: Any) -> float | None:
    """Coerce a TIFF rational, Pillow rational, or plain number to a float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, tuple) and len(value) == 2:
        numerator, denominator = value
        if denominator:
            return float(numerator) / float(denominator)
        return None
    # Pillow returns IFDRational, which supports float() directly.
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    if isinstance(value, str):
        cleaned = value.strip().strip("\x00").strip()
        return cleaned or None
    return None


def _merge(directories: Sequence[TiffDirectory]) -> dict[int, Any]:
    """Flatten every directory into one tag mapping.

    Later directories win only where earlier ones are silent, so IFD0's camera
    identity is not overwritten by a preview sub-directory that happens to reuse
    a tag number.
    """
    merged: dict[int, Any] = {}
    for directory in directories:
        for tag, value in directory.items():
            merged.setdefault(tag, value)
    return merged


def from_tags(tags: dict[int, Any]) -> CaptureInfo:
    """Build capture info from a flat tag mapping."""
    make = _text(tags.get(T.TAG_MAKE))
    model = _text(tags.get(T.TAG_MODEL))
    if make and model and model.upper().startswith(make.upper().split()[0]):
        camera = model  # "NIKON CORPORATION" + "NIKON D750" should not double up
    else:
        camera = " ".join(part for part in (make, model) if part) or None

    # Panasonic writes no standard ISO tag in IFD0 and records it privately
    # instead, so an RW2 would otherwise report no sensitivity at all.
    iso = tags.get(T.TAG_ISO)
    if iso is None:
        iso = tags.get(T.TAG_PANASONIC_ISO)
    if isinstance(iso, list) and iso:
        iso = iso[0]

    orientation = tags.get(T.TAG_ORIENTATION)
    if isinstance(orientation, list) and orientation:
        orientation = orientation[0]

    return CaptureInfo(
        camera=camera,
        lens=_text(tags.get(T.TAG_LENS_MODEL)),
        iso=int(iso) if isinstance(iso, (int, float)) else None,
        aperture=_rational(tags.get(T.TAG_F_NUMBER)),
        shutter_seconds=_rational(tags.get(T.TAG_EXPOSURE_TIME)),
        focal_length=_rational(tags.get(T.TAG_FOCAL_LENGTH)),
        focal_length_35mm=_rational(tags.get(T.TAG_FOCAL_LENGTH_35MM)),
        timestamp=_text(tags.get(T.TAG_DATETIME_ORIGINAL)),
        orientation=int(orientation) if isinstance(orientation, (int, float)) else None,
    )


def extract(directories: Sequence[TiffDirectory], pillow_exif: dict[int, Any]) -> CaptureInfo:
    """Build capture info from whichever source has the tags.

    Raw directories are consulted first because they carry the complete set;
    Pillow's mapping fills the gaps and is the only source for plain JPEGs.
    """
    tags = _merge(directories)
    for tag, value in pillow_exif.items():
        tags.setdefault(tag, value)
    return from_tags(tags)
