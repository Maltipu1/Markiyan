"""Uptime monitor that doesn't cry wolf.

Rules (each one came from a real false alarm):
  * one failed check is noise — an incident opens only after CONFIRM_CHECKS failures in a row;
  * people are notified only when the site has been down for NOTIFY_AFTER, and again when it recovers
    (no "recovered" message if nobody was told it was down);
  * if a large share of the fleet fails at the same moment, the problem is almost certainly the
    monitor's own network — we log it and wait instead of paging everyone.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .core import Clock, EventLog, Notifier, fmt_duration
from .world import World

CONFIRM_CHECKS = 2
NOTIFY_AFTER = 5 * 60
MASS_SHARE = 0.6
MASS_MIN = 3


@dataclass
class SiteState:
    fails: int = 0
    first_fail: float | None = None
    incident: bool = False
    notified: bool = False
    last_error: str = ""
    last_ms: int = 0


class Monitor:
    def __init__(self, world: World, clock: Clock, events: EventLog, notifier: Notifier):
        self.world, self.clock, self.events, self.notifier = world, clock, events, notifier
        self.state: dict[str, SiteState] = {}
        self.in_glitch = False

    def watched(self) -> list[str]:
        return sorted(self.world.sites)

    def tick(self) -> None:
        domains = self.watched()
        if not domains:
            return
        results = {d: self.world.probe(d) for d in domains}
        bad = [d for d, r in results.items() if not r.ok]

        if len(bad) >= max(MASS_MIN, math.ceil(MASS_SHARE * len(domains))):
            if not self.in_glitch:
                self.in_glitch = True
                self.events.add("monitor.glitch",
                                f"{len(bad)} з {len(domains)} сайтів не відповідають одночасно — схоже на "
                                f"проблему самого монітора, тривоги не надсилаю", "warn")
            return  # counters stay frozen, nothing opens or closes during a glitch
        if self.in_glitch:
            self.in_glitch = False
            self.events.add("monitor.glitch_over", "Монітор знову бачить мережу", "ok")

        now = self.clock.now()
        for d, r in results.items():
            st = self.state.setdefault(d, SiteState())
            st.last_ms = r.ms
            if r.ok:
                if st.incident:
                    took = now - (st.first_fail or now)
                    self.events.add("site.up", f"{d} знову працює (лежав {fmt_duration(took)})", "ok", domain=d)
                    if st.notified:
                        self.notifier.send(f"✅ Сайт {d} знову працює (не відкривався {fmt_duration(took)}).")
                elif st.fails:
                    self.events.add("site.blip", f"{d}: одна невдала перевірка ({st.last_error}) — шум, ігнорую",
                                    domain=d)
                self.state[d] = SiteState(last_ms=r.ms)
                continue

            st.fails += 1
            st.last_error = r.error
            if st.first_fail is None:
                st.first_fail = now
            if st.fails == CONFIRM_CHECKS and not st.incident:
                st.incident = True
                self.events.add("site.down", f"{d} не відповідає ({r.error}) — відкрито інцидент", "error",
                                domain=d, error=r.error)
            if st.incident and not st.notified and now - st.first_fail >= NOTIFY_AFTER:
                st.notified = True
                self.notifier.send(f"🔴 Сайт {d} не відкривається вже {fmt_duration(now - st.first_fail)}. "
                                   f"Причина: {r.error}. Команда вже займається.")
                self.events.add("site.alert", f"{d}: лежить ≥ {fmt_duration(NOTIFY_AFTER)}, людей сповіщено",
                                "error", domain=d)

    def open_incidents(self) -> list[dict]:
        now = self.clock.now()
        return [{"domain": d, "since": s.first_fail, "for": fmt_duration(now - (s.first_fail or now)),
                 "error": s.last_error, "notified": s.notified}
                for d, s in sorted(self.state.items()) if s.incident]

    def status(self, domain: str) -> str:
        st = self.state.get(domain)
        if not st:
            return "unknown"
        if st.incident:
            return "down"
        return "warn" if st.fails else "up"
