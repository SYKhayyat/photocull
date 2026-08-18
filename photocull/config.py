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
            include_suffixes=tuple(
                s.lower() if s.startswith(".") else f".{s.lower()}"
                for s in data.get("include_suffixes", base.include_suffixes)
            ),
        )


@dataclass(frozen=True, slots=True)
class SharpnessConfig:
    """Tuning for the acutance pass."""

    grid_long_edge: int = 24
    sharp_fraction_threshold: float = 0.5
    sharp_acutance: float = 40.0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SharpnessConfig":
        _check_keys("sharpness", data, [f.name for f in cls.__dataclass_fields__.values()])
        base = cls()
        sharp = data.get("sharp_acutance", base.sharp_acutance)
        if not isinstance(sharp, (int, float)) or sharp <= 0:
            raise ConfigError(f"[sharpness].sharp_acutance must be positive, got {sharp!r}")
        return cls(
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
        return cls(
            detectors=tuple(str(name) for name in detectors),
            zone=str(data.get("zone", base.zone)),
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
        )


@dataclass(frozen=True, slots=True)
class Config:
    """The complete, validated configuration for one run."""

    input: InputConfig = field(default_factory=InputConfig)
    sharpness: SharpnessConfig = field(default_factory=SharpnessConfig)
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
            ["input", "sharpness", "subject", "grouping", "rating", "output"],
        )
        rating_data = data.get("rating", {})
        rating = RatingConfig.from_dict(rating_data) if rating_data else RatingConfig(rules=DEFAULT_RULES)
        if not rating.rules:
            rating = replace(rating, rules=DEFAULT_RULES)
        return cls(
            input=InputConfig.from_dict(data.get("input", {})),
            sharpness=SharpnessConfig.from_dict(data.get("sharpness", {})),
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


# The default rules deliberately never award five stars on sharpness alone: a
# perfectly focused frame with blown highlights is not a keeper, and a rule set
# that says otherwise trains you to ignore it.
DEFAULT_RULES: tuple[RatingRule, ...] = (
    RatingRule(
        when="max_local_acutance < 12",
        stars=1,
        label="red",
        reason="nothing in the frame is sharp",
    ),
    RatingRule(
        when="subject_found and subject_background_ratio is not None and subject_background_ratio < 0.9",
        stars=2,
        label="yellow",
        reason="background is sharper than the subject - focus missed",
    ),
    RatingRule(
        when="highlight_clipped > 0.05",
        stars=2,
        label="yellow",
        reason="highlights clipped beyond recovery",
    ),
    RatingRule(
        when="subject_found and subject_background_ratio >= 3 and max_local_acutance >= 25 "
        "and highlight_clipped <= 0.02",
        stars=5,
        label="green",
        reason="subject clearly sharper than its background, highlights intact",
    ),
    RatingRule(
        when="group_size > 1 and is_group_best",
        stars=4,
        label="green",
        reason="best frame of its group",
    ),
    RatingRule(when="max_local_acutance >= 25", stars=3, reason="acceptably sharp somewhere"),
    RatingRule(when="True", stars=2, reason="no rule matched strongly"),
)
