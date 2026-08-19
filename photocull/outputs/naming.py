"""Naming per-frame output files without losing one to another.

A photo library is a tree, and a tree is exactly where basenames stop being
unique. Two subfolders in one shoot each holding a ``DSC_0001.NEF`` is not an
edge case -- it is the normal result of a card-per-day workflow, and it is the
reason this tool is recursive by default. Any writer that names an output after
``report.filename`` alone will silently overwrite one frame's answer with
another's, report success, and leave nothing behind to notice it by.

Two shapes of answer live here because two shapes of output want different
things:

* **Mirrored** for anything a photo-editing program has to pair back up with the
  original. An XMP sidecar is only a sidecar because it is named after the file
  it describes; renaming it to ``00042-DSC_0001.xmp`` resolves the collision and
  breaks the one property that made the format worth writing. Mirroring the
  source tree's relative path keeps the name and moves the uniqueness into the
  folder, where it came from in the first place.
* **Indexed** for anything referenced only from inside our own output. A contact
  sheet thumbnail is reached through a URL the page itself wrote, so nothing
  cares what it is called as long as no two are called the same.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Sequence

# Windows forbids these outright; the rest of the world merely makes them
# painful. Replaced rather than rejected, because a filename is data.
_UNSAFE = '<>:"/\\|?*'


def safe_part(name: str) -> str:
    """One path component, safe on every filesystem, still recognisable."""
    cleaned = "".join("-" if c in _UNSAFE or ord(c) < 32 else c for c in name).strip(" .")
    return cleaned[:80] or "frame"


def slug(name: str) -> str:
    """A flat, conservative name from a filename, dropping its extension."""
    kept = "".join(c if c.isalnum() or c in "-_." else "-" for c in Path(name).stem)
    return kept[:60] or "frame"


def _common_root(paths: Sequence[str]) -> Path | None:
    """The deepest folder every path sits under, or ``None`` if there isn't one.

    Two drives on Windows have no common root at all, and a run can legitimately
    span them, so this has to be allowed to fail.
    """
    parents = [str(Path(p).parent) for p in paths]
    if not parents:
        return None
    try:
        return Path(os.path.commonpath(parents))
    except ValueError:
        return None


def mirrored_names(paths: Sequence[str], suffix: str = "") -> list[str]:
    """Relative names mirroring the source tree, one per path, all distinct.

    ``suffix`` is appended whole, so an XMP sidecar asks for ``".xmp"`` and gets
    ``DSC_0001.NEF.xmp`` -- extension included, which is what every program that
    reads sidecars expects to find beside the original.

    Paths that share no common root, or that escape it, fall back to their
    basename and are disambiguated by a counter. That is worse than mirroring
    and better than losing a frame.
    """
    root = _common_root(paths)
    used: set[str] = set()
    names: list[str] = []
    for path in paths:
        candidate = Path(path)
        relative: Path | None = None
        if root is not None:
            try:
                relative = candidate.relative_to(root)
            except ValueError:
                relative = None
        parts = [safe_part(part) for part in (relative.parts if relative else (candidate.name,))]
        name = str(PurePosixPath(*parts)) + suffix

        # Sanitising can map two distinct names onto one, and the fallback above
        # can too. Neither is common; both are silent if left alone.
        if name in used:
            stem, dot, extension = name.partition(".")
            counter = 2
            while f"{stem}-{counter}{dot}{extension}" in used:
                counter += 1
            name = f"{stem}-{counter}{dot}{extension}"
        used.add(name)
        names.append(name)
    return names


def indexed_names(names: Sequence[str], suffix: str) -> list[str]:
    """Flat ``00000-name`` names, unique by construction.

    For output referenced only from our own pages, where the name carries no
    meaning a program depends on and the index is cheaper than a tree.
    """
    return [f"{index:05d}-{slug(name)}{suffix}" for index, name in enumerate(names)]
