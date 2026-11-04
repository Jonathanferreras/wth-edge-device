"""Phone alerts: a boat on its way, the gates going down, the bridge opening and closing.

Two parts, like the detectors:

* ``AlertRules`` is pure. Events and detector state in, at most a few ``Alert`` out. It
  decides *when* to tell someone and keeps that quiet: an alert only fires on a transition,
  one opening gives at most one "gates down", one "opening" and one "closed", the same AIS
  vessel is announced once, every kind has a cooldown, and there is a cap per hour and
  optional quiet hours.
* ``Notifier`` is a detector listener. It runs the rules and hands alerts to a worker
  thread that sends them, so a slow network never holds up the detector loop.

Channels, all free (secrets in ``/etc/wth/secrets.env``, never in the toml):

* ``ntfy``: push notifications through ntfy.sh (or your own server) to the ntfy phone
  app. No account. ``WTH_NTFY_TOPIC`` (treat the topic like a password), optional
  ``WTH_NTFY_TOKEN``.
* ``telegram``: a Telegram bot. ``WTH_TELEGRAM_TOKEN`` and ``WTH_TELEGRAM_CHAT_ID``.
* ``discord``: a channel webhook. ``WTH_DISCORD_WEBHOOK``.
* ``email``: SMTP, which also reaches carriers' email-to-SMS gateways
  (``5551234567@tmomail.net``). ``WTH_SMTP_USER``, ``WTH_SMTP_PASSWORD``,
  ``WTH_ALERT_EMAIL_TO`` (comma separated).
* ``console``: the log only, for a laptop.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

log = logging.getLogger(__name__)

KINDS = ("boat_approaching", "boat_seen", "gates_down", "opening", "closed")
# Phases that belong to an opening, from the gates going down until traffic flows again.
CYCLE_PHASES = ("gates_down", "lifting", "raised", "lowering", "releasing")
UP_PHASES = ("lifting", "raised", "lowering")
HEADING = {"e2w": "heading west", "w2e": "heading east"}
TAGS = {"boat_approaching": ["ship"], "boat_seen": ["ship"], "gates_down": ["no_entry"],
        "opening": ["bridge_at_night"], "closed": ["white_check_mark"], "test": ["wave"]}


@dataclass
class Alert:
    kind: str
    title: str
    message: str
    at: float
    tags: list[str] = field(default_factory=list)


def _minutes(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    m = max(1, round(seconds / 60))
    return f"{m} min"


class AlertRules:
    """Decides which detector events are worth a phone alert."""

    def __init__(self, cfg, device_id: str = "") -> None:
        self.cfg = cfg
        self.device_id = device_id
        self.wanted = {k.lower() for k in cfg.on}
        unknown = self.wanted - set(KINDS)
        if unknown:
            log.warning("notify.on: ignoring unknown alert kinds %s (known: %s)", sorted(unknown), ", ".join(KINDS))
        self._last_kind: dict[str, float] = {}
        self._last_vessel: dict[Any, float] = {}
        self._sent: list[float] = []
        # The opening in progress: when it started and what we already said about it.
        self._cycle_started: float | None = None
        self._cycle_said: set[str] = set()

    # ---- the decision ----
    def check(self, events: list[dict[str, Any]], st: dict[str, Any], now: float,
              hour: int | None = None) -> list[Alert]:
        candidates: list[Alert] = []
        for e in events:
            if e.get("type") == "phase":
                candidates += self._phase(e, st, now)
            elif e.get("type") == "boat_started":
                if a := self._boat_seen(e, now):
                    candidates.append(a)
        if a := self._ais(st, now):
            candidates.append(a)
        return [a for a in candidates if self._allow(a, now, hour)]

    def _phase(self, e: dict[str, Any], st: dict[str, Any], now: float) -> list[Alert]:
        phase, prev = e.get("phase"), e.get("previous")
        opening = st.get("opening") or {}
        wait = opening.get("wait") or {}
        out: list[Alert] = []
        if phase in CYCLE_PHASES and self._cycle_started is None:
            self._cycle_started, self._cycle_said = now, set()
        if phase == "gates_down" and "gates_down" not in self._cycle_said:
            self._cycle_said.add("gates_down")
            usual = wait.get("initialTotalMinutes")
            msg = "Traffic has stopped both ways, so the bridge is probably about to open."
            if usual:
                msg += f" An opening usually takes about {round(usual)} min."
            out.append(Alert("gates_down", "Bridge gates down", msg, now))
        elif phase in UP_PHASES and "opening" not in self._cycle_said:
            self._cycle_said.add("opening")
            boat = HEADING.get(e.get("boat_direction") or "")
            msg = "The bridge is lifting" + (f" for a boat {boat}" if boat else "") + "."
            left = wait.get("remainingMinutes")
            if left:
                msg += f" Traffic should move again in about {max(1, round(left))} min."
            out.append(Alert("opening", "Bridge opening", msg, now))
        elif phase == "flowing" and self._cycle_started is not None:
            # Only an opening that actually lifted gets a "closed"; a false alarm at the
            # gates ends quietly (the gates-down alert said "probably").
            if "opening" in self._cycle_said or prev in ("lowering", "releasing"):
                took = _minutes(now - self._cycle_started)
                out.append(Alert("closed", "Bridge closed", f"Traffic is moving again. The opening took about {took}.", now))
            self._cycle_started, self._cycle_said = None, set()
        return out

    def _boat_seen(self, e: dict[str, Any], now: float) -> Alert | None:
        # During an opening we already said why the bridge is up; one boat message is enough.
        if self._cycle_started is not None and "opening" in self._cycle_said:
            return None
        heading = HEADING.get(e.get("direction") or "")
        msg = "The camera sees a boat at the bridge" + (f", {heading}" if heading else "") + "."
        return Alert("boat_seen", "Boat at the bridge", msg, now)

    def _ais(self, st: dict[str, Any], now: float) -> Alert | None:
        v = (st.get("ais") or {}).get("approaching")
        if not v or "boat_approaching" not in self.wanted:
            return None
        eta = v.get("eta_s")
        if eta is None or eta > self.cfg.ais_max_eta_s:
            return None
        key = v.get("mmsi") or v.get("name")
        last = self._last_vessel.get(key)
        if last is not None and now - last < self.cfg.vessel_cooldown_s:
            return None
        self._last_vessel[key] = now
        # Forget vessels long gone, so the table doesn't grow forever.
        self._last_vessel = {k: t for k, t in self._last_vessel.items() if now - t < self.cfg.vessel_cooldown_s}
        name = (v.get("name") or "").strip() or "A vessel"
        dist = v.get("distance_m")
        where = f"{dist / 1609.34:.1f} mi" if dist and dist >= 800 else (f"{dist} m" if dist else "nearby")
        msg = f"{name} is {where} {v.get('side') or ''} of the bridge and closing, about {_minutes(eta)} out."
        return Alert("boat_approaching", "Boat approaching", " ".join(msg.split()), now)

    def _allow(self, a: Alert, now: float, hour: int | None) -> bool:
        if a.kind not in self.wanted:
            return False
        last = self._last_kind.get(a.kind)
        if last is not None and now - last < self.cfg.cooldown_s:
            log.info("alert %s skipped: cooldown", a.kind)
            return False
        start, end = self.cfg.quiet_hours
        if start != end:
            h = time.localtime(now).tm_hour if hour is None else hour
            quiet = start <= h < end if start < end else (h >= start or h < end)
            if quiet:
                log.info("alert %s skipped: quiet hours", a.kind)
                return False
        self._sent = [t for t in self._sent if now - t < 3600]
        if self.cfg.max_per_hour and len(self._sent) >= self.cfg.max_per_hour:
            log.warning("alert %s skipped: %d alerts in the last hour", a.kind, len(self._sent))
            return False
        self._sent.append(now)
        self._last_kind[a.kind] = now
        a.tags = TAGS.get(a.kind, [])
        return True


# ---------------------------------------------------------------- channels

class ChannelError(Exception):
    pass


def _env(*names: str) -> list[str]:
    values = [os.environ.get(n, "").strip() for n in names]
    missing = [n for n, v in zip(names, values) if not v]
    if missing:
        raise ChannelError(f"{', '.join(missing)} not set in /etc/wth/secrets.env")
    return values


def _post_json(url: str, body: dict[str, Any], timeout: float, headers: dict[str, str] | None = None) -> None:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "wth-edge", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200].decode(errors="replace")
        raise ChannelError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ChannelError(str(getattr(exc, "reason", exc))) from exc


def missing_secrets(channel: str) -> list[str]:
    """Env vars a channel needs that are not set. ``[]`` = ready."""
    need = {
        "ntfy": ["WTH_NTFY_TOPIC"],
        "telegram": ["WTH_TELEGRAM_TOKEN", "WTH_TELEGRAM_CHAT_ID"],
        "discord": ["WTH_DISCORD_WEBHOOK"],
        "email": ["WTH_SMTP_USER", "WTH_SMTP_PASSWORD", "WTH_ALERT_EMAIL_TO"],
    }.get(channel, [])
    return [n for n in need if not os.environ.get(n, "").strip()]


def send(channel: str, alert: Alert, cfg) -> None:
    """Send one alert on one channel. Raises ``ChannelError``."""
    title = alert.title
    t = cfg.timeout_s
    if channel == "console":
        log.info("ALERT %s: %s", title, alert.message)
    elif channel == "ntfy":
        (topic,) = _env("WTH_NTFY_TOPIC")
        body: dict[str, Any] = {"topic": topic, "title": title, "message": alert.message,
                                "tags": alert.tags, "priority": cfg.ntfy_priority}
        if cfg.click_url:
            body["click"] = cfg.click_url
        token = os.environ.get("WTH_NTFY_TOKEN", "").strip()
        _post_json(cfg.ntfy_server.rstrip("/"), body, t, {"Authorization": f"Bearer {token}"} if token else None)
    elif channel == "telegram":
        token, chat = _env("WTH_TELEGRAM_TOKEN", "WTH_TELEGRAM_CHAT_ID")
        text = f"{title}\n{alert.message}" + (f"\n{cfg.click_url}" if cfg.click_url else "")
        _post_json(f"https://api.telegram.org/bot{token}/sendMessage", {"chat_id": chat, "text": text}, t)
    elif channel == "discord":
        (hook,) = _env("WTH_DISCORD_WEBHOOK")
        _post_json(hook, {"content": f"**{title}**\n{alert.message}"}, t)
    elif channel == "email":
        user, password, to = _env("WTH_SMTP_USER", "WTH_SMTP_PASSWORD", "WTH_ALERT_EMAIL_TO")
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = user, to, title
        # Short and plain: carriers' SMS gateways cut long messages.
        msg.set_content(alert.message)
        try:
            ctx = ssl.create_default_context()
            if cfg.smtp_port == 465:
                server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=t, context=ctx)
            else:
                server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=t)
                server.starttls(context=ctx)
            with server:
                server.login(user, password)
                server.send_message(msg, to_addrs=[a.strip() for a in to.split(",") if a.strip()])
        except (smtplib.SMTPException, OSError) as exc:
            raise ChannelError(str(exc)) from exc
    else:
        raise ChannelError(f"unknown channel {channel!r} (ntfy, telegram, discord, email, console)")


# ---------------------------------------------------------------- listener

class Notifier:
    """A detector listener: rules on the detector thread, sending on its own thread."""

    def __init__(self, cfg, device_id: str = "") -> None:
        self.cfg = cfg
        self.rules = AlertRules(cfg, device_id)
        self.channels = [c.lower() for c in cfg.channels]
        self._queue: queue.Queue[Alert | None] = queue.Queue(maxsize=50)
        self._thread: threading.Thread | None = None
        self.sent = 0
        self.last: dict[str, Any] | None = None
        self.errors: dict[str, str] = {}

    def __call__(self, events: list[dict[str, Any]], st: dict[str, Any], now: float | None = None) -> None:
        now = time.time() if now is None else now
        for alert in self.rules.check(events, st, now):
            log.info("alert: %s: %s", alert.title, alert.message)
            try:
                self._queue.put_nowait(alert)
            except queue.Full:
                log.warning("alert queue full; dropping %s", alert.kind)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="notify", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout=self.cfg.timeout_s + 1)

    def deliver(self, alert: Alert) -> dict[str, str | None]:
        """Send on every channel now. Returns channel -> error (None = sent)."""
        results: dict[str, str | None] = {}
        for ch in self.channels:
            try:
                send(ch, alert, self.cfg)
                results[ch] = None
                self.errors.pop(ch, None)
            except Exception as exc:
                results[ch] = str(exc)
                self.errors[ch] = str(exc)
                log.warning("alert via %s failed: %s", ch, exc)
        if any(err is None for err in results.values()):
            self.sent += 1
        self.last = {"kind": alert.kind, "title": alert.title, "at": alert.at}
        return results

    def _run(self) -> None:
        while True:
            alert = self._queue.get()
            if alert is None:
                return
            self.deliver(alert)

    def status(self) -> dict[str, Any]:
        return {"channels": self.channels, "sent": self.sent, "last": self.last, "errors": dict(self.errors)}


def sample_alert() -> Alert:
    return Alert("test", "Watch The Hutch", "Test alert: phone alerts from the bridge camera are working.",
                 time.time(), TAGS["test"])
