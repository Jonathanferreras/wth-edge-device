"""Command line: ``wth-edge run | display | doctor | config | update | import-dataset | eval``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from . import config as config_mod


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        # journald adds timestamps; keep lines short.
        format="%(levelname)s [%(name)s] %(message)s" if os.environ.get("INVOCATION_ID") else "%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def _load(args) -> config_mod.Config:
    try:
        return config_mod.load(args.config)
    except config_mod.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        print(f"profiles: {', '.join(config_mod.available('profiles'))}", file=sys.stderr)
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wth-edge", description="Watch The Hutch edge device")
    parser.add_argument("--config", help="device file (default: $WTH_CONFIG or /etc/wth/device.toml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"wth-edge {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="run the device app (camera, telemetry, web panel)")
    run_p.add_argument("--source", help="replay this .mp4 instead of the camera (loops)")
    run_p.add_argument("--port", type=int, help="web panel port")
    sub.add_parser("display", help="run the round status screen")
    sub.add_parser("doctor", help="check this Pi against its profile")
    sub.add_parser("config", help="print the merged config as JSON")
    sub.add_parser("update", help="git pull and re-run install.sh (needs sudo)")
    imp = sub.add_parser("import-dataset", help="import the old wth-inference-training dataset as clips")
    imp.add_argument("path", help="the dataset folder, e.g. ~/wth-inference-training/dataset")
    imp.add_argument("--data-dir", help="clip store root (default: paths.data_dir from config)")
    imp.add_argument("--link", action="store_true", help="symlink the videos instead of copying")
    imp.add_argument("--dry-run", action="store_true", help="list what would be imported")
    ev = sub.add_parser("eval", help="run the detectors over recorded clips and score them")
    ev.add_argument("path", help="a clip store folder (the one holding clips/), e.g. ~/wth-clips")
    ev.add_argument("--site", help="ROIs for clips without their own (imported clips: legacy-pi3)", default="legacy-pi3")
    ev.add_argument("--label", help="only clips with this label, e.g. boat")
    ev.add_argument("--limit", type=int, help="only the first N clips")
    ev.add_argument("--full-res", action="store_true", help="don't shrink frames to the live detector size")
    ev.add_argument("--out", help="write per-clip results to a .json or .csv file")
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.cmd == "update":
        repo = config_mod.REPO_ROOT
        if os.geteuid() != 0:
            print("run with sudo: sudo wth-edge update", file=sys.stderr)
            return 1
        owner = Path(repo).stat().st_uid
        subprocess.run(["git", "-C", str(repo), "pull", "--ff-only"], check=True,
                       user=owner if owner != 0 else None)
        return subprocess.call([str(repo / "install.sh")])

    cfg = _load(args)
    if args.cmd == "run":
        from .app import run

        if args.source:
            cfg.camera.driver = "file"
            cfg.camera.file_path = str(Path(args.source).expanduser().resolve())
        if args.port:
            cfg.web.port = args.port
        return run(cfg)
    if args.cmd == "display":
        from .display.service import run_display

        return run_display(cfg)
    if args.cmd == "doctor":
        from .doctor import run_doctor

        return run_doctor(cfg)
    if args.cmd == "import-dataset":
        from .clips import ClipStore
        from .importer import import_dataset

        store = ClipStore(Path(args.data_dir).expanduser() if args.data_dir else cfg.paths.data_dir)
        done = import_dataset(args.path, store, link=args.link, dry_run=args.dry_run)
        for d in done:
            print(f"{d['id']}  {d.get('source_path') or d['import']['source_path']}")
        print(f"{'would import' if args.dry_run else 'imported'} {len(done)} clips into {store.root}")
        return 0
    if args.cmd == "eval":
        from .evaluate import run_eval

        summary = run_eval(cfg, args.path, site=args.site, limit=args.limit, label=args.label,
                           full_res=args.full_res, out=args.out)
        s = summary
        print()
        print(f"boat clips:   {s['boat_found']}/{s['boat_clips']} found, direction right on "
              f"{s['direction_ok']}/{s['direction_checked']}")
        print(f"other clips:  {s['false_alarms']}/{s['other_clips']} false alarms")
        print(f"speed:        {s['ms_per_frame']} ms per detector frame on this machine")
        return 0
    if args.cmd == "config":
        print(json.dumps(asdict(cfg), indent=2, default=str))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
