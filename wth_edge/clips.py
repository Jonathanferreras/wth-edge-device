"""Recorded clips on disk: ``<data_dir>/clips/YYYY-MM-DD/<id>.mp4`` plus a ``<id>.json``
sidecar and a ``<id>.jpg`` thumbnail.

The sidecar is the training metadata: when, why and how the clip was recorded, its label,
and (from phase 3) a per-second log of detections to use as draft labels. The same layout
is used for clips imported from the old dataset, so training code reads one format.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

SCHEMA = 1
ID_RE = re.compile(r"^[0-9A-Za-z_.-]{1,128}$")


def utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def new_clip_id(device_id: str, trigger: str, ts: float | None = None) -> str:
    stamp = datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_dev = re.sub(r"[^0-9A-Za-z-]", "-", device_id)[:40]
    return f"{stamp}_{safe_dev}_{trigger}"


class ClipStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.root = Path(data_dir) / "clips"

    # ---- paths ----
    def dir_for(self, clip_id: str) -> Path:
        day = clip_id[:8]
        if not day.isdigit():
            day = "imported"
        return self.root / f"{day[:4]}-{day[4:6]}-{day[6:8]}" if day.isdigit() else self.root / day

    def paths(self, clip_id: str) -> dict[str, Path]:
        if not ID_RE.match(clip_id):
            raise ValueError(f"bad clip id {clip_id!r}")
        d = self.dir_for(clip_id)
        return {"video": d / f"{clip_id}.mp4", "meta": d / f"{clip_id}.json", "thumb": d / f"{clip_id}.jpg"}

    # ---- metadata ----
    def write_meta(self, meta: dict[str, Any]) -> None:
        p = self.paths(meta["id"])["meta"]
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, indent=2))
        os.replace(tmp, p)

    def read_meta(self, clip_id: str) -> dict[str, Any] | None:
        try:
            return json.loads(self.paths(clip_id)["meta"].read_text())
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            return None

    def iter_meta(self) -> Iterator[dict[str, Any]]:
        if not self.root.is_dir():
            return
        for p in self.root.glob("*/*.json"):
            try:
                meta = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(meta, dict) and "id" in meta:
                yield meta

    def list(self, limit: int | None = None) -> list[dict[str, Any]]:
        clips = sorted(self.iter_meta(), key=lambda m: m.get("started_at", 0), reverse=True)
        for m in clips:
            v = self.paths(m["id"])["video"]
            m["size_bytes"] = v.stat().st_size if v.exists() else 0
        return clips[:limit] if limit else clips

    def update(self, clip_id: str, **fields: Any) -> dict[str, Any] | None:
        meta = self.read_meta(clip_id)
        if meta is None:
            return None
        meta.update(fields)
        self.write_meta(meta)
        return meta

    def delete(self, clip_id: str) -> bool:
        found = False
        for p in self.paths(clip_id).values():
            try:
                p.unlink()
                found = True
            except FileNotFoundError:
                pass
        return found

    # ---- housekeeping ----
    def prune(self, min_free_percent: float, active_id: str | None = None) -> list[str]:
        """Delete the oldest clips not marked keep until free space is above the floor."""
        self.root.mkdir(parents=True, exist_ok=True)
        deleted: list[str] = []

        def free_pct() -> float:
            # Same as df and the telemetry: root-reserved blocks don't count as free or used.
            u = shutil.disk_usage(self.root)
            return u.free / ((u.used + u.free) or 1) * 100

        if free_pct() >= min_free_percent:
            return deleted
        candidates = sorted(
            (m for m in self.iter_meta() if not m.get("keep") and m["id"] != active_id),
            key=lambda m: m.get("started_at", 0),
        )
        for m in candidates:
            if free_pct() >= min_free_percent:
                break
            self.delete(m["id"])
            deleted.append(m["id"])
            log.warning("disk below %.0f%% free: deleted clip %s", min_free_percent, m["id"])
        return deleted
