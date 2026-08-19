"""Diffing two runs, which is what calibration actually consists of.

Changing a threshold and looking at the new numbers tells you almost nothing.
The question is always *what moved* -- which frames left the keeper list, which
rejects came back, whether the rule you loosened caught the twelve frames you
had in mind or four hundred you did not. Answering that today means two JSON
files and your own diff tool, and a text diff of a JSON report is unreadable by
construction: every frame's measurements shift a little, so the interesting five
lines arrive buried in eight hundred.

So the comparison is done here, over the report structure rather than over its
text, and it reports the two things a person changing a threshold wants:
verdicts that changed, and which direction they went.

Frames are matched on ``path``, falling back to ``filename``. Path is exact and
survives duplicate basenames; the fallback is what lets you compare a run from
before a folder was moved against one from after.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import ConfigError

# Thresholds the plain-list writers use, restated so a comparison speaks the
# same language as the files the user is actually looking at.
KEEPER_MINIMUM = 4
REJECT_MAXIMUM = 2


def load_report(path: Path) -> dict[str, Any]:
    """Read one ``photocull.json``, or fail saying which file and why."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"report not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read report {path}: {exc}") from exc
    if not isinstance(payload, Mapping) or "photos" not in payload:
        raise ConfigError(f"{path} is not a photocull JSON report (no 'photos' key)")
    return dict(payload)


def _index(photos: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    for photo in photos:
        key = str(photo.get("path") or photo.get("filename") or "")
        if key:
            index[key] = photo
    return index


def _bucket(rating: Any) -> str:
    """Which of the three lists a rating puts a frame on."""
    if rating is None:
        return "unrated"
    if rating >= KEEPER_MINIMUM:
        return "keeper"
    if rating <= REJECT_MAXIMUM:
        return "reject"
    return "middle"


def compare(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """Everything that changed between two runs, as plain data.

    Returned rather than printed, so the same comparison serves the console, the
    ``--json`` flag and a test without any of them reimplementing it.
    """
    left = _index(before.get("photos", []))
    right = _index(after.get("photos", []))

    shared = [key for key in right if key in left]
    changes: list[dict[str, Any]] = []
    movement: dict[str, int] = {}

    for key in shared:
        old, new = left[key], right[key]
        old_rating, new_rating = old.get("rating"), new.get("rating")
        if old_rating == new_rating:
            continue
        transition = f"{_bucket(old_rating)} -> {_bucket(new_rating)}"
        movement[transition] = movement.get(transition, 0) + 1
        changes.append(
            {
                "path": key,
                "filename": new.get("filename") or old.get("filename"),
                "from": old_rating,
                "to": new_rating,
                "delta": (new_rating or 0) - (old_rating or 0),
                "transition": transition,
                # The reason is the whole point: a rating that moved without a
                # rule change behind it is a measurement that moved, and those
                # are two very different things to be looking at.
                "because": list(new.get("reasons") or []),
            }
        )

    changes.sort(key=lambda change: (-abs(change["delta"]), str(change["filename"])))

    def bucket_counts(index: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
        counts = {"keeper": 0, "middle": 0, "reject": 0, "unrated": 0}
        for photo in index.values():
            counts[_bucket(photo.get("rating"))] += 1
        return counts

    return {
        "before": {"path": before.get("config_source"), "frames": len(left)},
        "after": {"path": after.get("config_source"), "frames": len(right)},
        "only_in_before": sorted(set(left) - set(right)),
        "only_in_after": sorted(set(right) - set(left)),
        "compared": len(shared),
        "changed": len(changes),
        "buckets": {"before": bucket_counts(left), "after": bucket_counts(right)},
        "movement": dict(sorted(movement.items(), key=lambda item: (-item[1], item[0]))),
        "changes": changes,
    }


def format_comparison(result: Mapping[str, Any], limit: int = 20) -> str:
    """The console rendering: totals first, then the frames that moved."""
    lines: list[str] = []
    before, after = result["buckets"]["before"], result["buckets"]["after"]
    lines.append(f"compared {result['compared']} frame(s) present in both runs")
    if result["only_in_before"]:
        lines.append(f"  {len(result['only_in_before'])} frame(s) only in the first run")
    if result["only_in_after"]:
        lines.append(f"  {len(result['only_in_after'])} frame(s) only in the second run")

    lines.append("")
    lines.append(f"  {'':10}{'before':>8}{'after':>8}{'change':>8}")
    for name in ("keeper", "middle", "reject", "unrated"):
        delta = after[name] - before[name]
        lines.append(f"  {name:10}{before[name]:>8}{after[name]:>8}{delta:>+8}")

    lines.append("")
    if not result["changed"]:
        lines.append("  no frame changed its rating")
        return "\n".join(lines)

    lines.append(f"  {result['changed']} frame(s) changed rating:")
    for transition, count in result["movement"].items():
        lines.append(f"    {count:>5}  {transition}")

    lines.append("")
    shown = list(result["changes"])[:limit]
    # Two shoots in one library can each hold a DSC_0001.NEF. They are matched
    # correctly -- on path -- but printing the bare name would show the same
    # line twice and leave the reader unable to tell which frame moved.
    seen: dict[str, int] = {}
    for change in result["changes"]:
        name = str(change["filename"])
        seen[name] = seen.get(name, 0) + 1

    lines.append(f"  biggest movers ({len(shown)} of {result['changed']}):")
    for change in shown:
        old = "-" if change["from"] is None else change["from"]
        new = "-" if change["to"] is None else change["to"]
        because = change["because"][-1] if change["because"] else ""
        name = str(change["filename"])
        if seen.get(name, 0) > 1:
            name = "/".join(Path(str(change["path"])).parts[-2:])
        lines.append(f"    {old} -> {new}  {name}")
        if because:
            lines.append(f"            {because}")
    return "\n".join(lines)


def compare_files(first: Path, second: Path) -> dict[str, Any]:
    """Load two report files and compare them."""
    return compare(load_report(first), load_report(second))
