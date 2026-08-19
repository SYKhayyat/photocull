"""Configuration: every threshold, weight and rule the tool obeys.

Nothing in this package hardcodes an aesthetic judgement. What counts as sharp,
which detector to prefer, how near-duplicates are grouped and what earns five
stars all live here, and all of them are overridable from a TOML file.

The defaults below are chosen to be defensible, not authoritative. They are a
starting point you are expected to disagree with -- which is why ``photocull
init`` writes them out as a commented file rather than hiding them in the code.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import ConfigError

DEFAULT_CONFIG_NAME = "photocull.toml"


def _check_keys(section: str, data: Mapping[str, Any], allowed: Sequence[str]) -> None:
    """Reject unknown keys loudly.

    A silently ignored typo in a config file is the worst kind of bug: the tool
    runs, reports success, and obeys a setting you did not write.
    """
    # A section that is not a table at all -- ``input = 5`` -- would otherwise
    # fail on ``set(data)`` with a TypeError, which main() does not catch and
    # which names neither the section nor the value.
    if not isinstance(data, Mapping):
        raise ConfigError(f"[{section}] must be a table, got {data!r}")
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ConfigError(
            f"[{section}] has unknown key(s): {', '.join(unknown)}. "
            f"Valid keys: {', '.join(sorted(allowed))}"
        )


def _positive(section: str, name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"[{section}].{name} must be a positive integer, got {value!r}")
    return value


def _fraction(section: str, name: str, value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0.0 <= value <= 1.0:
        raise ConfigError(f"[{section}].{name} must be between 0 and 1, got {value!r}")
    return float(value)


def _suffixes(value: Any) -> tuple[str, ...]:
    """Normalise a list of file extensions, leading dot optional.

    Every other malformed value in this module produces a ConfigError naming the
    section, the key and the offending value. A non-string in here used to
    escape as a bare AttributeError from ``str.startswith``, which main() does
    not catch, so a one-character config mistake surfaced as a traceback.
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"[input].include_suffixes must be a list of extensions, got {value!r}")
    suffixes: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip(". "):
            raise ConfigError(
                f"[input].include_suffixes must contain extensions like '.nef', got {entry!r}"
            )
        cleaned = entry.strip().lower()
        suffixes.append(cleaned if cleaned.startswith(".") else f".{cleaned}")
    return tuple(suffixes)


@dataclass(frozen=True, slots=True)
class InputConfig:
    """How files are found and decoded."""

    working_edge: int = 1024
    thumbnail_edge: int = 320
    recursive: bool = True
    prefer_raw_decode: bool = False
    include_suffixes: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "InputConfig":
        _check_keys("input", data, [f.name for f in cls.__dataclass_fields__.values()])
        base = cls()
        return cls(
            working_edge=_positive("input", "working_edge", data.get("working_edge", base.working_edge)),
            thumbnail_edge=_positive(
                "input", "thumbnail_edge", data.get("thumbnail_edge", base.thumbnail_edge)
            ),
            recursive=bool(data.get("recursive", base.recursive)),
            prefer_raw_decode=bool(data.get("prefer_raw_decode", base.prefer_raw_decode)),
            include_suffixes=_suffixes(data.get("include_suffixes", base.include_suffixes)),
        )


@dataclass(frozen=True, slots=True)
class SharpnessConfig:
    """Tuning for the acutance pass."""

    grid_long_edge: int = 24
    sharp_fraction_threshold: float = 0.5
    sharp_acutance: float = 40.0
    min_background_acutance: float = 2.0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SharpnessConfig":
        _check_keys("sharpness", data, [f.name for f in cls.__dataclass_fields__.values()])
        base = cls()
        sharp = data.get("sharp_acutance", base.sharp_acutance)
        if not isinstance(sharp, (int, float)) or sharp <= 0:
            raise ConfigError(f"[sharpness].sharp_acutance must be positive, got {sharp!r}")
        floor = data.get("min_background_acutance", base.min_background_acutance)
        if not isinstance(floor, (int, float)) or floor < 0:
            raise ConfigError(f"[sharpness].min_background_acutance must be >= 0, got {floor!r}")
        return cls(
            min_background_acutance=float(floor),
            grid_long_edge=_positive(
                "sharpness", "grid_long_edge", data.get("grid_long_edge", base.grid_long_edge)
            ),
            sharp_fraction_threshold=_fraction(
                "sharpness",
                "sharp_fraction_threshold",
                data.get("sharp_fraction_threshold", base.sharp_fraction_threshold),
            ),
            sharp_acutance=float(sharp),
        )


