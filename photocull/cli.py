"""Command line interface.

Five verbs, each doing one thing:

``run``      analyse a folder and write reports
``explain``  analyse a single frame and print every number, for calibration
``compare``  say what moved between two runs, which is the other half of that
``doctor``   say which detectors and loaders can actually run on this machine
``init``     write a commented config file you can start editing

``doctor`` earns its place: every optional capability degrades to a named
fallback instead of failing, which is good for reliability and terrible for
understanding what just happened. One command that prints the truth about the
environment costs nothing and removes the mystery.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from pathlib import Path
from typing import Sequence

from . import __version__
from .analysis import Analyzer, build_detector_chain
from .config import DEFAULT_CONFIG_NAME, Config
from .default_config import DEFAULT_CONFIG_TOML
from .detect import DETECTOR_NAMES
from .errors import PhotocullError
from .loading import PLAIN_SUFFIXES, RAW_SUFFIXES, build_registry
from .outputs import WRITER_NAMES, build_writers
from .pipeline import discover, run, summarise
from .rating import Rater
from .rating import validate as validate_rules


def _progress(done: int, total: int, name: str) -> None:
    """Single-line progress that behaves when stderr is not a terminal."""
    if not sys.stderr.isatty():
        return
    width = 28
    filled = int(width * done / total) if total else width
    bar = "#" * filled + "." * (width - filled)
    sys.stderr.write(f"\r  [{bar}] {done}/{total}  {name[:34]:<34}")
    sys.stderr.flush()


def _number(value: float | None, width: int = 8) -> str:
    """Format a measurement that is allowed to be absent.

    Half the interesting figures are undefined for some frames -- no subject
    found, or a background with nothing to compare against -- and "undefined" is
    a real answer this tool goes out of its way to report rather than fake.
    """
    return f"{value:{width}.2f}" if value is not None else f"{'-':>{width}}"


def _load_config(args: argparse.Namespace, target: Path) -> Config:
    if args.config:
        return Config.load(Path(args.config))
    if args.no_config:
        return Config()
    return Config.discover(target if target.is_dir() else target.parent)


def command_run(args: argparse.Namespace) -> int:
    target = Path(args.path).expanduser().resolve()
    config = _load_config(args, target)

    if args.detector:
        from dataclasses import replace

        config = replace(config, subject=replace(config.subject, detectors=tuple(args.detector)))
    if args.format:
        from dataclasses import replace

        config = replace(config, output=replace(config.output, formats=tuple(args.format)))
    if args.no_group:
        from dataclasses import replace

        config = replace(config, grouping=replace(config.grouping, enabled=False))

    # Before discovery, not after analysis: a typo'd measurement name should
    # cost a millisecond, not the whole run.
    validate_rules(config.rating.rules, config.rating.rank_by)

    files = discover(target, config)
    if not files:
        print(f"No supported images found under {target}", file=sys.stderr)
        return 1

    writers = build_writers(config.output.formats)
    source = config.source_path or "built-in defaults"
    print(f"photocull {__version__}  |  {len(files)} file(s)  |  config: {source}")
    print(f"  subject chain: {' -> '.join(config.subject.detectors)}")

    started = time.perf_counter()
    reports = run(target, config, workers=args.workers, progress=_progress)
    elapsed = time.perf_counter() - started
    if sys.stderr.isatty():
        sys.stderr.write("\r" + " " * 78 + "\r")

    summary = summarise(reports)
    rate = len(files) / elapsed if elapsed else 0.0
    print(f"  analysed {summary['analysed']}/{summary['total']} in {elapsed:.1f}s ({rate:.1f}/s)")
    if summary["failed"]:
        print(f"  {summary['failed']} file(s) failed; see the report for details")
    print(f"  subject located in {summary['subject_found']} frame(s): {summary['by_detector']}")
    print(f"  {summary['groups']} group(s); {summary['in_multi_frame_groups']} frame(s) have near-duplicates")
    print(f"  ratings: {summary['by_rating']}")

    directory = Path(args.out or config.output.directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    written = [writer.write(reports, directory, config) for writer in writers]
    print("  wrote:")
    for path in written:
        print(f"    {path}")

    html = next((p for p in written if p.suffix == ".html"), None)
    if html and (args.open or config.output.open_html):
        webbrowser.open(html.as_uri())
    return 0


def command_explain(args: argparse.Namespace) -> int:
    """Print every measurement for one frame, so thresholds can be calibrated."""
    target = Path(args.path).expanduser().resolve()
    config = _load_config(args, target)
    validate_rules(config.rating.rules, config.rating.rank_by)
    analyzer = Analyzer(config, root=target.parent)
    result = analyzer.analyse(target, want_thumbnail=False)
    report = result.report

    # The command exists to calibrate thresholds, and a calibration tool that
    # prints every measurement without ever saying what the current thresholds
    # made of them leaves the loop open. One call closes it.
    if not report.error:
        report = Rater(config.rating.rules).apply(report)

    if args.json:
        print(json.dumps(report.as_dict(include_tiles=True), indent=2, default=str))
        return 1 if report.error else 0

    if report.error:
        print(f"{report.filename}: {report.error}", file=sys.stderr)
        return 1

    print(f"{report.filename}  {report.width}x{report.height}")
    print(f"  subject      {report.detection.source} ({report.detection.confidence.value})")
    print(f"               {report.detection.note}")
    if report.detection.box:
        box = report.detection.box
        print(f"               box x={box.x:.3f} y={box.y:.3f} w={box.w:.3f} h={box.h:.3f}")
    sharp = report.sharpness
    print("  sharpness")
    print(f"    peak local            {sharp.max_local_acutance:8.2f}")
    print(f"    frame mean            {sharp.global_acutance:8.2f}")
    if sharp.subject_acutance is not None:
        # Each of these is optional on its own. A subject can be measured while
        # the ratio against it is refused: min_background_acutance declines to
        # divide by a textureless background, which is exactly the case a night
        # sky or a studio backdrop produces. Guarding only on subject_acutance
        # crashed this command on any such frame.
        print(f"    subject               {_number(sharp.subject_acutance)}")
        print(f"    background            {_number(sharp.background_acutance)}")
        print(f"    subject / background  {_number(sharp.subject_background_ratio)}")
        if sharp.subject_background_ratio is None:
            print("                          (background has too little texture to divide by)")
    print(f"    in focus              {sharp.sharp_fraction * 100:7.1f}%")
    print(f"    focus point           x={sharp.focus_x:.2f} y={sharp.focus_y:.2f}")
    print("  blur")
    print(f"    looks like            {report.blur.likely_cause}")
    print(f"    anisotropy            {report.blur.anisotropy:8.2f} @ {report.blur.dominant_axis_degrees:.0f} deg")
    print("  exposure")
    print(f"    highlights clipped    {report.exposure.highlight_clipped * 100:7.2f}%")
    print(f"    shadows clipped       {report.exposure.shadow_clipped * 100:7.2f}%")
    print(f"    dynamic range         {report.exposure.dynamic_range:8.2f}")
    capture = report.capture
    if capture.camera or capture.shutter_seconds:
        print("  capture")
        print(f"    camera                {capture.camera or '-'}")
        print(f"    lens                  {capture.lens or '-'}")
        shutter = f"1/{1 / capture.shutter_seconds:.0f}s" if capture.shutter_seconds else "-"
        print(f"    exposure              {shutter}  f/{capture.aperture or '-'}  ISO {capture.iso or '-'}")
        margin = capture.reciprocal_margin
        if margin is not None:
            verdict = "safe" if margin >= 0 else "below the handholding rule"
            print(f"    shutter margin        {margin:+.1f} stops ({verdict})")

    print("  verdict")
    stars = "-" if report.rating is None else "*" * report.rating + "." * (5 - report.rating)
    print(f"    rating                {stars}  ({report.rating if report.rating is not None else 'none'})")
    print(f"    label                 {report.label or '-'}")
    for reason in report.reasons:
        print(f"    because               {reason}")
    # Said plainly rather than left to be discovered. Half the default ladder
    # asks about a frame's standing among its near-duplicates, and one frame has
    # no near-duplicates by construction -- so those rules cannot fire here, and
    # a rating from `explain` can legitimately differ from the same frame's
    # rating in a full run.
    if any("group" in rule.when for rule in config.rating.rules):
        print("    note                  rules about groups cannot fire for a single frame;")
        print("                          run the folder to see this frame ranked")
    return 0


def command_compare(args: argparse.Namespace) -> int:
    """Say what moved between two runs, which is what calibration needs.

    Deliberately takes the JSON reports rather than re-running anything. The
    second half of a calibration loop is comparing a run you already did against
    one you did an hour ago with a different threshold, and re-analysing eight
    hundred frames to answer that would make the loop too slow to use.
    """
    from .compare import compare_files, format_comparison

    result = compare_files(Path(args.before).expanduser(), Path(args.after).expanduser())
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(format_comparison(result, limit=args.limit))
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    """Report what this machine can actually do."""
    target = Path(args.path or ".").expanduser().resolve()
    config = _load_config(args, target)

    print(f"photocull {__version__}")
    print(f"  config: {config.source_path or 'built-in defaults'}")

    registry = build_registry(config.input.prefer_raw_decode)
    print(f"  loaders: {', '.join(registry.names)}")
    if config.input.prefer_raw_decode and "raw-decode" not in registry.names:
        print("    ! prefer_raw_decode is set but rawpy is not installed; using embedded previews")

    print("  detectors:")
    chain = build_detector_chain(config, root=target)
    usable_any = False
    for name, usable, reason in chain.availability_report():
        mark = "ok " if usable else "-- "
        detail = "" if usable else f"  ({reason})"
        print(f"    {mark}{name}{detail}")
        usable_any = usable_any or usable

    print(f"  formats available: {', '.join(WRITER_NAMES)}")
    print(f"  extensions handled: {len(RAW_SUFFIXES)} raw, {len(PLAIN_SUFFIXES)} standard")
    return 0 if usable_any else 1


def command_fetch_models(args: argparse.Namespace) -> int:
    """Download the optional model files, once, on purpose.

    Separate from ``run`` deliberately. Nothing here reaches the network unless
    you asked it to, so behaviour does not change based on whether you happen to
    be online.
    """
    from .assets import ASSETS, fetch, model_directory

    print(f"model cache: {model_directory()}")
    for asset in ASSETS:
        if asset.present() and not args.force:
            print(f"  ok   {asset.filename} (already present)")
            continue
        print(f"  .... {asset.filename} - {asset.description} ({asset.size_bytes // 1024} KB)")
        path = fetch(asset, force=args.force)
        print(f"  ok   {path}")
    return 0


def command_init(args: argparse.Namespace) -> int:
    target = Path(args.path or DEFAULT_CONFIG_NAME).expanduser().resolve()
    if target.exists() and not args.force:
        print(f"{target} already exists; pass --force to overwrite", file=sys.stderr)
        return 1
    target.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    print(f"wrote {target}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="photocull",
        description="Measure where focus actually landed, and show its work.",
    )
    parser.add_argument("--version", action="version", version=f"photocull {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_config_flags(target: argparse.ArgumentParser) -> None:
        target.add_argument("-c", "--config", help=f"path to a config file (default: nearest {DEFAULT_CONFIG_NAME})")
        target.add_argument("--no-config", action="store_true", help="ignore config files, use built-in defaults")

    run_parser = sub.add_parser("run", help="analyse a folder and write reports")
    run_parser.add_argument("path", help="file or folder to analyse")
    add_config_flags(run_parser)
    run_parser.add_argument("-o", "--out", help="report directory")
    run_parser.add_argument("-f", "--format", action="append", choices=WRITER_NAMES,
                            help="output format (repeatable; overrides config)")
    run_parser.add_argument("-d", "--detector", action="append", choices=DETECTOR_NAMES,
                            help="subject detector, in preference order (repeatable)")
    run_parser.add_argument("-j", "--workers", type=int, help="parallel workers (default: CPU count, max 8)")
    run_parser.add_argument("--no-group", action="store_true", help="skip near-duplicate grouping")
    run_parser.add_argument("--open", action="store_true", help="open the contact sheet when done")
    run_parser.set_defaults(handler=command_run)

    explain_parser = sub.add_parser("explain", help="print every measurement for one frame")
    explain_parser.add_argument("path", help="image file")
    add_config_flags(explain_parser)
    explain_parser.add_argument("--json", action="store_true", help="emit the full record as JSON")
    explain_parser.set_defaults(handler=command_explain)

    compare_parser = sub.add_parser("compare", help="say what changed between two runs")
    compare_parser.add_argument("before", help="the earlier photocull.json")
    compare_parser.add_argument("after", help="the later photocull.json")
    compare_parser.add_argument("--json", action="store_true", help="emit the comparison as JSON")
    compare_parser.add_argument("--limit", type=int, default=20,
                                help="how many changed frames to list (default: 20)")
    compare_parser.set_defaults(handler=command_compare)

    doctor_parser = sub.add_parser("doctor", help="report which capabilities are available here")
    doctor_parser.add_argument("path", nargs="?", help="folder the run would target")
    add_config_flags(doctor_parser)
    doctor_parser.set_defaults(handler=command_doctor)

    fetch_parser = sub.add_parser("fetch-models", help="download optional model files (one time)")
    fetch_parser.add_argument("--force", action="store_true", help="re-download even if present")
    fetch_parser.set_defaults(handler=command_fetch_models)

    init_parser = sub.add_parser("init", help="write a commented default config file")
    init_parser.add_argument("path", nargs="?", help=f"where to write it (default: ./{DEFAULT_CONFIG_NAME})")
    init_parser.add_argument("--force", action="store_true", help="overwrite an existing file")
    init_parser.set_defaults(handler=command_init)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except PhotocullError as error:
        print(f"photocull: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
