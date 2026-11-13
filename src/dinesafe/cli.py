"""Command-line interface.

    python -m dinesafe download   # fetch the latest data from Toronto Open Data
    python -m dinesafe build      # clean, engineer features, train models, export dashboard data
    python -m dinesafe run        # download + build
    python -m dinesafe serve      # serve the dashboard at http://localhost:8000

Run ``python -m dinesafe <command> --help`` for options.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import logging
import sys
from pathlib import Path

from dinesafe.config import Paths, default_root

log = logging.getLogger("dinesafe")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    # urllib3 logs every retry at WARNING already; keep its debug chatter out unless asked.
    logging.getLogger("urllib3").setLevel(logging.DEBUG if verbose else logging.WARNING)


def cmd_download(args: argparse.Namespace, paths: Paths) -> int:
    from dinesafe.ckan import CkanClient
    from dinesafe.download import download_current, download_historical

    client = CkanClient()
    path = download_current(client, paths, force=args.force, keep=args.keep)
    print(f"Current feed: {path}")
    if not args.skip_historical:
        files = download_historical(client, paths, force=args.force)
        print(f"Historical archive: {len(files)} yearly files in {paths.historical}")
    return 0


def cmd_build(args: argparse.Namespace, paths: Paths) -> int:
    from dinesafe.pipeline import build, run_metrics_summary

    result = build(paths, snapshot=Path(args.snapshot) if args.snapshot else None,
                   use_historical=not args.skip_historical, evaluate=not args.no_eval)
    print(f"Built {len(result.establishments):,} establishments, {len(result.inspections):,} inspections, "
          f"{len(result.infractions):,} infractions.")
    summary = run_metrics_summary(result)
    if summary:
        print(f"Validation: {summary}")
    print(f"Dashboard data: {paths.dashboard_data / 'dinesafe-data.js'}")
    print("Open dashboard/index.html in a browser, or run: python -m dinesafe serve")
    return 0


def cmd_run(args: argparse.Namespace, paths: Paths) -> int:
    rc = cmd_download(args, paths)
    return rc or cmd_build(args, paths)


def cmd_serve(args: argparse.Namespace, paths: Paths) -> int:
    directory = paths.root / "dashboard"
    if not (directory / "data" / "dinesafe-data.js").exists():
        print("No dashboard data yet - run `python -m dinesafe run` first.", file=sys.stderr)
        return 1
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    with http.server.ThreadingHTTPServer((args.host, args.port), handler) as httpd:
        print(f"Serving the dashboard at http://{args.host}:{args.port}/  (Ctrl+C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dinesafe", description="Toronto DineSafe data pipeline and dashboard")
    p.add_argument("--root", type=Path, default=None, help="project directory (default: this repository)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    def add_download_opts(sp):
        sp.add_argument("--force", action="store_true", help="download even if the data hasn't changed")
        sp.add_argument("--keep", type=int, default=None, metavar="N",
                        help="keep only the N newest raw snapshots")

    def add_build_opts(sp):
        sp.add_argument("--snapshot", help="build from this raw CSV instead of the newest snapshot")
        sp.add_argument("--no-eval", action="store_true", help="skip model validation (faster)")

    commands = [
        ("download", cmd_download, [add_download_opts], "fetch the latest data from Toronto Open Data"),
        ("build", cmd_build, [add_build_opts], "clean, engineer features, train models, export dashboard data"),
        ("run", cmd_run, [add_download_opts, add_build_opts], "download + build"),
    ]
    for name, fn, opts, help_text in commands:
        sp = sub.add_parser(name, help=help_text, description=help_text)
        sp.add_argument("--skip-historical", action="store_true", help="don't use the 2001-2023 archive")
        for o in opts:
            o(sp)
        sp.set_defaults(func=fn)
    sp = sub.add_parser("serve", help="serve the dashboard locally")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    _setup_logging(args.verbose)
    paths = Paths((args.root or default_root()).resolve())
    try:
        return args.func(args, paths)
    except Exception as exc:  # noqa: BLE001 - turn failures into a readable message + exit code
        if args.verbose:
            raise
        log.error("%s: %s", type(exc).__name__, exc)
        return 1