@dataclass(frozen=True, slots=True)
class ExposureConfig:
    """Where the tonal range is sampled from.

    ``dynamic_range`` and the tonal spread are taken from percentiles rather
    than from the extremes, so that one hot pixel or a single specular highlight
    does not define the range of a whole photograph. How far in to move is a
    real judgement -- 0.5 ignores the extreme half-percent at each end, higher
    values ignore more -- and it was reachable only by editing the source until
    this section existed.
    """

    percentile: float = 0.5

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExposureConfig":
        _check_keys("exposure", data, [f.name for f in cls.__dataclass_fields__.values()])
        base = cls()
        value = data.get("percentile", base.percentile)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0.0 <= value < 50.0:
            raise ConfigError(
                f"[exposure].percentile must be at least 0 and below 50, got {value!r}"
            )
        return cls(percentile=float(value))


@dataclass(frozen=True, slots=True)
class SubjectConfig:
    """Which detectors to use, in which order."""

    detectors: tuple[str, ...] = ("manual", "af-point", "face", "saliency")
    zone: str = "center"
    prefer_eyes: bool = True
    sidecar: str = "photocull-subjects.json"
    face_score: float = 0.9
    face_min_size: float = 0.05

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SubjectConfig":
        _check_keys("subject", data, [f.name for f in cls.__dataclass_fields__.values()])
        base = cls()
        detectors = data.get("detectors", base.detectors)
        if isinstance(detectors, str):
            detectors = [detectors]
        if not isinstance(detectors, (list, tuple)) or not detectors:
            raise ConfigError("[subject].detectors must be a non-empty list of names")

        # Values, not just keys. _check_keys exists so a typo cannot be silently
        # obeyed; a typo in a *value* was still reaching build_chain, which on
        # the parallel path runs first inside the pool initialiser -- so a
        # misspelt zone came back as a BrokenProcessPool traceback at the
        # default worker count and as a clear sentence at -j1.
        from .detect import DETECTOR_NAMES
        from .detect.simple import ZONES

        names = tuple(str(name) for name in detectors)
        for name in names:
            if name not in DETECTOR_NAMES:
                raise ConfigError(
                    f"[subject].detectors has unknown detector '{name}'; "
                    f"known: {', '.join(DETECTOR_NAMES)}"
                )
        zone = str(data.get("zone", base.zone))
        if zone not in ZONES:
            raise ConfigError(f"[subject].zone is unknown: '{zone}'; choose from {sorted(ZONES)}")

        return cls(
            detectors=names,
            zone=zone,
            prefer_eyes=bool(data.get("prefer_eyes", base.prefer_eyes)),
            sidecar=str(data.get("sidecar", base.sidecar)),
            face_score=_fraction("subject", "face_score", data.get("face_score", base.face_score)),
            face_min_size=_fraction(
                "subject", "face_min_size", data.get("face_min_size", base.face_min_size)
            ),
        )


@dataclass(frozen=True, slots=True)
class GroupingConfig:
    """How near-duplicate frames are gathered so they can be ranked together.

    Visual similarity rather than capture time, because not everyone shoots
    bursts. Two frames of the same composition ten minutes apart are still a
    choice between two frames.
    """

    enabled: bool = True
    hash_size: int = 8
    max_distance: int = 10
    max_time_gap_seconds: float = 0.0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GroupingConfig":
        _check_keys("grouping", data, [f.name for f in cls.__dataclass_fields__.values()])
        base = cls()
        distance = data.get("max_distance", base.max_distance)
        if not isinstance(distance, int) or isinstance(distance, bool) or distance < 0:
            raise ConfigError(f"[grouping].max_distance must be >= 0, got {distance!r}")
        gap = data.get("max_time_gap_seconds", base.max_time_gap_seconds)
        if not isinstance(gap, (int, float)) or gap < 0:
            raise ConfigError(f"[grouping].max_time_gap_seconds must be >= 0, got {gap!r}")
        return cls(
            enabled=bool(data.get("enabled", base.enabled)),
            hash_size=_positive("grouping", "hash_size", data.get("hash_size", base.hash_size)),
            max_distance=distance,
            max_time_gap_seconds=float(gap),
        )


