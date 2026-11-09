"""The self-tuning session: record, review, tune, report, clean up. Owned by the device
process (it needs the recorder and the live config); started and stopped from the panel.

One round:

1. **Record** ``duration_s`` in chunks of ``chunk_s`` through the normal ``Recorder``
   (trigger ``tune``). The live detector keeps running and logs what it reports every
   second into each chunk's clip.json, like any clip. Auto-recording stands aside.
2. **Review**, in the background while the next chunk records: the reference pass
   (``reference.py``) counts cars and boats and saves the moments to review; Claude labels
   them, by default Claude Code on the Pi through a queue folder (``claude_code.py``), or
   the API (``review.py``).
3. **Tune** (``analysis.py``): score the live log against the labels, fit thresholds, replay
   the recording with the old and new settings, and keep the new ones only if they score
   better. Kept settings go live at once and are saved to ``<data_dir>/tuning.toml``.
4. **Report** to ``<data_dir>/tune/<round>/report.json`` with a few frames, and delete the
   recording.

Rounds repeat while the new settings helped and accuracy is under ``target_accuracy``, up to
``max_rounds``. Turning the switch off while recording ends the recording and still writes a
report on what was recorded; turning it off again during the analysis abandons it.
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import config as config_mod
from . import analysis, review
from .reference import LANES, Reference

log = logging.getLogger(__name__)

TRIGGER = "tune"
ID_CHARS = set("0123456789TZr-")


class TuneError(RuntimeError):
    pass


def _now_id(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _lower_priority() -> None:
    """Analysis runs behind the live detector: nice this thread (Linux: per thread)."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
    except (AttributeError, OSError):
        pass


