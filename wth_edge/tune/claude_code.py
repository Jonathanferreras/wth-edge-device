"""Claude Code on the Pi as the reviewer.

The service runs as ``wth`` and Claude Code is logged in as you, so the two meet in a
folder: ``<data_dir>/tune/queue``.

* The service drops one batch per recorded chunk there (``<round>__<clip>.json``: the frame
  pairs to look at, the box names, the gap between the frames).
* ``wth-edge tune-review``, run every couple of minutes as you by
  ``wth-tune-review.timer``, picks up each batch, runs ``claude -p`` on a few moments at a
  time (it reads the frames with its Read tool and answers in a fixed JSON shape), and
  writes ``<round>__<clip>.labels.json`` next to it. It also leaves ``.reviewer.json`` so
  the panel can tell whether the timer is alive.
* The service reads the labels back and carries on with the tuning.

The queue folder is group ``wth`` and group-writable (setgid), and install.sh puts your user
in group ``wth`` for the timer, so both sides can read and write it.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .review import SCHEMA, SYSTEM

log = logging.getLogger(__name__)

QUEUE = "queue"
HEARTBEAT = ".reviewer.json"
LABELS = ".labels.json"
STALE_S = 1800.0

MOMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, **SCHEMA["properties"]},
                "required": ["id", *SCHEMA["required"]],
                "additionalProperties": False,
            },
        }
    },
    "required": ["moments"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- service side
def queue_dir(data_dir: str | Path) -> Path:
    """The queue folder, created by the service: group-writable and setgid, so the review
    timer (your user, in group wth) can write labels there."""
    q = Path(data_dir) / "tune" / QUEUE
    q.mkdir(parents=True, exist_ok=True)
    try:
        if q.stat().st_mode & 0o2070 != 0o2070:
            os.chmod(q, 0o2775)
    except OSError:
        pass
    return q


def submit(qdir: Path, rid: str, clip_id: str, frames_dir: Path, boxes: list[str], gap: float,
           samples: list[dict[str, Any]]) -> Path:
    batch = qdir / f"{rid}__{clip_id}.json"
    body = {"round": rid, "clip_id": clip_id, "frames_dir": str(frames_dir.resolve()), "boxes": boxes, "gap_s": gap,
            "submitted_at": time.time(),
            "moments": [{"id": moment_id(s), "a": s["a"], "b": s["b"]} for s in samples]}
    tmp = batch.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, indent=1))
    os.chmod(tmp, 0o664)
    os.replace(tmp, batch)
    return batch


def moment_id(sample: dict[str, Any]) -> str:
    return sample["a"].rsplit(".", 1)[0]


def labels_path(batch: Path) -> Path:
    return batch.with_name(batch.name[: -len(".json")] + LABELS)


def read_labels(batch: Path) -> dict[str, Any] | None:
    try:
        return json.loads(labels_path(batch).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def heartbeat(qdir: Path) -> dict[str, Any] | None:
    try:
        return json.loads((qdir / HEARTBEAT).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def available(data_dir: str | Path) -> tuple[bool, str]:
    hb = heartbeat(Path(data_dir) / "tune" / QUEUE)
    if hb is None:
        return False, "the Claude Code review timer hasn't run on this Pi yet (sudo ./install.sh sets it up)"
    age = time.time() - float(hb.get("at") or 0)
    if age > STALE_S:
        return False, f"the Claude Code review timer last ran {age / 60:.0f} min ago (check wth-tune-review.timer)"
    if hb.get("error"):
        return False, f"Claude Code: {hb['error']}"
    return True, f"Claude Code as {hb.get('user', '?')}"


# ---------------------------------------------------------------- reviewer side
def find_claude(command: str = "") -> str | None:
    for c in (command, "claude"):
        if c and (found := shutil.which(c)):
            return found
    local = Path.home() / ".local" / "bin" / "claude"
    return str(local) if local.is_file() else None


def prompt_for(batch: dict[str, Any], moments: list[dict[str, Any]]) -> str:
    frames = Path(batch["frames_dir"])
    lines = [SYSTEM, "",
             f"Boxes on this camera: {', '.join(batch['boxes'])}.",
             f"Below are {len(moments)} moments. For each one, use the Read tool to look at both image files "
             f"(frame A, then frame B {batch['gap_s']:g} seconds later), then label it. Don't edit or create files. "
             "Answer with one entry per moment, using its id.", ""]
    for m in moments:
        lines.append(f"- id {m['id']}: A = {frames / m['a']}  B = {frames / m['b']}")
    return "\n".join(lines)


def _clean(entry: dict[str, Any]) -> dict[str, Any]:
    """Keep only well-formed fields; anything odd becomes unclear."""
    out: dict[str, Any] = {}
    props = SCHEMA["properties"]
    for lane in ("northbound", "southbound"):
        v = entry.get(lane) if isinstance(entry.get(lane), dict) else {}
        out[lane] = {k: (v.get(k) if v.get(k) in props[lane]["properties"][k]["enum"] else "unclear")
                     for k in ("motion", "amount")}
    for k in ("deck", "bridge", "boat"):
        out[k] = entry.get(k) if entry.get(k) in props[k]["enum"] else "unclear"
    out["note"] = str(entry.get("note") or "")[:200]
    return out


def run_claude(claude: str, prompt: str, frames_dir: str, model: str = "", timeout: float = 900,
               run: Callable = subprocess.run) -> dict[str, Any]:
    cmd = [claude, "-p", prompt, "--output-format", "json", "--json-schema", json.dumps(MOMENTS_SCHEMA),
           "--allowedTools", "Read", "--add-dir", frames_dir]
    if model:
        cmd += ["--model", model]
    proc = run(cmd, capture_output=True, text=True, timeout=timeout, cwd=frames_dir)
    try:
        out = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        out = {}
    if proc.returncode != 0 or out.get("is_error"):
        msg = (out.get("result") or proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise RuntimeError(msg[:300])
    return out


def review_pending(tcfg, data_dir: str | Path, claude: str | None = None, run: Callable = subprocess.run,
                   max_batches: int | None = None) -> int:
    """Label every batch waiting in the queue. Returns the number of moments labelled."""
    qdir = Path(data_dir) / "tune" / QUEUE
    if not qdir.is_dir() or not os.access(qdir, os.W_OK):
        # The service creates it (group wth, setgid, group-writable) when it starts.
        log.error("can't write %s: restart wth-edge (it creates the queue) and check you're in group wth", qdir)
        return 0
    claude = claude or find_claude(tcfg.claude_command)
    beat = {"at": time.time(), "user": getpass.getuser(), "claude": claude, "error": None}
    if claude is None:
        beat["error"] = "claude is not installed for this user (curl -fsSL https://claude.ai/install.sh | bash)"
    _write(qdir / HEARTBEAT, beat)
    if claude is None:
        log.error(beat["error"])
        return 0
    done = 0
    batches = sorted(p for p in qdir.glob("*.json") if not p.name.endswith(LABELS) and not p.name.startswith("."))
    for path in batches[:max_batches] if max_batches else batches:
        try:
            batch = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        state = read_labels(path) or {"done": False, "labels": {}, "errors": [], "runs": 0,
                                      "input_tokens": 0, "output_tokens": 0}
        if state.get("done"):
            continue
        todo = [m for m in batch["moments"] if m["id"] not in state["labels"]]
        size = max(1, int(tcfg.claude_code_batch))
        for i in range(0, len(todo), size):
            group = todo[i:i + size]
            try:
                out = run_claude(claude, prompt_for(batch, group), batch["frames_dir"], tcfg.claude_code_model,
                                 tcfg.claude_code_timeout_s, run)
            except Exception as exc:  # a failed run: note it, leave these moments for the next run
                state["errors"].append(f"{time.strftime('%H:%M:%S')} {exc}")
                state["errors"] = state["errors"][-10:]
                log.warning("claude run failed: %s", exc)
                beat["error"] = str(exc)[:200]
                _write(qdir / HEARTBEAT, beat)
                _write(labels_path(path), state)
                if _limit_hit(str(exc)):
                    return done  # usage limit: stop until the next timer run
                continue
            state["runs"] += 1
            usage = out.get("usage") or {}
            state["input_tokens"] += sum(int(usage.get(k) or 0) for k in
                                         ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            state["output_tokens"] += int(usage.get("output_tokens") or 0)
            wanted = {m["id"] for m in group}
            for entry in (out.get("structured_output") or {}).get("moments") or []:
                if isinstance(entry, dict) and entry.get("id") in wanted:
                    state["labels"][entry["id"]] = _clean(entry)
                    done += 1
            _write(labels_path(path), state)
        # Moments Claude skipped twice in a row aren't worth chasing: finish the batch.
        missing = [m for m in batch["moments"] if m["id"] not in state["labels"]]
        state["tries"] = state.get("tries", 0) + 1
        state["done"] = not missing or state["tries"] >= 3
        _write(labels_path(path), state)
    beat["finished_at"] = time.time()
    _write(qdir / HEARTBEAT, beat)
    return done


def _limit_hit(msg: str) -> bool:
    m = msg.lower()
    return "limit" in m and ("usage" in m or "rate" in m or "reached" in m)


def _write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    try:
        os.chmod(tmp, 0o664)
    except OSError:
        pass
    os.replace(tmp, path)


class QueueReviewer:
    """The service's end: hand chunks to the queue, then wait for the labels."""

    deferred = True
    poll_s = 5.0

    give_up_s = 1200.0  # stop waiting when the review timer hasn't checked in for this long

    def __init__(self, tcfg, data_dir: str | Path) -> None:
        self.tcfg = tcfg
        self.data_dir = data_dir
        self.qdir = queue_dir(data_dir)
        self.batches: list[Path] = []
        self.runs = self.input_tokens = self.output_tokens = 0
        self.errors: list[str] = []

    def submit(self, rid: str, clip_id: str, frames_dir: Path, boxes: list[str], gap: float,
               samples: list[dict[str, Any]]) -> None:
        if samples:
            self.batches.append(submit(self.qdir, rid, clip_id, frames_dir, boxes, gap, samples))

    def progress(self) -> tuple[int, bool]:
        """(moments labelled so far, every batch finished)."""
        n, finished = 0, True
        for b in self.batches:
            st = read_labels(b) or {}
            n += len(st.get("labels") or {})
            finished = finished and bool(st.get("done"))
        return n, finished

    def collect(self, samples: list[dict[str, Any]], should_stop: Callable[[], bool],
                on_progress: Callable[[int], None] | None = None) -> None:
        """Wait (up to ``review_wait_s``) for the batches, then put the labels on the samples
        and clear the batches out of the queue."""
        deadline = time.time() + self.tcfg.review_wait_s
        last_ok = time.time()
        while self.batches and not should_stop() and time.time() < deadline:
            n, finished = self.progress()
            if on_progress:
                on_progress(n)
            if finished:
                break
            if available(self.data_dir)[0] or n:
                last_ok = time.time()
            elif time.time() - last_ok > self.give_up_s:
                log.warning("tune: the Claude Code review timer isn't running; not waiting for labels")
                break
            time.sleep(self.poll_s)
        labels: dict[str, Any] = {}
        for b in self.batches:
            st = read_labels(b) or {}
            labels.update(st.get("labels") or {})
            self.runs += int(st.get("runs") or 0)
            self.input_tokens += int(st.get("input_tokens") or 0)
            self.output_tokens += int(st.get("output_tokens") or 0)
            self.errors += st.get("errors") or []
            for p in (b, labels_path(b)):
                p.unlink(missing_ok=True)
        for s in samples:
            if (lab := labels.get(moment_id(s))) is not None:
                s["label"] = lab

    def usage(self) -> dict[str, Any]:
        return {"model": "Claude Code" + (f" ({self.tcfg.claude_code_model})" if self.tcfg.claude_code_model else ""),
                "calls": self.runs, "failures": len(self.errors), "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "errors": self.errors[-5:]}
