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
from .tiffreader import TiffDirectory, find_largest_preview, frame_size, read_preview_bytes

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
    # The EXIF orientation as the file stores it, kept even though ``luma`` has
    # already been rotated to match. Autofocus coordinates are recorded in
    # unrotated sensor space, so placing that box on the rotated luma needs to
    # know which way round the file was.
    orientation: int | None = None
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


# EXIF tag numbers spelled out here rather than imported from tiffreader,
# because this path talks to Pillow's mapping rather than to a parsed TIFF tree.
_TAG_ORIENTATION = 0x0112
_TAG_EXIF_IFD = 0x8769
_TAG_GPS_IFD = 0x8825

# Orientations 5-8 all involve a quarter turn, so for those the stored pixel
# dimensions describe the frame with its sides swapped relative to how it is
# viewed -- and viewed is how everything downstream measures it.
_TRANSPOSING_ORIENTATIONS = frozenset({5, 6, 7, 8})


def _flat_exif(image: Image.Image) -> dict[int, object]:
    """Pillow's EXIF with the sub-IFDs merged in.

    ``getexif()`` returns IFD0 alone, and IFD0 holds almost nothing this tool
    wants: shutter speed, aperture, ISO, capture time, focal length and lens
    name all live in the Exif sub-IFD that tag 0x8769 points at. Without
    following that pointer a JPEG reports a camera name and nothing else, which
    silently disables the handholding-rule figure, ``explain``'s capture block
    and time-gap grouping -- a documented knob that works on raw files and
    quietly does nothing on JPEGs is worse than one that does not exist.

    Flattened into one mapping because that is the shape ``exif.extract``
    already reduces parsed raw directories to. Both sources then merge the same
    way and nothing downstream has to know which kind of file it came from.
    """
    try:
        source = image.getexif()
    except Exception:  # a truncated or malformed APP1 segment is not fatal
        return {}
    merged: dict[int, object] = dict(source)
    for pointer in (_TAG_EXIF_IFD, _TAG_GPS_IFD):
        try:
            sub = source.get_ifd(pointer)
        except Exception:
            continue
        # setdefault, not update: IFD0's camera identity must not be overwritten
        # by a sub-directory that happens to reuse a tag number. Same rule
        # exif._merge applies to raw directories.
        for tag, value in sub.items():
            merged.setdefault(tag, value)
    # The pointers themselves are file offsets, not measurements.
    for pointer in (_TAG_EXIF_IFD, _TAG_GPS_IFD):
        merged.pop(pointer, None)
    return merged


def _orientation_value(value: object) -> int | None:
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 8:
        return value
    return None


def _orientation_of(exif: dict[int, object]) -> int | None:
    return _orientation_value(exif.get(_TAG_ORIENTATION))


def _orientation_from_directories(directories: Sequence[TiffDirectory]) -> int | None:
    for directory in directories:
        found = _orientation_value(directory.get(_TAG_ORIENTATION))
        if found is not None:
            return found
    return None


# Orientation value to the transposition that turns stored pixels into viewed
# ones. The same table ``ImageOps.exif_transpose`` uses; spelled out because the
# raw path has to apply it from a tag Pillow cannot see.
_TRANSPOSITIONS = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def _apply_orientation(image: Image.Image, orientation: int | None) -> Image.Image:
    """Rotate stored pixels into viewing position, from an explicit value.

    ``ImageOps.exif_transpose`` reads the tag off the image, which is no use
    when the orientation was recorded in the raw container rather than in the
    embedded preview we just decoded.
    """
    operation = _TRANSPOSITIONS.get(orientation or 1)
    return image.transpose(operation) if operation is not None else image


def _viewed_size(width: int, height: int, orientation: int | None) -> tuple[int, int]:
    """Stored dimensions restated as the photograph is actually seen.

    The luma every metric runs on has been through ``exif_transpose``. Reporting
    the stored dimensions beside it would describe a landscape frame whose
    analysis ran on a portrait one -- wrong in the CSV, the JSON and the header
    line of ``explain``.
    """
    if orientation in _TRANSPOSING_ORIENTATIONS:
        return height, width
    return width, height


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
                # Read before the transpose, which drops the Orientation tag
                # from the image it returns -- right for the pixels, and it
                # would take with it the one tag still needed here.
                exif = _flat_exif(image)
                orientation = _orientation_of(exif)
                image = ImageOps.exif_transpose(image) or image
                luma = _to_luma(image, working_edge)
                thumbnail = _make_thumbnail(image, thumbnail_edge)
        except (OSError, ValueError) as exc:
            raise UnreadableImage(f"{path.name}: {exc}") from exc

        width, height = _viewed_size(width, height, orientation)
        return LoadedImage(
            path, luma, thumbnail, width, height, self.name, exif=exif, orientation=orientation
        )


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

        # Bodies differ over whether the embedded preview repeats the
        # orientation tag. Where it does not, the container's IFD0 still holds
        # it, and a portrait frame would otherwise be measured, thumbnailed and
        # reported on its side.
        container_orientation = _orientation_from_directories(directories)
        try:
            payload = read_preview_bytes(path, location)
            with Image.open(io.BytesIO(payload)) as image:
                width, height = image.size
                image.draft("RGB", (working_edge, working_edge))
                orientation = _orientation_of(_flat_exif(image))
                if orientation is None:
                    orientation = container_orientation
                image = _apply_orientation(image, orientation)
                luma = _to_luma(image, working_edge)
                thumbnail = _make_thumbnail(image, thumbnail_edge)
        except (OSError, ValueError) as exc:
            raise UnreadableImage(f"{path.name}: preview would not decode ({exc})") from exc

        # The preview is not always the full frame, and the report's dimensions
        # are meant to describe the photograph, not the JPEG we happened to read.
        declared = frame_size(path, directories)
        if declared and declared[0] * declared[1] > width * height:
            width, height = declared

        # Both the preview's size and the declared frame size are recorded in
        # sensor orientation, so the swap applies to whichever of them won.
        width, height = _viewed_size(width, height, orientation)
        return LoadedImage(
            path,
            luma,
            thumbnail,
            width,
            height,
            self.name,
            directories=directories,
            orientation=orientation,
        )


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
        # rawpy honours the camera's flip while postprocessing, so these
        # dimensions already describe the viewed frame and must not be swapped a
        # second time. The orientation is still recorded, because autofocus
        # coordinates are sensor-space whichever loader produced the pixels.
        return LoadedImage(
            path,
            luma,
            thumbnail,
            width * 2,
            height * 2,
            self.name,
            directories=directories,
            orientation=_orientation_from_directories(directories),
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
