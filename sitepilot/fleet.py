"""Wires the pieces together and drives simulated time."""
from __future__ import annotations

from .core import Clock, EventLog, make_notifier, Notifier
from .healing import Healer
from .monitor import Monitor
from .provisioning import Provisioner
from .rollout import Rollouts
from .triage import triage
from .world import World

TICK = 60  # one platform cycle = one simulated minute

SEED_DOMAINS = ["aurora-shop.test", "birch-store.test", "cobalt-market.test", "dune-goods.test",
                "ember-deals.test", "fjord-outlet.test", "granite-hub.test"]


class Platform:
    def __init__(self, echo: bool = False, notifier: Notifier | None = None):
        self.clock = Clock()
        self.events = EventLog(self.clock, echo=echo)
        self.notifier = notifier or make_notifier(self.clock)
        self.world = World(self.clock)
        self.provisioner = Provisioner(self.world, self.clock, self.events, self.notifier)
        self.monitor = Monitor(self.world, self.clock, self.events, self.notifier)
        self.healer = Healer(self.world, self.clock, self.events, self.notifier, self.provisioner)
        self.rollouts = Rollouts(self.world, self.clock, self.events, self.notifier)
        self.inbox: list[dict] = []

    def tick(self, n: int = 1) -> None:
        for _ in range(n):
            self.clock.advance(TICK)
            self.healer.tick()
            self.monitor.tick()

    def seed(self, domains: list[str] | None = None) -> None:
        self.notifier.muted = True   # seeding isn't news
        try:
            for d in domains or SEED_DOMAINS:
                self.provisioner.run(d)
        finally:
            self.notifier.muted = False

    def triage_email(self, text: str, result=None) -> dict:
        """`result` lets the web server run the (slow) model call outside its state lock."""
        result = result or triage(text)
        known = [d for d in result.domains if d in self.world.sites]
        item = {"time": self.clock.now(), **result.to_dict(), "known_domains": known}
        self.inbox.insert(0, item)
        level = "error" if result.urgency == "critical" else "warn" if result.needs_alert else "info"
        self.events.add("mail." + result.category, f"Лист: {result.summary}", level, source=result.source)
        if result.needs_alert:
            where = f" ({', '.join(result.domains)})" if result.domains else ""
            flags = f" ⚠️ {'; '.join(result.red_flags)}." if result.red_flags else ""
            self.notifier.send(f"📨 {result.category.upper()}{where}: {result.summary}. "
                               f"Дедлайн: {result.deadline or 'не вказано'}.{flags} Що робити: {result.action}")
        return item

    def state(self) -> dict:
        snap = self.world.snapshot()
        for s in snap["sites"]:
            s["status"] = self.monitor.status(s["domain"])
        return {
            "time": self.clock.now(),
            **snap,
            "incidents": self.monitor.open_incidents(),
            "monitor_glitch": self.monitor.in_glitch,
            "jobs": [j.to_dict() for j in self.provisioner.jobs.values()],
            "rollouts": [r.to_dict() for r in self.rollouts.history[-5:]][::-1],
            "notifications": self.notifier.sent[-30:][::-1],
            "events": self.events.recent(80),
            "inbox": self.inbox[:10],
        }