@dataclass(frozen=True, slots=True)
class RatingRule:
    """One ``when`` expression and the stars and label it awards."""

    when: str
    stars: int | None = None
    label: str | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RatingConfig:
    """Ordered rules turning measurements into a rating. First match wins.

    Ordered and first-match rather than a weighted sum, because a weighted score
    cannot explain itself. A rule that fired has a name and a reason, and the
    contact sheet can print both.
    """

    rank_by: str = "subject_or_max_acutance"
    rules: tuple[RatingRule, ...] = ()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RatingConfig":
        _check_keys("rating", data, ["rank_by", "rules"])
        base = cls()
        rules: list[RatingRule] = []
        for index, raw in enumerate(data.get("rules", []) or []):
            if not isinstance(raw, Mapping) or "when" not in raw:
                raise ConfigError(f"[[rating.rules]] #{index + 1} needs a 'when' expression")
            _check_keys(f"rating.rules[{index + 1}]", raw, ["when", "stars", "label", "reason"])
            stars = raw.get("stars")
            if stars is not None and (not isinstance(stars, int) or not 0 <= stars <= 5):
                raise ConfigError(f"[[rating.rules]] #{index + 1}: stars must be 0-5, got {stars!r}")
            rules.append(
                RatingRule(
                    when=str(raw["when"]),
                    stars=stars,
                    label=str(raw["label"]) if raw.get("label") else None,
                    reason=str(raw.get("reason", "")),
                )
            )
        return cls(rank_by=str(data.get("rank_by", base.rank_by)), rules=tuple(rules))


@dataclass(frozen=True, slots=True)
class OutputConfig:
    """Which reports get written, and where."""

    formats: tuple[str, ...] = ("json", "csv", "html")
    directory: str = "photocull-report"
    include_tile_map: bool = False
    write_xmp_next_to_originals: bool = False
    open_html: bool = False
    # Above this many frames the contact sheet stops inlining its thumbnails and
    # writes them to a sibling folder instead. The single-file page is the whole
    # point of the format -- it opens off a USB stick in five years with no
    # server and no asset folder -- and it stays the default. But a thumbnail is
    # about 22 KB of base64, so a wedding at five thousand frames is a 113 MB
    # HTML file, and "portable" stops being true some way before "impossible".
    # A threshold rather than a flag, because the user this design protects is
    # exactly the one who does not read config files.
    self_contained_max_frames: int = 1500

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OutputConfig":
        _check_keys("output", data, [f.name for f in cls.__dataclass_fields__.values()])
        base = cls()
        formats = data.get("formats", base.formats)
        if isinstance(formats, str):
            formats = [formats]
        if not isinstance(formats, (list, tuple)) or not formats:
            raise ConfigError("[output].formats must be a non-empty list")
        return cls(
            formats=tuple(str(f) for f in formats),
            directory=str(data.get("directory", base.directory)),
            include_tile_map=bool(data.get("include_tile_map", base.include_tile_map)),
            write_xmp_next_to_originals=bool(
                data.get("write_xmp_next_to_originals", base.write_xmp_next_to_originals)
            ),
            open_html=bool(data.get("open_html", base.open_html)),
            self_contained_max_frames=_positive(
                "output",
                "self_contained_max_frames",
                data.get("self_contained_max_frames", base.self_contained_max_frames),
            ),
        )


@dataclass(frozen=True, slots=True)
class Config:
    """The complete, validated configuration for one run."""

    input: InputConfig = field(default_factory=InputConfig)
    sharpness: SharpnessConfig = field(default_factory=SharpnessConfig)
    exposure: ExposureConfig = field(default_factory=ExposureConfig)
    subject: SubjectConfig = field(default_factory=SubjectConfig)
    grouping: GroupingConfig = field(default_factory=GroupingConfig)
    rating: RatingConfig = field(default_factory=lambda: RatingConfig(rules=DEFAULT_RULES))
    output: OutputConfig = field(default_factory=OutputConfig)
    source_path: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], source: str | None = None) -> "Config":
        _check_keys(
            "top level",
            data,
            ["input", "sharpness", "exposure", "subject", "grouping", "rating", "output"],
        )
        rating_data = data.get("rating", {})
        rating = RatingConfig.from_dict(rating_data) if rating_data else RatingConfig(rules=DEFAULT_RULES)
        if not rating.rules:
            rating = replace(rating, rules=DEFAULT_RULES)
        return cls(
            input=InputConfig.from_dict(data.get("input", {})),
            sharpness=SharpnessConfig.from_dict(data.get("sharpness", {})),
            exposure=ExposureConfig.from_dict(data.get("exposure", {})),
            subject=SubjectConfig.from_dict(data.get("subject", {})),
            grouping=GroupingConfig.from_dict(data.get("grouping", {})),
            rating=rating,
            output=OutputConfig.from_dict(data.get("output", {})),
            source_path=source,
        )

    @classmethod
    def load(cls, path: Path | None) -> "Config":
        """Load a config file, or return defaults when ``path`` is ``None``."""
        if path is None:
            return cls()
        try:
            raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError(f"config file not found: {path}") from exc
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read config {path}: {exc}") from exc
        return cls.from_dict(raw, source=str(path))

    @classmethod
    def discover(cls, start: Path) -> "Config":
        """Use ``photocull.toml`` from the target folder or any parent, if present."""
        for directory in [start, *start.parents]:
            candidate = directory / DEFAULT_CONFIG_NAME
            if candidate.is_file():
                return cls.load(candidate)
        return cls()


