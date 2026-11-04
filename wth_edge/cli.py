"""Command line: ``wth-edge run | display | doctor | config | update``."""

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
    sub.add_parser("run", help="run the device app (camera, telemetry, web panel)")
    sub.add_parser("display", help="run the round status screen")
    sub.add_parser("doctor", help="check this Pi against its profile")
    sub.add_parser("config", help="print the merged config as JSON")
    sub.add_parser("update", help="git pull and re-run install.sh (needs sudo)")
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

        return run(cfg)
    if args.cmd == "display":
        from .display.service import run_display

        return run_display(cfg)
    if args.cmd == "doctor":
        from .doctor import run_doctor

        return run_doctor(cfg)
    if args.cmd == "config":
        print(json.dumps(asdict(cfg), indent=2, default=str))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
