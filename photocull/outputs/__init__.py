"""Report writers and the registry that resolves them by name."""

from __future__ import annotations

from typing import Callable, Sequence

from ..errors import ConfigError
from .base import ReportWriter
from .contactsheet import ContactSheetWriter
from .machine import CsvWriter, JsonWriter, KeeperListWriter, RejectListWriter, XmpWriter

__all__ = [
    "ContactSheetWriter",
    "CsvWriter",
    "JsonWriter",
    "KeeperListWriter",
    "RejectListWriter",
    "ReportWriter",
    "WRITER_NAMES",
    "XmpWriter",
    "build_writers",
]

_REGISTRY: dict[str, Callable[[], ReportWriter]] = {
    "json": JsonWriter,
    "csv": CsvWriter,
    "html": ContactSheetWriter,
    "keepers": KeeperListWriter,
    "rejects": RejectListWriter,
    "xmp": XmpWriter,
}

WRITER_NAMES = tuple(_REGISTRY)


def build_writers(names: Sequence[str]) -> list[ReportWriter]:
    """Resolve output format names into writer instances."""
    writers: list[ReportWriter] = []
    for name in names:
        builder = _REGISTRY.get(name)
        if builder is None:
            raise ConfigError(f"unknown output format '{name}'; known: {', '.join(WRITER_NAMES)}")
        writers.append(builder())
    return writers
