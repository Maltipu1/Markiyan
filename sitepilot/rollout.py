"""Progressive rollouts with automatic rollback.

  dry run  ->  canary (1-2 sites)  ->  batches  ->  done
Every site is health-checked right after its update. The first failure stops the rollout and
puts every site already touched back on its previous release, newest first.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .core import Clock, EventLog, Notifier
from .world import World

CANARY = 2
BATCH = 4


@dataclass
class RolloutResult:
    version: str
    dry_run: bool
    status: str = "planned"            # planned | done | rolled_back | rejected
    stages: list[list[str]] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    failed_on: str | None = None
    reason: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class Rollouts:
    def __init__(self, world: World, clock: Clock, events: EventLog, notifier: Notifier):
        self.world, self.clock, self.events, self.notifier = world, clock, events, notifier
        self.history: list[RolloutResult] = []

    def plan(self, version: str, domains: list[str] | None = None) -> list[list[str]]:
        targets = [d for d in sorted(domains or self.world.sites) if self.world.sites[d].version != version]
        stages = [targets[:CANARY]]
        rest = targets[CANARY:]
        stages += [rest[i:i + BATCH] for i in range(0, len(rest), BATCH)]
        return [s for s in stages if s]

    def run(self, version: str, domains: list[str] | None = None, dry_run: bool = False) -> RolloutResult:
        res = RolloutResult(version, dry_run, stages=self.plan(version, domains))
        self.history.append(res)

        problems = []
        if version not in self.world.releases:
            problems.append(f"реліз {version} не існує")
        for d in sum(res.stages, []):
            # same rule as the monitor: one failed check is noise, two in a row is a real problem
            if not self.world.probe(d).ok and not self.world.probe(d).ok:
                problems.append(f"{d} вже зараз не працює — оновлювати лежачий сайт не можна")
        if problems:
            res.status, res.reason = "rejected", "; ".join(problems)
            self.events.add("rollout.rejected", f"Розкат {version} відхилено: {res.reason}", "warn")
            return res
        if dry_run:
            self.events.add("rollout.plan", f"Пробний прогін {version}: {len(sum(res.stages, []))} сайтів, "
                                            f"етапи {[len(s) for s in res.stages]}")
            return res

        previous: dict[str, str] = {}
        for n, stage in enumerate(res.stages):
            label = "канарка" if n == 0 else f"партія {n}"
            for d in stage:
                site = self.world.sites[d]
                previous[d] = site.version
                self.world.backup(d)
                self.world.deploy(d, site.server_id, version, site.template)
                probe = self.world.probe(d)
                if not probe.ok:
                    res.failed_on, res.reason = d, probe.error
                    self._rollback(previous, res)
                    return res
                res.updated.append(d)
            self.events.add("rollout.stage", f"{version}: {label} ({len(stage)}) — OK", "ok")
        res.status = "done"
        for d in res.updated:            # the new release is now what a restore should bring back
            self.world.backup(d)
        self.events.add("rollout.done", f"Реліз {version} на {len(res.updated)} сайтах", "ok")
        return res

    def _rollback(self, previous: dict[str, str], res: RolloutResult) -> None:
        self.events.add("rollout.failed", f"{res.failed_on} після {res.version}: {res.reason} — відкочую", "error")
        for d in reversed(list(previous)):
            site = self.world.sites[d]
            self.world.deploy(d, site.server_id, previous[d], site.template)
        res.status = "rolled_back"
        self.events.add("rollout.rolled_back", f"Повернуто {len(previous)} сайт(и) на попередній реліз", "ok")
        self.notifier.send(f"↩️ Реліз {res.version} зупинено на {res.failed_on} ({res.reason}). "
                           f"Відкочено {len(previous)} сайт(и), решту не чіпали.")
