"""``wth-edge eval``: run the detectors over recorded clips and score them.

Clips come from a clip store (``import-dataset`` output or a Pi's ``/var/lib/wth``), so
each has a ``clip.json`` with its label and, for imported boat clips, the direction from
the old folder names. Frames are decoded with PyAV, sampled at the detector rate and
resized to the live detector's frame size, then fed through the same ``Pipeline`` the
device runs. Timing is the clip's own, so ``gone_s`` and ``hold_s`` behave as live.

The score is deliberately simple:

* boat clips: did at least one crossing start (recall), and was its direction right?
* other clips: any crossing is a false alarm.
* bridge: which positions were seen and the angle range, to set the thresholds.
* opening: the phases the state machine went through, and the traffic levels per lane.
"""

from __future__ import annotations

import copy
import csv
import json
import logging
import time
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from . import config as config_mod
from .camera import HiRes
from .clips import ClipStore

log = logging.getLogger(__name__)


def _frames(path: Path, fps: float, size: tuple[int, int] | None,
            hires: bool) -> Iterator[tuple[float, np.ndarray, HiRes | None]]:
    """(t, lores RGB, full-res frame or None), like the Pi's two streams."""
    import av

    with av.open(str(path)) as c:
        stream = c.streams.video[0]
        stream.thread_type = "AUTO"
        next_t = 0.0
        for frame in c.decode(stream):
            if frame.pts is None:
                continue
            t = float(frame.pts * frame.time_base)
            if t + 1e-6 < next_t:
                continue
            next_t = t + 1.0 / fps
            full = frame.to_ndarray(format="rgb24")
            hi = HiRes(full, "rgb", (full.shape[1], full.shape[0])) if hires else None
            small = frame.reformat(width=size[0], height=size[1]).to_ndarray(format="rgb24") if size else full
            yield t, small, hi


def _rois_for(meta: dict[str, Any], fallback: dict) -> dict:
    raw = meta.get("rois") or {}
    if not raw:
        return copy.deepcopy(fallback)
    return {k: config_mod.Roi(**v) for k, v in raw.items()}


def evaluate_clip(config, video: Path, meta: dict[str, Any], rois: dict, size) -> dict[str, Any]:
    from .detectors import Pipeline

    pipe = Pipeline(config, rois)
    events: list[dict[str, Any]] = []
    positions: list[str] = []
    phases: list[str] = []
    stages: list[str] = []
    levels: dict[str, list[str]] = {}
    angles: list[float] = []
    n, t_end, t0 = 0, 0.0, time.perf_counter()
    for t, img, grey in _frames(video, config.detectors.fps, size, config.camera.detector_hires):
        evs = pipe.process(t, img, grey)
        events += evs
        st = pipe.state()
        if st["bridge"].get("angle_deg") is not None:
            angles.append(st["bridge"]["angle_deg"])
        if not positions or positions[-1] != st["bridge"]["position"]:
            positions.append(st["bridge"]["position"])
        if st.get("opening") and (not phases or phases[-1] != st["opening"]["phase"]):
            phases.append(st["opening"]["phase"])
        if st.get("stage") and (not stages or stages[-1] != st["stage"]["stage"]):
            stages.append(st["stage"]["stage"])
        for name, lane in (st.get("traffic") or {}).items():
            seen = levels.setdefault(name, [])
            if not seen or seen[-1] != lane["level"]:
                seen.append(lane["level"])
        n, t_end = n + 1, t
    # Close an open crossing at the end of the clip.
    if pipe.boats is not None:
        events += pipe.boats.update(t_end + config.boat_detector.gone_s + 1, None)
    starts = [e for e in events if e["type"] == "boat_started"]
    ends = [e for e in events if e["type"] == "boat_ended"]
    imp = meta.get("import") or {}
    expected_dir = imp.get("direction")
    got_dirs = [e.get("direction") for e in ends if e.get("direction")]
    label = meta.get("label") or "other"
    return {
        "id": meta.get("id"),
        "source": imp.get("source_path") or str(video),
        "label": label,
        "bad_footage": bool(imp.get("bad_footage")),
        "expected_direction": expected_dir,
        "frames": n,
        "seconds": round(t_end, 1),
        "ms_per_frame": round((time.perf_counter() - t0) * 1000 / max(n, 1), 1),
        "crossings": len(starts),
        "directions": got_dirs,
        "direction_ok": (expected_dir in got_dirs) if expected_dir and got_dirs else None,
        "best_confidence": max((e.get("confidence") or 0 for e in ends), default=0.0),
        "bridge_positions": positions,
        "phases": phases,
        "stages": stages,
        "traffic": levels,
        "angle_min": round(min(angles), 1) if angles else None,
        "angle_max": round(max(angles), 1) if angles else None,
        "angle_found": round(len(angles) / max(n, 1), 2),
        "events": events,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    boat = [r for r in rows if r["label"] == "boat" and not r["bad_footage"]]
    other = [r for r in rows if r["label"] != "boat"]
    with_dir = [r for r in boat if r["expected_direction"] and r["crossings"]]
    return {
        "clips": len(rows),
        "boat_clips": len(boat),
        "boat_found": sum(1 for r in boat if r["crossings"]),
        "direction_checked": len(with_dir),
        "direction_ok": sum(1 for r in with_dir if r["direction_ok"]),
        "other_clips": len(other),
        "false_alarms": sum(1 for r in other if r["crossings"]),
        "ms_per_frame": round(sum(r["ms_per_frame"] for r in rows) / max(len(rows), 1), 1),
    }


def run_eval(config, path: str | Path, site: str | None = None, limit: int | None = None,
             label: str | None = None, full_res: bool = False, out: str | Path | None = None) -> dict[str, Any]:
    path = Path(path).expanduser()
    fallback = config.site.rois
    if site:
        fallback = config_mod._build_site(config_mod._read_toml(config_mod.find_named("sites", site)).get("site", {})).rois
    size = None if full_res else tuple(config.camera.lores_size)
    store = ClipStore(path)
    metas = sorted(store.iter_meta(), key=lambda m: m.get("started_at", 0))
    if label:
        metas = [m for m in metas if m.get("label") == label]
    if limit:
        metas = metas[:limit]
    rows = []
    for i, meta in enumerate(metas, 1):
        video = store.paths(meta["id"])["video"]
        if not video.exists():
            continue
        try:
            row = evaluate_clip(config, video, meta, _rois_for(meta, fallback), size)
        except Exception as exc:
            log.warning("skipping %s: %s", meta["id"], exc)
            continue
        rows.append(row)
        flag = ""
        if row["label"] == "boat":
            flag = "FOUND" if row["crossings"] else "MISSED"
            if row["direction_ok"] is False:
                flag += " wrong-dir"
        elif row["crossings"]:
            flag = "FALSE-ALARM"
        print(f"[{i}/{len(metas)}] {row['label']:<7} {flag:<18} crossings={row['crossings']} dirs={row['directions']} "
              f"expected={row['expected_direction']} bridge={'>'.join(row['bridge_positions'])} "
              f"phases={'>'.join(row['phases'])} "
              f"nb={'>'.join(row['traffic'].get('northbound', []))} sb={'>'.join(row['traffic'].get('southbound', []))} "
              f"angle={row['angle_min']}..{row['angle_max']} {row['ms_per_frame']} ms/f  {row['source']}", flush=True)
    summary = summarize(rows)
    if out:
        out = Path(out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.suffix == ".csv":
            keys = [k for k in rows[0] if k != "events"] if rows else []
            with out.open("w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
        else:
            out.write_text(json.dumps({"summary": summary, "clips": rows}, indent=2, default=str))
    return summary
