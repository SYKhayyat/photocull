"""Turning files on disk into the normalised arrays the metrics expect.

Loaders are registered strategies behind one interface, tried in priority order.
Adding support for a new container is writing a class and registering it; no
other module changes. That is the whole reason this indirection exists.

Two decisions here carry most of the value:

* **Raw files are read via their embedded JPEG preview by default.** Every raw
  container carries a camera-rendered JPEG, usually at full resolution. Reading
  it means touching two megabytes of a thirty megabyte file instead of
  demosaicing the sensor data, which is roughly two orders of magnitude faster
  and makes an 800 frame session practical.
* **Everything is measured at a fixed working resolution.** Acutance is scale
  dependent, so comparing a 45 megapixel frame against a 12 megapixel one is
  meaningless unless both are measured at the same size.

The preview is sharpened by the camera, so its absolute acutance runs higher
than a neutral raw conversion would. Since every comparison this tool makes is
between frames measured the same way, that offset cancels. The exception worth
knowing about: mixing preview-loaded raws and true raw conversions in one run
compares two different scales, which is why the loader used is recorded on every
report.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

import numpy as np
from PIL import Image, ImageOps

from .errors import UnreadableImage
from .tiffreader import TiffDirectory, find_largest_preview, read_preview_bytes

# Rec. 709 luma weights. Matching how a viewer perceives brightness matters
# because a channel-average turns a red subject on green foliage into mush and
# throws away the very edges we are trying to measure.
_LUMA_WEIGHTS = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

# Pillow refuses very large images by default as a denial-of-service guard. That
# guard is aimed at untrusted uploads; here the input is the user's own camera
# output, and a 100-megapixel embedded preview is a normal thing for a modern
# body to write. Raised rather than removed, so a genuinely absurd file is still
# refused instead of exhausting memory.
Image.MAX_IMAGE_PIXELS = 512_000_000

RAW_SUFFIXES = frozenset(
    {".nef", ".nrw", ".cr2", ".cr3", ".arw", ".srf", ".sr2", ".dng", ".raf", ".rw2", ".orf", ".pef"}
)
PLAIN_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp"})


@dataclass(slots=True)
class LoadedImage:
    """A decoded photograph, reduced to what the metrics actually need."""

    path: Path
    luma: np.ndarray
    thumbnail: Image.Image
    original_width: int
    original_height: int
    loader: str
    directories: Sequence[TiffDirectory] = field(default_factory=tuple)
    exif: dict[int, object] = field(default_factory=dict)


def _to_luma(image: Image.Image, working_edge: int) -> np.ndarray:
    """Downscale to the working resolution and return normalised luma."""
    width, height = image.size
    scale = working_edge / max(width, height)
    if scale < 1.0:
        target = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
        # BOX averages over the whole source region for each output pixel. Unlike
        # LANCZOS it adds no ringing, and ringing is indistinguishable from
        # acutance to a Laplacian -- it would manufacture sharpness that is not
        # in the photograph.
        image = image.resize(target, Image.Resampling.BOX)

    array = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    return array @ _LUMA_WEIGHTS


def _make_thumbnail(image: Image.Image, edge: int) -> Image.Image:
    thumb = image.convert("RGB").copy()
    thumb.thumbnail((edge, edge), Image.Resampling.BOX)
    return thumb


@runtime_checkable
class ImageLoader(Protocol):
    """Strategy for turning one file into a :class:`LoadedImage`."""

    name: str

    def can_load(self, path: Path) -> bool: ...

    def load(self, path: Path, working_edge: int, thumbnail_edge: int) -> LoadedImage: ...


class PlainImageLoader:
    """Loads anything Pillow opens directly: JPEG, PNG, TIFF, WebP."""

    name = "pillow"

    def can_load(self, path: Path) -> bool:
        return path.suffix.lower() in PLAIN_SUFFIXES

    def load(self, path: Path, working_edge: int, thumbnail_edge: int) -> LoadedImage:
        try:
            with Image.open(path) as image:
                # Recorded before draft(), which changes image.size as a side
                # effect -- reporting the drafted size as the file's dimensions
                # would be quietly, confidently wrong.
                width, height = image.size
                # draft() asks libjpeg to decode at a reduced DCT scale, which is
                # dramatically cheaper than decoding full size and then
                # discarding most of the pixels.
                image.draft("RGB", (working_edge, working_edge))
                image = ImageOps.exif_transpose(image) or image
                exif = dict(image.getexif())
                luma = _to_luma(image, working_edge)
                thumbnail = _make_thumbnail(image, thumbnail_edge)
        except (OSError, ValueError) as exc:
            raise UnreadableImage(f"{path.name}: {exc}") from exc

        return LoadedImage(path, luma, thumbnail, width, height, self.name, exif=exif)


class RawPreviewLoader:
    """Loads raw files through the full-size JPEG preview they already contain."""

    name = "raw-preview"

    def can_load(self, path: Path) -> bool:
        return path.suffix.lower() in RAW_SUFFIXES

    def load(self, path: Path, working_edge: int, thumbnail_edge: int) -> LoadedImage:
        try:
            location, directories = find_largest_preview(path)
        except (OSError, ValueError) as exc:
            raise UnreadableImage(f"{path.name}: not a readable raw container ({exc})") from exc
        if location is None:
            raise UnreadableImage(f"{path.name}: no embedded preview found")

        try:
            payload = read_preview_bytes(path, location)
            with Image.open(io.BytesIO(payload)) as image:
                width, height = image.size
                image.draft("RGB", (working_edge, working_edge))
                image = ImageOps.exif_transpose(image) or image
                luma = _to_luma(image, working_edge)
                thumbnail = _make_thumbnail(image, thumbnail_edge)
        except (OSError, ValueError) as exc:
            raise UnreadableImage(f"{path.name}: preview would not decode ({exc})") from exc

        return LoadedImage(path, luma, thumbnail, width, height, self.name, directories=directories)


class RawDecodeLoader:
    """Loads raw files by demosaicing the sensor data, when rawpy is installed.

    Slower than the preview path by a wide margin, and opt-in for that reason.
    Worth it when you want acutance measured on neutral data rather than on a
    camera-sharpened JPEG -- for instance when comparing bodies whose in-camera
    sharpening differs.
    """

    name = "raw-decode"

    def __init__(self) -> None:
        try:
            import rawpy  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise UnreadableImage("rawpy is not installed") from exc

    def can_load(self, path: Path) -> bool:
        return path.suffix.lower() in RAW_SUFFIXES

    def load(self, path: Path, working_edge: int, thumbnail_edge: int) -> LoadedImage:
        import rawpy

        try:
            with rawpy.imread(str(path)) as raw:
                # half_size demosaics at half linear resolution: four times less
                # work, and still far above the working resolution we measure at.
                rgb = raw.postprocess(half_size=True, no_auto_bright=True, use_camera_wb=True)
        except Exception as exc:  # rawpy raises its own hierarchy
            raise UnreadableImage(f"{path.name}: raw decode failed ({exc})") from exc

        image = Image.fromarray(rgb)
        width, height = image.size
        luma = _to_luma(image, working_edge)
        thumbnail = _make_thumbnail(image, thumbnail_edge)
        try:
            _, directories = find_largest_preview(path)
        except (OSError, ValueError):
            directories = []
        return LoadedImage(
            path, luma, thumbnail, width * 2, height * 2, self.name, directories=directories
        )


class LoaderRegistry:
    """Ordered collection of loaders; first one that accepts a path wins."""

    def __init__(self, loaders: Sequence[ImageLoader]) -> None:
        self._loaders = list(loaders)

    @property
    def names(self) -> list[str]:
        return [loader.name for loader in self._loaders]

    def load(self, path: Path, working_edge: int, thumbnail_edge: int) -> LoadedImage:
        for loader in self._loaders:
            if loader.can_load(path):
                return loader.load(path, working_edge, thumbnail_edge)
        raise UnreadableImage(f"{path.name}: no loader handles '{path.suffix}'")

    def handles(self, path: Path) -> bool:
        return any(loader.can_load(path) for loader in self._loaders)


def build_registry(prefer_raw_decode: bool = False) -> LoaderRegistry:
    """Assemble the default loader chain for this machine.

    When ``prefer_raw_decode`` is set but rawpy is missing, the preview loader
    still handles raw files. A missing optional dependency degrades the quality
    of the measurement; it never removes the ability to run.
    """
    loaders: list[ImageLoader] = []
    if prefer_raw_decode:
        try:
            loaders.append(RawDecodeLoader())
        except UnreadableImage:
            pass
    loaders.append(RawPreviewLoader())
    loaders.append(PlainImageLoader())
    return LoaderRegistry(loaders)
