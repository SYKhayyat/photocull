"""Running the analysis over a folder: discovery, parallelism, grouping, rating.

Order matters and is not arbitrary. Per-file measurement is embarrassingly
parallel and goes first. Grouping is inherently global -- it compares every
frame against every other -- so it happens once, in the parent, after the
workers are done. Ranking within a group depends on grouping, and rating depends
on rank, because "best frame of its group" is a thing a rule can ask about.
"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .analysis import Analyzer
from .config import Config
from .errors import ConfigError
from .grouping import group_by_similarity
from .loading import PLAIN_SUFFIXES, RAW_SUFFIXES
from .models import PhotoReport
from .rating import Expression, Rater

# Workers are processes, so each needs its own Analyzer. Building it in an
# initialiser means one construction per worker instead of one per photograph,
# which matters because loading an OpenCV cascade dwarfs the cost of using it.
_WORKER: Analyzer | None = None

ProgressCallback = Callable[[int, int, str], None]


def _init_worker(config: Config, root: str) -> None:
    global _WORKER
    _WORKER = Analyzer(config, root=Path(root))


def _analyse_one(path_text: str) -> tuple[PhotoReport, bytes | None, int]:
    assert _WORKER is not None, "worker was not initialised"
    result = _WORKER.analyse(Path(path_text))
    fingerprint = result.fingerprint
    # Hashes cross the process boundary as bytes: small, and it avoids pickling
    # a NumPy array for every single file.
    return result.report, (fingerprint.tobytes() if fingerprint is not None else None), (
        len(fingerprint) if fingerprint is not None else 0
    )


def discover(root: Path, config: Config) -> list[Path]:
    """Find candidate image files under ``root``, sorted for stable output."""
    if root.is_file():
        return [root]
    if not root.is_dir():
        raise ConfigError(f"not a file or directory: {root}")

    allowed = set(config.input.include_suffixes) or (RAW_SUFFIXES | PLAIN_SUFFIXES)
    pattern = "**/*" if config.input.recursive else "*"
    found = [
        path
        for path in root.glob(pattern)
        if path.is_file() and path.suffix.lower() in allowed
    ]
    return sorted(found)


def _rank_within_groups(reports: list[PhotoReport], rank_by: str) -> list[PhotoReport]:
    """Order each group best-first and record every frame's rank in it."""
    known = set(reports[0].flat_metrics()) if reports else set()
    expression = Expression(rank_by, sorted(known))

    # Keyed by (has_group, id) so an ungrouped frame -- which becomes a group of
    # one keyed by its own index -- can never collide with real group 0.
    by_group: dict[tuple[bool, int], list[int]] = {}
    for index, report in enumerate(reports):
        key = (True, report.group_id) if report.group_id is not None else (False, index)
        by_group.setdefault(key, []).append(index)

    ranked = list(reports)
    for indices in by_group.values():
        def score(index: int) -> float:
            value = expression.evaluate(ranked[index].flat_metrics())
            return float(value) if isinstance(value, (int, float)) else float("-inf")

        ordered = sorted(indices, key=score, reverse=True)
        for rank, index in enumerate(ordered):
            ranked[index] = replace(ranked[index], group_rank=rank, group_size=len(indices))
    return ranked


def run(
    root: Path,
    config: Config,
    workers: int | None = None,
    progress: ProgressCallback | None = None,
) -> list[PhotoReport]:
    """Analyse every image under ``root`` and return finished reports."""
    import numpy as np

    paths = discover(root, config)
    if not paths:
        return []

    worker_count = workers if workers is not None else min(os.cpu_count() or 1, 8)
    reports: list[PhotoReport] = []
    fingerprints: list[np.ndarray | None] = []

    if worker_count <= 1:
        analyzer = Analyzer(config, root=root)
        for index, path in enumerate(paths, start=1):
            result = analyzer.analyse(path)
            reports.append(result.report)
            fingerprints.append(result.fingerprint)
            if progress:
                progress(index, len(paths), path.name)
    else:
        with ProcessPoolExecutor(
            max_workers=worker_count, initializer=_init_worker, initargs=(config, str(root))
        ) as pool:
            stream = pool.map(_analyse_one, [str(p) for p in paths], chunksize=4)
            for index, (report, raw_hash, length) in enumerate(stream, start=1):
                reports.append(report)
                fingerprints.append(
                    np.frombuffer(raw_hash, dtype=np.uint8) if raw_hash else None
                )
                if progress:
                    progress(index, len(paths), report.filename)

    if config.grouping.enabled:
        usable = [i for i, f in enumerate(fingerprints) if f is not None]
        if usable:
            ids = group_by_similarity(
                [fingerprints[i] for i in usable],
                max_distance=config.grouping.max_distance,
                timestamps=[reports[i].capture.timestamp for i in usable],
                max_time_gap_seconds=config.grouping.max_time_gap_seconds,
            )
            for position, index in enumerate(usable):
                reports[index] = replace(reports[index], group_id=ids[position])

    reports = _rank_within_groups(reports, config.rating.rank_by)

    rater = Rater(config.rating.rules, sorted(reports[0].flat_metrics()))
    return [rater.apply(report) for report in reports]


def summarise(reports: Sequence[PhotoReport]) -> dict[str, object]:
    """Headline numbers for the console and the contact sheet header."""
    total = len(reports)
    failed = sum(1 for r in reports if r.error)
    analysed = total - failed
    groups = {r.group_id for r in reports if r.group_id is not None}
    duplicates = sum(1 for r in reports if r.group_size > 1)
    with_subject = sum(1 for r in reports if r.detection.found)
    return {
        "total": total,
        "analysed": analysed,
        "failed": failed,
        "groups": len(groups),
        "in_multi_frame_groups": duplicates,
        "subject_found": with_subject,
        "by_detector": _count(r.detection.source for r in reports if r.detection.found),
        "by_rating": _count(str(r.rating) for r in reports if r.rating is not None),
    }


def _count(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
