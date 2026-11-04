"""Import the old ``wth-inference-training/dataset`` into the clip store.

The old dataset is folders of .mp4 files, for example::

    boat_crossings/04-03-2026/boat1/e2w/04-03-2026_boat_crossing_e2w_003.mp4
    boat_crossings/04-03-2026/boat1/boat_details.json
    traffic/03-16-2026/...

Each file becomes a clip with a ``clip.json`` sidecar in the same format the device
records, so training code reads one layout. What the folder names say is kept as
metadata: the label (from the top folder), the direction (``e2w`` / ``w2e``), a
``bad_footage`` flag, and any ``boat_details.json`` found on the way up.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .clips import SCHEMA, ClipStore, utc_iso

log = logging.getLogger(__name__)

DATE_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4})")
LABELS = {"boat_crossings": "boat", "boats": "boat", "traffic": "traffic", "bridge": "bridge"}


def _probe(path: Path) -> tuple[float | None, tuple[int, int] | None, bytes | None]:
    """Duration, size and a JPEG thumbnail of the first frame (needs PyAV)."""
    try:
        import av
    except ImportError:
        return None, None, None
    try:
        with av.open(str(path)) as c:
            s = c.streams.video[0]
            dur = float(c.duration / 1_000_000) if c.duration else (float(s.duration * s.time_base) if s.duration else None)
            size = (s.codec_context.width, s.codec_context.height)
            thumb = None
            for frame in c.decode(s):
                img = frame.to_image()
                img.thumbnail((320, 180))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=75)
                thumb = buf.getvalue()
                break
            return dur, size, thumb
    except Exception as exc:  # broken files exist in the old set
        log.warning("could not read %s: %s", path, exc)
        return None, None, None


def _boat_details(path: Path, root: Path) -> Any:
    for d in [path.parent, *path.parent.parents]:
        f = d / "boat_details.json"
        if f.is_file():
            try:
                return json.loads(f.read_text())
            except json.JSONDecodeError:
                return None
        if d == root:
            break
    return None


def describe(path: Path, root: Path) -> dict[str, Any]:
    rel = path.relative_to(root)
    parts = [p.lower() for p in rel.parts]
    label = next((LABELS[p] for p in parts if p in LABELS), "other")
    direction = next((d for d in ("e2w", "w2e") if any(d in p for p in parts)), None)
    m = DATE_RE.search(str(rel))
    if m:
        day = datetime(int(m[3]), int(m[1]), int(m[2]), tzinfo=timezone.utc)
    else:
        day = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(hour=0, minute=0, second=0)
    digest = hashlib.sha1(str(rel).encode()).hexdigest()[:8]
    clip_id = f"{day:%Y%m%d}T000000Z_import_{label}_{digest}"
    return {
        "id": clip_id,
        "label": label,
        "direction": direction,
        "bad_footage": any("bad" in p for p in parts),
        "source_path": str(rel),
        "day": day,
    }


def import_dataset(root: str | Path, store: ClipStore, link: bool = False, dry_run: bool = False) -> list[dict[str, Any]]:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    out = []
    for path in sorted(root.rglob("*.mp4")):
        if any(p.startswith(".") for p in path.relative_to(root).parts):
            continue
        d = describe(path, root)
        paths = store.paths(d["id"])
        if paths["meta"].exists():
            log.info("skip %s (already imported)", d["source_path"])
            continue
        if dry_run:
            out.append(d)
            continue
        dur, size, thumb = _probe(path)
        paths["video"].parent.mkdir(parents=True, exist_ok=True)
        if link:
            os.symlink(path, paths["video"])
        else:
            shutil.copy2(path, paths["video"])
        if thumb:
            paths["thumb"].write_bytes(thumb)
        ts = d["day"].timestamp()
        meta = {
            "schema": SCHEMA,
            "id": d["id"],
            "device_id": "legacy-pi3",
            "site": "default",
            "trigger": "import",
            "label": d["label"],
            "note": "",
            "keep": True,  # never auto-delete the old dataset
            "started_at": ts,
            "started_at_utc": utc_iso(ts),
            "prebuffer_s": 0.0,
            "ended_at": ts + dur if dur else None,
            "duration_s": round(dur, 1) if dur else None,
            "camera": {"sensor": "imx219", "size": list(size) if size else None, "framerate": None},
            "rois": {},
            "detections": [],
            "import": {
                "source_path": d["source_path"],
                "direction": d["direction"],
                "bad_footage": d["bad_footage"],
                "boat_details": _boat_details(path, root),
            },
        }
        store.write_meta(meta)
        out.append(meta)
        log.info("imported %s -> %s", d["source_path"], d["id"])
    return out