# Two kinds of comparison live in this file, and keeping them apart is the whole
# design of the default ladder.
#
# Within a near-duplicate group -- same subject, same light, same framing --
# comparison is reliable, and so is subject-versus-background inside a single
# frame. Absolute thresholds across a whole library are not: what counts as
# "sharp" moves with content, lens and subject matter, which is why the README
# has a section explaining that the numbers are not absolutely comparable.
#
# There is a third trap underneath that one, and it is the reason these rules
# ask about `subject_confidence`. A subject-versus-background figure is only
# evidence about focus when the subject was located *independently of sharpness*.
# The saliency detector finds the subject by looking for the region of highest
# local contrast -- which is very nearly the same thing the sharpness map
# measures -- so asking "is the subject sharper than its background" about a
# saliency box is asking whether saliency worked, not whether focus landed. On a
# real 835-frame library the median subject/background ratio was 3.44 for
# detected faces and 6.92 for saliency boxes, and the relative acutance figure
# was 1.000 at the median for saliency: perfectly circular. So the verdicts that
# rest on the subject require a subject somebody other than the sharpness pass
# picked -- manual, autofocus metadata, or a detected face.
#
# Everything else falls through to group rank, which is independent of all of it.
#
# Rules with no ``stars`` are annotations: they attach a label and a reason and
# let the ladder continue. Highlight clipping is one, deliberately. The exposure
# module says of it "Reported, never scored ... that is the photographer's call,
# not this tool's", and a rule set that rejected a backlit portrait over a blown
# rim light would be contradicting that in the only place it matters.

# Confidence levels meaning "something other than the sharpness map put the box
# here". Written out rather than negating "low" because a frame with no subject
# at all reports "none", and that must not count as independent evidence.
_INDEPENDENT_SUBJECT = 'subject_confidence in ["high", "medium"]'

DEFAULT_RULES: tuple[RatingRule, ...] = (
    RatingRule(
        when="highlight_clipped > 0.05",
        label="yellow",
        reason="highlights clipped beyond recovery - your call whether that matters",
    ),
    RatingRule(
        when="max_local_acutance < 12",
        stars=1,
        label="red",
        reason="nothing in the frame is sharp",
    ),
    RatingRule(
        when=f"{_INDEPENDENT_SUBJECT} and subject_background_ratio < 0.9",
        stars=2,
        label="yellow",
        reason="background is sharper than the subject - focus missed",
    ),
    RatingRule(
        when=f"is_group_best and {_INDEPENDENT_SUBJECT} and subject_background_ratio >= 1.5",
        stars=5,
        label="green",
        reason="best frame of its group, and focus landed on the subject",
    ),
    RatingRule(
        when=f"{_INDEPENDENT_SUBJECT} and subject_background_ratio >= 3",
        stars=5,
        label="green",
        reason="subject clearly sharper than its background",
    ),
    RatingRule(
        when="is_group_best",
        stars=4,
        label="green",
        reason="best frame of its group",
    ),
    # The keeper path for a frame that has no near-duplicates to beat. Without
    # this a unique, well-focused photograph could never reach keepers.txt
    # except by clearing an absolute acutance bar, which is exactly the
    # comparison that does not hold across content.
    RatingRule(
        when=f"{_INDEPENDENT_SUBJECT} and subject_background_ratio >= 1.5",
        stars=4,
        label="green",
        reason="focus landed on the subject rather than behind it",
    ),
    # Lost to a near-duplicate. Not a reject -- just not the one to work on.
    RatingRule(
        when="group_size > 1 and not is_group_best",
        stars=3,
        reason="a near-duplicate beat this frame",
    ),
    RatingRule(when="max_local_acutance >= 25", stars=3, reason="acceptably sharp somewhere"),
    RatingRule(when="True", stars=2, reason="no rule matched strongly"),
)
