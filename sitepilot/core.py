"""Shared primitives: a controllable clock, the event log and notifiers."""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field


class Clock:
    """Simulated time in seconds. Everything reads time from here, so tests and the demo
    can fast-forward an hour in a millisecond."""

    def __init__(self, start: float = 0.0):
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def fmt_duration(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 60:
        return f"{m} хв"
    return f"{m // 60} год {m % 60} хв"


def fmt_ts(seconds: float) -> str:
    m = int(seconds // 60)
    return f"T+{m // 60:02d}:{m % 60:02d}"


@dataclass
class Event:
    ts: float
    kind: str
    message: str
    level: str = "info"          # info | warn | error | ok
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ts": self.ts, "time": fmt_ts(self.ts), "kind": self.kind,
                "message": self.message, "level": self.level, "data": self.data}


class EventLog:
    def __init__(self, clock: Clock, echo: bool = False, limit: int = 2000):
        self.clock = clock
        self.echo = echo
        self.limit = limit
        self.items: list[Event] = []

    def add(self, kind: str, message: str, level: str = "info", **data) -> Event:
        ev = Event(self.clock.now(), kind, message, level, data)
        self.items.append(ev)
        if len(self.items) > self.limit:
            del self.items[: len(self.items) - self.limit]
        if self.echo:
            mark = {"info": " ", "warn": "!", "error": "✗", "ok": "✓"}.get(level, " ")
            print(f"  {fmt_ts(ev.ts)} {mark} [{kind}] {message}")
        return ev

    def of_kind(self, prefix: str) -> list[Event]:
        return [e for e in self.items if e.kind.startswith(prefix)]

    def recent(self, n: int = 100) -> list[dict]:
        return [e.to_dict() for e in self.items[-n:]][::-1]


class Notifier:
    """Collects outgoing messages. Subclasses actually deliver them somewhere."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.sent: list[dict] = []
        self.muted = False

    def send(self, text: str, audience: str = "ops") -> None:
        if self.muted:
            return
        self.sent.append({"ts": self.clock.now(), "time": fmt_ts(self.clock.now()),
                          "audience": audience, "text": text})
        self.deliver(text, audience)

    def deliver(self, text: str, audience: str) -> None:  # pragma: no cover - overridden
        pass


class TelegramNotifier(Notifier):
    """Sends to a Telegram chat when SITEPILOT_TG_TOKEN and SITEPILOT_TG_CHAT are set.
    Delivery errors never break the platform — the message is still kept in `sent`."""

    def __init__(self, clock: Clock, token: str, chat_id: str):
        super().__init__(clock)
        self.token, self.chat_id = token, chat_id

    def deliver(self, text: str, audience: str) -> None:
        body = urllib.parse.urlencode({"chat_id": self.chat_id, "text": f"[{audience}] {text}"}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{self.token}/sendMessage", body, timeout=10)
        except OSError as e:
            print(f"telegram delivery failed: {e}")


def make_notifier(clock: Clock) -> Notifier:
    token, chat = os.getenv("SITEPILOT_TG_TOKEN"), os.getenv("SITEPILOT_TG_CHAT")
    if token and chat:
        return TelegramNotifier(clock, token, chat)
    return Notifier(clock)


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
