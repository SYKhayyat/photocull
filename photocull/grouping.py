"""Gathering near-identical frames so they can be ranked against each other.

Grouping by *visual similarity* rather than capture time is a deliberate choice.
Timestamp clustering only finds bursts, and plenty of photographers do not shoot
bursts -- they take three considered frames of the same composition over two
minutes, which is exactly the same decision to make and completely invisible to
a time-based grouper.

Similarity is measured with a difference hash: shrink the frame, compare each
pixel with its right-hand neighbour, keep the sign. What survives is the coarse
gradient structure of the picture, which is stable under exposure changes, minor
recomposition and the difference between a raw preview and a JPEG, while still
separating two genuinely different photographs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

import numpy as np
from PIL import Image

# Popcount for every byte value, computed once. Turns Hamming distance over
# packed bits into an array lookup instead of a Python loop.
_POPCOUNT = np.array([bin(value).count("1") for value in range(256)], dtype=np.uint8)

# Cap on how many hashes are compared in one vectorised block. Keeps peak memory
# bounded regardless of how many photographs are in the run.
_CHUNK = 512


def difference_hash(luma: np.ndarray, hash_size: int = 8) -> np.ndarray:
    """Return a packed difference hash of a normalised luma image."""
    image = Image.fromarray((np.clip(luma, 0, 1) * 255).astype(np.uint8))
    small = np.asarray(
        image.resize((hash_size + 1, hash_size), Image.Resampling.BOX), dtype=np.int16
    )
    bits = small[:, 1:] > small[:, :-1]
    return np.packbits(bits.reshape(-1))


def hamming_distance(left: np.ndarray, right: np.ndarray) -> int:
    return int(_POPCOUNT[np.bitwise_xor(left, right)].sum())


@dataclass(slots=True)
class _DisjointSet:
    """Union-find, so 'A is like B' and 'B is like C' put all three together.

    Transitivity is the right behaviour here: a slow pan across a scene produces
    a chain of frames where neighbours match and the ends do not, and those are
    still one decision to make.
    """

    parent: list[int]

    @classmethod
    def of_size(cls, count: int) -> "_DisjointSet":
        return cls(parent=list(range(count)))

    def find(self, item: int) -> int:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:  # path compression
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def _parse_timestamp(value: str | None) -> float | None:
    """EXIF timestamps use 'YYYY:MM:DD HH:MM:SS'."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y:%m:%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def group_by_similarity(
    hashes: Sequence[np.ndarray],
    max_distance: int,
    timestamps: Sequence[str | None] | None = None,
    max_time_gap_seconds: float = 0.0,
) -> list[int]:
    """Assign a group id to every item; identical ids mean near-duplicates.

    ``max_time_gap_seconds`` is an optional extra constraint, not the primary
    signal: set it above zero to refuse to group visually similar frames taken
    hours apart, which matters for repeated subjects like a studio backdrop.
    """
    count = len(hashes)
    if count == 0:
        return []

    matrix = np.vstack([h.reshape(1, -1) for h in hashes])
    epochs = [_parse_timestamp(t) for t in timestamps] if timestamps else [None] * count

    groups = _DisjointSet.of_size(count)
    for start in range(0, count, _CHUNK):
        block = matrix[start : start + _CHUNK]
        # Compare this block against everything from its own start onwards; the
        # relation is symmetric, so the lower triangle would be wasted work.
        distances = _POPCOUNT[np.bitwise_xor(block[:, None, :], matrix[None, start:, :])].sum(axis=2)
        rows, cols = np.nonzero(distances <= max_distance)
        for row, col in zip(rows.tolist(), cols.tolist()):
            left, right = start + row, start + col
            if left >= right:
                continue
            if max_time_gap_seconds > 0:
                first, second = epochs[left], epochs[right]
                if first is not None and second is not None:
                    if abs(first - second) > max_time_gap_seconds:
                        continue
            groups.union(left, right)

    # Renumber so ids are small and stable in file order, which makes a report
    # sorted by group read sensibly instead of jumping around.
    canonical: dict[int, int] = {}
    result = []
    for index in range(count):
        root = groups.find(index)
        if root not in canonical:
            canonical[root] = len(canonical)
        result.append(canonical[root])
    return result
