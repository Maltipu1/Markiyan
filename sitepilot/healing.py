"""Self-healing: when a server dies, move its sites somewhere healthy before customers notice.

A server is declared dead after DEAD_AFTER failed pings in a row (one lost ping is not a reason
to move production). Its sites are restored from the latest backup onto the least-loaded healthy
server — on a different provider when possible — DNS is switched and each site is verified.
"""
from __future__ import annotations

from .core import Clock, EventLog, Notifier, fmt_duration
from .provisioning import Provisioner
from .world import ProviderError, World

DEAD_AFTER = 2


class Healer:
    def __init__(self, world: World, clock: Clock, events: EventLog, notifier: Notifier,
                 provisioner: Provisioner):
        self.world, self.clock, self.events, self.notifier = world, clock, events, notifier
        self.provisioner = provisioner
        self.misses: dict[str, int] = {}
        self.enabled = True

    def tick(self) -> None:
        for srv in list(self.world.servers.values()):
            if srv.draining or not srv.sites:
                continue
            if self.world.server_ping(srv.id):
                self.misses.pop(srv.id, None)
                continue
            self.misses[srv.id] = self.misses.get(srv.id, 0) + 1
            if self.misses[srv.id] == 1:
                self.events.add("server.miss", f"{srv.id} не відповів на пінг — чекаю підтвердження", "warn",
                                server=srv.id)
            if self.misses[srv.id] >= DEAD_AFTER and self.enabled:
                self.evacuate(srv.id)

    def evacuate(self, server_id: str) -> dict:
        src = self.world.servers[server_id]
        src.draining = True
        started = self.clock.now()
        self.events.add("heal.start", f"{server_id} ({src.provider}) мертвий — переношу {len(src.sites)} сайт(и)",
                        "error", server=server_id)
        moved, failed = [], []
        for domain in sorted(src.sites):
            try:
                target = self.provisioner.ensure_server(avoid_provider=src.provider, exclude={server_id})
                version = self.world.restore(domain, target)
                self.world.set_dns(domain, target)
                probe = self.world.probe(domain)
                if not probe.ok:
                    raise ProviderError(probe.error)
                moved.append((domain, target))
                self.events.add("heal.moved", f"{domain} → {target} (з бекапу, реліз {version})", "ok",
                                domain=domain, server=target)
            except ProviderError as e:
                failed.append(domain)
                self.events.add("heal.failed", f"{domain}: не вдалося перенести — {e}", "error", domain=domain)
        took = fmt_duration(self.clock.now() - started + 60 * DEAD_AFTER)
        targets = ", ".join(sorted({t for _, t in moved})) or "—"
        msg = (f"🛠 Сервер {server_id} ({src.provider}) впав. Перенесено сайтів: {len(moved)} → {targets} "
               f"(від виявлення ~{took}).")
        if failed:
            msg += f" Не вдалося: {', '.join(failed)} — потрібна людина."
        self.notifier.send(msg)
        return {"moved": moved, "failed": failed}
