"""Claude as the reviewer: label a pair of frames the way a person watching would.

Two ways to reach Claude: Claude Code on the Pi, logged in as you (``claude_code.py``, the
default), or the Anthropic API from inside the service (``ClaudeReviewer`` below,
``[tune] reviewer = "api"``). Both use the instructions and answer shape defined here.

Each moment is two frames ``pair_gap_s`` apart with the detector boxes drawn and named.
Two frames, because from one still a queue at the gates and moving traffic look alike.
Claude answers in a fixed JSON shape (structured outputs), so labels compare directly
with what the detector logged.

The API reviewer needs the ``anthropic`` package and ``ANTHROPIC_API_KEY`` in the service's environment
(``/etc/wth/secrets.env``). Without them the loop still records, counts and reports; it
just can't tell whether the detector was right, so it doesn't tune.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

LANE = {
    "type": "object",
    "properties": {
        "motion": {"type": "string", "enum": ["empty", "moving", "stopped", "unclear"]},
        "amount": {"type": "string", "enum": ["none", "light", "moderate", "heavy", "unclear"]},
    },
    "required": ["motion", "amount"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "northbound": LANE,
        "southbound": LANE,
        "deck": {"type": "string", "enum": ["clear", "cars", "unclear"]},
        "bridge": {"type": "string", "enum": ["down", "raised", "unclear"]},
        "boat": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "note": {"type": "string"},
    },
    "required": ["northbound", "southbound", "deck", "bridge", "boat", "note"],
    "additionalProperties": False,
}

SYSTEM = """You check the work of a camera that watches a bascule drawbridge from a window.
A detector reads the scene through boxes drawn on the frame. Each box has its name written
at its top-left corner:

- northbound, southbound: the two traffic lanes leading onto the bridge.
- deck: the roadway on the bridge span itself.
- bridge, far_leaf: the two bridge leaves (the halves of the deck that lift to let boats through).
- boat, boat_near: the river, where boats pass.

You get two frames of the same view, A and then B, a couple of seconds apart. Judge only
what is inside the named boxes, and compare A with B to tell moving from stopped.

For each lane:
- motion: "empty" (no vehicles in the box), "moving" (vehicles in the box are driving: they
  are in different places in B than in A), "stopped" (vehicles are there but in the same
  place in both frames, e.g. queued at the gates), or "unclear".
- amount: how much of the lane is taken by vehicles: "none", "light" (one or two), "moderate"
  (several, gaps between them), "heavy" (the lane is mostly full), or "unclear".
deck: "clear" (no vehicle on the bridge span in either frame) or "cars".
bridge: "down" (both leaves flat, the road continuous) or "raised" (either leaf tilted up at
any angle, or the gates down and the span lifting).
boat: "yes" if a boat or ship is on the water inside a boat box in either frame, else "no".

Say "unclear" whenever you can't tell: night, glare, rain on the glass, a box that isn't on
the frame, or something blocking the view. A wrong confident answer is worse than "unclear".
Use note for anything odd you notice about a box (for example, a box that misses the lane or
takes in a pole or trees), in under 25 words; otherwise leave it empty."""


def available(tcfg, data_dir=None) -> tuple[bool, str]:
    """Whether the reviewer can run here, and if not, why (shown on the panel)."""
    if tcfg.reviewer == "claude-code":
        from . import claude_code

        return claude_code.available(data_dir or "/var/lib/wth")
    if tcfg.reviewer not in ("api", "claude"):
        return False, "review is off ([tune] reviewer = \"none\")"
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False, "the anthropic package is not installed (sudo /opt/wth-edge/.venv/bin/pip install anthropic)"
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return False, "no ANTHROPIC_API_KEY in /etc/wth/secrets.env"
    return True, f"Claude ({tcfg.model})"


def _image(path: Path) -> dict[str, Any]:
    data = base64.standard_b64encode(path.read_bytes()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


class ClaudeReviewer:
    def __init__(self, tcfg, client=None) -> None:
        self.tcfg = tcfg
        if client is None:
            import anthropic

            client = anthropic.Anthropic(max_retries=4, timeout=120.0)
        self.client = client
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0
        self.failures = 0

    def label(self, a: Path, b: Path, boxes: list[str]) -> dict[str, Any] | None:
        """Labels for one moment, or None when the call failed or was declined."""
        gap = self.tcfg.pair_gap_s
        content = [
            {"type": "text", "text": "Frame A:"},
            _image(a),
            {"type": "text", "text": f"Frame B, {gap:g} seconds later:"},
            _image(b),
            {"type": "text", "text": "Boxes on this camera: " + ", ".join(boxes) + ". Label what you see."},
        ]
        self.calls += 1
        try:
            response = self.client.beta.messages.create(
                model=self.tcfg.model,
                max_tokens=2048,
                system=SYSTEM,
                messages=[{"role": "user", "content": content}],
                output_config={"effort": self.tcfg.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
                # If the model declines, the API retries on a fallback model in the same call.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except Exception as exc:
            self.failures += 1
            log.warning("tune: review call failed: %s", exc)
            if self.failures >= 5 and self.failures > self.calls // 2:
                raise ReviewError(f"Claude review keeps failing: {exc}") from exc
            return None
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.input_tokens += getattr(usage, "input_tokens", 0) or 0
            self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        if response.stop_reason in ("refusal", "max_tokens"):
            log.warning("tune: review stopped (%s)", response.stop_reason)
            return None
        text = next((blk.text for blk in response.content if blk.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.warning("tune: review answer was not JSON: %.120s", text)
            return None

    def usage(self) -> dict[str, Any]:
        return {"model": self.tcfg.model, "calls": self.calls, "failures": self.failures,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


class ReviewError(RuntimeError):
    pass


def make_reviewer(tcfg, data_dir):
    """The reviewer for ``[tune] reviewer``: Claude Code on the Pi through the review queue
    ("claude-code"), or the API from inside the service ("api")."""
    if tcfg.reviewer == "claude-code":
        from .claude_code import QueueReviewer

        return QueueReviewer(tcfg, data_dir)
    return ClaudeReviewer(tcfg)
