"""Machine-readable projections of a run: JSON, CSV, plain lists, XMP sidecars.

JSON is the canonical form and carries everything. The others exist because a
tool that only speaks its own format is a tool you have to live inside; these
let the results leave and go somewhere useful.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Sequence
from xml.sax.saxutils import escape

from ..config import Config
from ..models import PhotoReport
from .naming import mirrored_names

# Columns leading the CSV. The rest follow alphabetically, so a new measurement
# appears automatically instead of being silently dropped from the spreadsheet.
_LEAD_COLUMNS = (
    "filename",
    "rating",
    "group_id",
    "group_rank",
    "group_size",
    "is_group_best",
    "subject_source",
    "subject_acutance",
    "background_acutance",
    "subject_background_ratio",
    "max_local_acutance",
    "sharp_fraction",
    "likely_cause",
    "highlight_clipped",
)


class JsonWriter:
    """The complete record, including tile maps when enabled."""

    name = "json"
    extension = ".json"

    def write(self, reports: Sequence[PhotoReport], directory: Path, config: Config) -> Path:
        from ..pipeline import summarise

        target = directory / f"photocull{self.extension}"
        payload = {
            "summary": summarise(reports),
            "config_source": config.source_path,
            "photos": [
                report.as_dict(include_tiles=config.output.include_tile_map) for report in reports
            ],
        }
        target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return target


class CsvWriter:
    """One row per photograph, every measurement flattened into a column."""

    name = "csv"
    extension = ".csv"

    def write(self, reports: Sequence[PhotoReport], directory: Path, config: Config) -> Path:
        target = directory / f"photocull{self.extension}"
        if not reports:
            target.write_text("", encoding="utf-8")
            return target

        rows = [report.flat_metrics() | {"rating": report.rating, "error": report.error or ""} for report in reports]
        every_key = sorted({key for row in rows for key in row})
        columns = [c for c in _LEAD_COLUMNS if c in every_key]
        columns += [key for key in every_key if key not in columns]

        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        return target


class KeeperListWriter:
    """A plain list of paths worth keeping, one per line.

    Deliberately dumb, because dumb pipes well: this is the file you feed to
    ``xargs cp``, to rsync, or to any program that takes a list of names.
    """

    name = "keepers"
    extension = ".txt"

    def __init__(self, minimum_rating: int = 4) -> None:
        self._minimum = minimum_rating

    def write(self, reports: Sequence[PhotoReport], directory: Path, config: Config) -> Path:
        target = directory / f"keepers{self.extension}"
        keepers = [r.path for r in reports if not r.error and (r.rating or 0) >= self._minimum]
        target.write_text("\n".join(keepers) + ("\n" if keepers else ""), encoding="utf-8")
        return target


class RejectListWriter:
    """The mirror image: everything that failed the bar, for review not deletion."""

    name = "rejects"
    extension = ".txt"

    def __init__(self, maximum_rating: int = 2) -> None:
        self._maximum = maximum_rating

    def write(self, reports: Sequence[PhotoReport], directory: Path, config: Config) -> Path:
        target = directory / f"rejects{self.extension}"
        rejects = [
            f"{r.path}\t{'; '.join(r.reasons)}"
            for r in reports
            if not r.error and r.rating is not None and r.rating <= self._maximum
        ]
        target.write_text("\n".join(rejects) + ("\n" if rejects else ""), encoding="utf-8")
        return target


class XmpWriter:
    """Star ratings and colour labels as XMP sidecars.

    Lightroom, darktable, RawTherapee and Bridge all read these natively, so the
    verdicts land in the software you already cull in rather than asking you to
    move somewhere new.

    Sidecars are written beside the originals only when explicitly enabled, and
    an existing sidecar is never overwritten -- yours may hold develop settings
    representing real work, and no rating is worth destroying that.

    In the report directory the sidecars mirror the source tree rather than
    landing in one flat folder. Two shoots in one library each holding a
    ``DSC_0001.NEF`` is the ordinary case, and a flat folder answers it by
    overwriting the first frame's verdict with the second's while the manifest
    goes on claiming it wrote both.
    """

    name = "xmp"
    extension = ".xmp"

    _TEMPLATE = (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="photocull">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        '    xmlns:xmp="http://ns.adobe.com/xap/1.0/"\n'
        '    xmlns:photocull="https://github.com/photocull/ns/1.0/"\n'
        '    xmp:Rating="{rating}"\n'
        '    xmp:Label="{label}"\n'
        '    photocull:Reason="{reason}"\n'
        '    photocull:SubjectSource="{source}"\n'
        '    photocull:SubjectAcutance="{subject}"\n'
        '    photocull:MaxLocalAcutance="{peak}"/>\n'
        ' </rdf:RDF>\n'
        "</x:xmpmeta>\n"
        '<?xpacket end="w"?>\n'
    )

    def write(self, reports: Sequence[PhotoReport], directory: Path, config: Config) -> Path:
        beside_originals = config.output.write_xmp_next_to_originals
        written = 0
        skipped: list[str] = []

        rateable = [r for r in reports if not r.error and r.rating is not None]
        # Computed over the whole set at once, because uniqueness is a property
        # of the set and cannot be decided one frame at a time.
        mirrored = mirrored_names([r.path for r in rateable], self.extension)

        for report, relative in zip(rateable, mirrored):
            if beside_originals:
                target = Path(report.path).with_suffix(Path(report.path).suffix + ".xmp")
                if target.exists():
                    skipped.append(target.name)
                    continue
            else:
                target = directory / "xmp" / relative
                target.parent.mkdir(parents=True, exist_ok=True)

            subject = report.sharpness.subject_acutance
            target.write_text(
                self._TEMPLATE.format(
                    rating=report.rating,
                    label=escape(report.label or ""),
                    reason=escape("; ".join(report.reasons)),
                    source=escape(report.detection.source),
                    subject=f"{subject:.2f}" if subject is not None else "",
                    peak=f"{report.sharpness.max_local_acutance:.2f}",
                ),
                encoding="utf-8",
            )
            written += 1

        manifest = directory / "xmp-manifest.txt"
        # Distinct names by construction, so this count is now a fact about the
        # disk rather than a count of attempts.
        lines = [f"wrote {written} sidecar(s)"]
        if written and not beside_originals:
            lines.append("in xmp/, mirroring the folder layout of the originals")
        if skipped:
            lines.append(f"skipped {len(skipped)} existing sidecar(s), none overwritten:")
            lines.extend(f"  {name}" for name in skipped)
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return manifest