class Tuner:
    def __init__(self, config, recorder, store) -> None:
        self.config = config
        self.tcfg = config.tune
        self.recorder = recorder
        self.store = store
        self.root = Path(config.paths.data_dir) / "tune"
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_recording = threading.Event()
        self._abort = threading.Event()
        self._wait = self._stop_recording.wait  # how a chunk waits out its length (tests swap it)
        self._status: dict[str, Any] = {"active": False, "phase": "idle"}

    # ------------------------------------------------------------ panel API
    def status(self) -> dict[str, Any]:
        ok, why = review.available(self.tcfg, self.config.paths.data_dir)
        with self._lock:
            st = dict(self._status)
        st.update({
            "enabled": self.tcfg.enabled,
            "recording_available": self.recorder.available,
            "reviewer": {"ok": ok, "why": why},
            "duration_s": st.get("duration_s") or self.tcfg.duration_s,
            "chunk_s": self.tcfg.chunk_s,
            "target_accuracy": self.tcfg.target_accuracy,
            "max_rounds": self.tcfg.max_rounds,
            "tuned": self.tuned(),
        })
        return st

    def tuned(self) -> list[dict[str, Any]]:
        out = []
        for dotted, base in sorted(self.config.untuned.items()):
            section, key = dotted.split(".", 1)
            out.append({"key": dotted, "base": base, "now": getattr(getattr(self.config, section), key)})
        return out

    def start(self, minutes: float | None = None) -> dict[str, Any]:
        if not self.tcfg.enabled:
            raise TuneError("self-tuning is turned off in the config ([tune] enabled = false)")
        with self._lock:
            if self._status.get("active"):
                raise TuneError("self-tuning is already running")
        rec = self.recorder
        if not rec.available:
            raise TuneError("recording is off on this device, and self-tuning needs it")
        if rec.active_trigger == "manual":
            raise TuneError("a clip is being recorded by hand; stop it first")
        duration = float(minutes) * 60 if minutes else self.tcfg.duration_s
        duration = min(max(duration, 60.0), 6 * 3600.0)
        self._check_disk(duration)
        sid = _now_id(time.time())
        self._stop_recording.clear()
        self._abort.clear()
        self._set(active=True, session=sid, phase="starting", round=1, duration_s=duration, started_at=time.time(),
                  message="Starting", error=None, chunk=0, chunks=0, reviewed=0, to_review=0)
        self._thread = threading.Thread(target=self._run, args=(sid, duration), name="tune", daemon=True)
        self._thread.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        """First call ends the recording (the analysis still runs); a second abandons it."""
        with self._lock:
            active, phase = self._status.get("active"), self._status.get("phase")
        if not active:
            return self.status()
        if phase in ("starting", "recording") and not self._stop_recording.is_set():
            self._stop_recording.set()
            self._set(message="Stopping: finishing the analysis of what was recorded")
        else:
            self._abort.set()
            self._stop_recording.set()
            self._set(message="Stopping: abandoning the analysis")
        return self.status()

    def close(self) -> None:
        self._abort.set()
        self._stop_recording.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def reset_tuning(self) -> list[dict[str, Any]]:
        """Undo every tuned value: back to the config files, live, and delete tuning.toml."""
        undone = self.tuned()
        for dotted, base in self.config.untuned.items():
            section, key = dotted.split(".", 1)
            setattr(getattr(self.config, section), key, base)
        self.config.untuned.clear()
        try:
            config_mod.tuning_override_path(self.config).unlink()
        except FileNotFoundError:
            pass
        log.info("tune: tuning reset (%d values)", len(undone))
        return undone

    def reports(self) -> list[dict[str, Any]]:
        out = []
        if not self.root.is_dir():
            return out
        for p in sorted(self.root.glob("*/report.json"), reverse=True):
            try:
                r = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            out.append({k: r.get(k) for k in ("id", "session", "round", "status", "started_at", "ended_at",
                                               "recorded_s", "headline", "applied")}
                       | {"accuracy": (r.get("accuracy") or {}).get("live", {}).get("accuracy"),
                          "cars": (r.get("counts") or {}).get("cars")})
        return out

    def report(self, rid: str) -> dict[str, Any] | None:
        if not rid or not set(rid) <= ID_CHARS:
            return None
        try:
            return json.loads((self.root / rid / "report.json").read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def frame_path(self, rid: str, name: str) -> Path | None:
        if not set(rid) <= ID_CHARS or "/" in name or ".." in name or not name.endswith(".jpg"):
            return None
        p = self.root / rid / "frames" / name
        return p if p.is_file() else None

    def recover(self) -> None:
        """At startup: create the review queue, and since a session can't survive a restart,
        mark an unfinished one interrupted and delete its recording."""
        if self.tcfg.reviewer == "claude-code":
            from .claude_code import queue_dir

            queue_dir(self.config.paths.data_dir)
        if not self.root.is_dir():
            return
        for p in self.root.glob("*/report.json"):
            try:
                r = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if r.get("status") == "running":
                r.update(status="interrupted", ended_at=r.get("ended_at") or p.stat().st_mtime,
                         headline="Interrupted: the device restarted during this round.")
                p.write_text(json.dumps(r, indent=2))
        qdir = self.root / "queue"
        for p in qdir.glob("*.json") if qdir.is_dir() else ():
            if not p.name.startswith("."):
                p.unlink(missing_ok=True)  # batches of a round that no longer exists
        if not self.tcfg.keep_footage:
            for meta in list(self.store.iter_meta()):
                if meta.get("trigger") == TRIGGER:
                    self.store.delete(meta["id"])

    # ------------------------------------------------------------ internals
    def _set(self, **fields: Any) -> None:
        with self._lock:
            self._status.update(fields)

    def _check_disk(self, duration: float) -> None:
        root = Path(self.config.paths.data_dir)
        root.mkdir(parents=True, exist_ok=True)
        u = shutil.disk_usage(root)
        need = self.config.recording.bitrate / 8 * duration * 1.2 + 200e6
        total = (u.used + u.free) or 1
        if (u.free - need) / total * 100 < self.config.recording.min_free_percent:
            raise TuneError(f"not enough free disk: {duration / 60:.0f} min of video needs about "
                            f"{need / 1e9:.1f} GB and the device keeps {self.config.recording.min_free_percent:.0f}% free")

    def _run(self, sid: str, duration: float) -> None:
        try:
            for rnd in range(1, max(1, self.tcfg.max_rounds) + 1):
                self._set(round=rnd)
                result = self._round(sid, rnd, duration)
                acc = result.get("accuracy")
                again = (result.get("status") == "done" and result.get("applied") and acc is not None
                         and acc < self.tcfg.target_accuracy and rnd < self.tcfg.max_rounds
                         and not self._stop_recording.is_set())
                if not again:
                    break
                log.info("tune: round %d at %.0f%%, under the %.0f%% target; another round", rnd, acc * 100,
                         self.tcfg.target_accuracy * 100)
        except Exception as exc:
            log.exception("tune: session failed")
            self._set(error=str(exc))
        finally:
            self.recorder.reserved_by = None
            self._set(active=False, phase="idle", message=None, ended_at=time.time())

    def _round(self, sid: str, rnd: int, duration: float) -> dict[str, Any]:
        tcfg = self.tcfg
        rid = f"{sid}-r{rnd}"
        rdir = self.root / rid
        (rdir / "frames").mkdir(parents=True, exist_ok=True)
        started = time.time()
        report: dict[str, Any] = {"id": rid, "session": sid, "round": rnd, "status": "running", "started_at": started,
                                  "duration_s": duration, "headline": "Recording"}
        self._write(rdir, report)
        ok, why = review.available(tcfg, self.config.paths.data_dir)
        reviewer = None
        if ok:
            try:
                reviewer = review.make_reviewer(tcfg, self.config.paths.data_dir)
            except Exception as exc:
                ok, why = False, f"could not start the reviewer: {exc}"
        ref = Reference(self.config, tcfg, rdir / "frames")
        chunk_s = min(tcfg.chunk_s, max(30.0, self.config.recording.max_duration_s - 10))
        n_chunks = max(1, math.ceil(duration / chunk_s))
        budget = max(1, tcfg.max_reviews // n_chunks)
        todo: queue.Queue = queue.Queue()
        records: list[dict[str, Any]] = []
        clip_ids: list[str] = []
        problems: list[str] = []
        self._set(phase="recording", chunk=0, chunks=n_chunks, reviewed=0, to_review=0, report=rid,
                  message=f"Recording chunk 1 of {n_chunks}")

        worker = threading.Thread(target=self._work, args=(todo, ref, reviewer, budget, records, problems),
                                  name="tune-review", daemon=True)
        worker.start()
        recorded = self._record(rid, n_chunks, chunk_s, duration, todo, clip_ids)
        todo.put(None)
        self._set(phase="reviewing", message="Reviewing the last chunks")
        while worker.is_alive():
            worker.join(timeout=1.0)
        if reviewer is not None and getattr(reviewer, "deferred", False) and not self._abort.is_set():
            self._set(message="Waiting for Claude Code to review the frames")
            reviewer.collect([s for r in records for s in r["samples"]], self._abort.is_set,
                             lambda n: self._set(reviewed=n))

        metas = [m for m in (self.store.read_meta(c) for c in clip_ids) if m]
        samples = [s for r in records for s in r["samples"]]
        counts = ref.counts()
        logs = analysis.live_logs(metas)
        gap = tcfg.pair_gap_s
        live_score = analysis.score(samples, logs, gap)
        changes: list[dict[str, Any]] = []
        replay_scores = None
        applied = False
        decision = ""
        labelled = sum(1 for s in samples if s.get("label"))
        if self._abort.is_set():
            decision = "Stopped before the analysis finished, so nothing was changed."
        elif reviewer is None:
            decision = f"Not tuned: no review ({why})."
        elif labelled < 10:
            decision = f"Not tuned: only {labelled} moments were reviewed, too few to go on."
        else:
            changes = analysis.fit(samples, logs, self.config, gap)
            if not changes:
                decision = "No change: the current thresholds already fit the review best."
            else:
                self._set(phase="replaying", message="Replaying the recording with the new settings")
                chunks = [(m, self.store.paths(m["id"])["video"],
                           {k: config_mod.Roi(**v) for k, v in (m.get("rois") or {}).items()} or dict(self.config.site.rois))
                          for m in metas]
                fields = ("northbound", "southbound", "deck", "bridge")
                try:
                    before, after = analysis.replay(chunks, [analysis.variant(self.config), analysis.variant(self.config, changes)],
                                                    should_stop=self._abort.is_set)
                    sb, sa = analysis.score(samples, before, gap, fields), analysis.score(samples, after, gap, fields)
                    gain = None if sb["accuracy"] is None or sa["accuracy"] is None else round(sa["accuracy"] - sb["accuracy"], 3)
                    replay_scores = {"before": sb, "after": sa, "gain": gain}
                except Exception as exc:
                    log.exception("tune: replay failed")
                    problems.append(f"replay failed: {exc}")
                    gain = None
                if self._abort.is_set():
                    decision = "Stopped during the replay, so nothing was changed."
                elif gain is None:
                    decision = "Not changed: the replay couldn't score the new settings."
                elif gain < tcfg.min_gain:
                    decision = (f"Not changed: the new settings scored {gain * 100:+.1f} points on the replay, "
                                f"under the {tcfg.min_gain * 100:.0f}-point bar.")
                elif not tcfg.auto_apply:
                    decision = f"Proposed only ([tune] auto_apply = false): {gain * 100:+.1f} points on the replay."
                else:
                    self._apply(changes, rid)
                    applied = True
                    decision = f"Applied: {gain * 100:+.1f} points on the replay. Undo with Reset tuning."

        disagreements = self._disagreements(samples, logs, gap, rdir)
        report.update({
            "status": "stopped" if self._abort.is_set() else "done",
            "ended_at": time.time(),
            "recorded_s": round(recorded, 1),
            "chunks": len(clip_ids),
            "reviewer": {"ok": reviewer is not None, "why": why, **(reviewer.usage() if reviewer else {})},
            "reviewed": labelled,
            "moments": len(samples),
            "counts": self._counts(counts, metas),
            "accuracy": {"live": live_score, "replay": replay_scores},
            "target_accuracy": tcfg.target_accuracy,
            "changes": changes,
            "applied": applied,
            "decision": decision,
            "disagreements": disagreements,
            "problems": problems,
            "rois": records[0]["rois"] if records else {k: vars(v) for k, v in self.config.site.rois.items()},
        })
        report["headline"] = self._headline(report)
        self._write(rdir, report)
        self._cleanup(clip_ids, rdir, disagreements)
        log.info("tune: round %s done: %s", rid, report["headline"])
        return {"status": report["status"], "applied": applied, "accuracy": live_score["accuracy"]}

    def _record(self, rid: str, n_chunks: int, chunk_s: float, duration: float,
                todo: queue.Queue, clip_ids: list[str]) -> float:
        rec = self.recorder
        rec.reserved_by = rid
        recorded = 0.0
        try:
            if rec.active_trigger not in (None, "manual"):
                rec.stop()  # an auto clip: the tuning chunks cover the same thing
            for i in range(n_chunks):
                if self._stop_recording.is_set():
                    break
                length = min(chunk_s, duration - recorded)
                if length < 5:
                    break
                self._set(chunk=i + 1, message=f"Recording chunk {i + 1} of {n_chunks}")
                try:
                    meta = rec.start(trigger=TRIGGER, label=None, note=f"self-tune {rid}")
                except Exception as exc:
                    self._set(error=f"could not record: {exc}")
                    log.warning("tune: could not start chunk: %s", exc)
                    break
                t0 = time.time()
                self._wait(length)
                saved = rec.stop()
                recorded += time.time() - t0
                if saved:
                    self.store.update(saved["id"], keep=True)  # the disk cleanup must not take it mid-round
                    clip_ids.append(saved["id"])
                    todo.put(saved["id"])
                else:
                    log.warning("tune: chunk %s was not saved", meta.get("id"))
        finally:
            rec.reserved_by = None
        return recorded

    def _work(self, todo: queue.Queue, ref: Reference, reviewer, budget: int,
              records: list[dict[str, Any]], problems: list[str]) -> None:
        _lower_priority()
        while True:
            cid = todo.get()
            if cid is None:
                return
            if self._abort.is_set():
                continue
            meta = self.store.read_meta(cid)
            if not meta:
                continue
            video = self.store.paths(cid)["video"]
            rois = {k: config_mod.Roi(**v) for k, v in (meta.get("rois") or {}).items()} or dict(self.config.site.rois)
            try:
                rec = ref.process(meta, video, rois, budget)
            except Exception as exc:
                log.exception("tune: reference pass failed on %s", cid)
                problems.append(f"{cid}: {exc}")
                continue
            for s in rec["samples"]:
                s["clip_id"] = cid
            records.append(rec)
            with self._lock:
                self._status["to_review"] = self._status.get("to_review", 0) + len(rec["samples"])
            if reviewer is None:
                continue
            boxes = list(rois)
            if getattr(reviewer, "deferred", False):
                # Claude Code picks these up from the queue; labels are collected at the end.
                reviewer.submit(ref.frames_dir.parent.name, cid, ref.frames_dir, boxes, self.tcfg.pair_gap_s,
                                rec["samples"])
                continue
            for s in rec["samples"]:
                if self._abort.is_set():
                    break
                try:
                    s["label"] = reviewer.label(ref.frames_dir / s["a"], ref.frames_dir / s["b"], boxes)
                except review.ReviewError as exc:
                    problems.append(str(exc))
                    reviewer = None
                    break
                with self._lock:
                    self._status["reviewed"] = self._status.get("reviewed", 0) + 1

    def _apply(self, changes: list[dict[str, Any]], rid: str) -> None:
        data: dict[str, dict[str, float]] = {}
        for c in changes:
            section, key = c["key"].split(".", 1)
            data.setdefault(section, {})[key] = c["new"]
        for dotted, (old, _new) in config_mod.apply_tuning(self.config, data).items():
            self.config.untuned.setdefault(dotted, old)
        config_mod.save_tuning(self.config, {c["key"]: c["new"] for c in changes}, note=f"last changed by round {rid}")
        log.info("tune: applied %s", ", ".join(f"{c['key']}={c['new']}" for c in changes))

    def _disagreements(self, samples, logs, gap, rdir: Path) -> list[dict[str, Any]]:
        """Moments where the detector and the review disagreed, spread over the fields."""
        found: dict[str, list[dict[str, Any]]] = {}
        for s in samples:
            label = s.get("label")
            if not label:
                continue
            j = analysis.judge(s, logs.get(s["clip_id"], []), gap)
            wrong = [f for f in analysis.FIELDS if j["ok"].get(f) is False]
            if not wrong:
                continue
            item = {"at": s["wall"], "a": s["a"], "b": s["b"], "fields": wrong, "live": j["live"],
                    "seen": {f: label.get(f) for f in analysis.FIELDS}, "note": label.get("note") or ""}
            found.setdefault(wrong[0], []).append(item)
        out: list[dict[str, Any]] = []
        groups = list(found.values())
        while len(out) < self.tcfg.keep_frames and any(groups):
            for g in groups:
                if g and len(out) < self.tcfg.keep_frames:
                    out.append(g.pop(0))
        return sorted(out, key=lambda d: d["at"])

    def _counts(self, ref_counts: dict[str, Any], metas: list[dict[str, Any]]) -> dict[str, Any]:
        """Cars and boats from the reference pass, plus what the detector itself reported."""
        seen: set[tuple] = set()
        events = []
        for m in metas:
            for d in m.get("detections") or []:
                e = d.get("event")
                if not e:
                    continue
                key = (e.get("type"), round(float(e.get("at") or 0), 1), e.get("direction"), e.get("leaf"), e.get("phase"))
                if key not in seen:
                    seen.add(key)
                    events.append(e)
        by_type: dict[str, int] = {}
        for e in events:
            by_type[e.get("type", "?")] = by_type.get(e.get("type", "?"), 0) + 1
        standstill = {lane: sum(1 for e in events if e.get("type") == "traffic" and e.get("direction") == lane
                                and e.get("level") == "standstill") for lane in LANES}
        phases = [e for e in events if e.get("type") == "phase"]
        return {
            **ref_counts,
            "boats_reference": len(ref_counts.get("boats") or []),
            "boats_detector": sum(1 for e in events if e.get("type") == "boat_started"),
            "boat_events": [{"at": e.get("at"), "direction": e.get("direction"), "confidence": e.get("confidence")}
                            for e in events if e.get("type") == "boat_started"],
            "openings": sum(1 for e in phases if e.get("phase") == "lifting"),
            "gates_down": sum(1 for e in phases if e.get("phase") == "gates_down"),
            "standstills": standstill,
            "events": by_type,
            "phases": [{"at": e.get("at"), "phase": e.get("phase")} for e in phases],
        }

    @staticmethod
    def _headline(r: dict[str, Any]) -> str:
        acc = r["accuracy"]["live"]["accuracy"]
        cars = (r["counts"].get("cars") or {})
        parts = []
        if acc is not None:
            parts.append(f"{acc * 100:.0f}% of reviewed readings right")
        if r["counts"].get("model"):
            parts.append(f"{cars.get('northbound', 0)} cars northbound, {cars.get('southbound', 0)} southbound")
        parts.append(f"{r['counts']['boats_reference']} boats")
        if r["applied"]:
            n = len(r["changes"])
            parts.append(f"{n} setting{'' if n == 1 else 's'} tuned")
        return ", ".join(parts)

    def _write(self, rdir: Path, report: dict[str, Any]) -> None:
        tmp = rdir / "report.json.tmp"
        tmp.write_text(json.dumps(report, indent=2, default=str))
        os.replace(tmp, rdir / "report.json")

    def _cleanup(self, clip_ids: list[str], rdir: Path, disagreements: list[dict[str, Any]]) -> None:
        if not self.tcfg.keep_footage:
            for cid in clip_ids:
                self.store.delete(cid)
        keep = {d["a"] for d in disagreements} | {d["b"] for d in disagreements}
        for p in (rdir / "frames").glob("*.jpg"):
            if p.name not in keep:
                p.unlink(missing_ok=True)
