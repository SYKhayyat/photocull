"""The report-writer interface.

Every writer is a projection of the same list of :class:`PhotoReport` objects.
Adding a format means adding a class and registering it; the analysis never
learns that the format exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

from ..config import Config
from ..models import PhotoReport


@runtime_checkable
class ReportWriter(Protocol):
    """Writes one representation of a finished run."""

    name: str
    extension: str

    def write(
        self, reports: Sequence[PhotoReport], directory: Path, config: Config
    ) -> Path:
        """Write the report and return the path that was created."""
        ...
