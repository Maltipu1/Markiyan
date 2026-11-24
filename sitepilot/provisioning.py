"""From "we need a site on example.test" to a live, monitored HTTPS site — without a human.

The pipeline is a list of idempotent steps. Each step first checks whether its work is already
done, so a job that crashed halfway can simply be run again. Transient provider errors are
retried, and if a provider keeps failing we move on to the next one.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .core import Clock, EventLog, Notifier
from .world import PROVIDERS, ProviderError, World

MAX_TRIES = 3
CURRENT_RELEASE = "1.0.0"


@dataclass
class Job:
    domain: str
    template: str = "landing"
    status: str = "queued"           # queued | running | done | failed
    steps: list[dict] = field(default_factory=list)
    server_id: str | None = None
    error: str = ""

    def to_dict(self) -> dict:
        return {"domain": self.domain, "template": self.template, "status": self.status,
                "server": self.server_id, "steps": self.steps, "error": self.error}


class Provisioner:
    def __init__(self, world: World, clock: Clock, events: EventLog, notifier: Notifier):
        self.world, self.clock, self.events, self.notifier = world, clock, events, notifier
        self.jobs: dict[str, Job] = {}
        self.release = CURRENT_RELEASE

    # ---- server placement ------------------------------------------------------------
    def pick_server(self, exclude: set[str] | None = None) -> str | None:
        """Least-loaded healthy server with a free slot (spreads sites so one dead box hurts less)."""
        exclude = exclude or set()
        candidates = [s for s in self.world.servers.values()
                      if s.healthy and not s.draining and s.free > 0 and s.id not in exclude]
        if not candidates:
            return None
        return min(candidates, key=lambda s: (len(s.sites), s.id)).id

    def new_server(self, avoid_provider: str | None = None) -> str:
        """Order a server, failing over across providers. Raises ProviderError if all are down."""
        order = [p for p in PROVIDERS if p != avoid_provider] + ([avoid_provider] if avoid_provider else [])
        errors = []
        for provider in order:
            for attempt in range(1, MAX_TRIES + 1):
                try:
                    srv = self.world.create_server(provider)
                    self.events.add("server.created", f"Новий сервер {srv.id} у {provider}", "ok",
                                    server=srv.id, provider=provider, attempt=attempt)
                    return srv.id
                except ProviderError as e:
                    errors.append(str(e))
                    self.events.add("provider.retry", f"{e} (спроба {attempt}/{MAX_TRIES})", "warn",
                                    provider=provider)
            self.events.add("provider.failover", f"{provider} недоступний — пробую наступного провайдера", "warn")
        raise ProviderError("усі провайдери недоступні: " + "; ".join(errors[-3:]))

    def ensure_server(self, avoid_provider: str | None = None, exclude: set[str] | None = None) -> str:
        return self.pick_server(exclude) or self.new_server(avoid_provider)

    # ---- pipeline ----------------------------------------------------------------------
    def _steps(self, job: Job):
        w = self.world

        def register():
            if job.domain in w.registered:
                return "вже зареєстровано"
            w.register_domain(job.domain)
            return "зареєстровано"

        def place():
            if job.server_id and w.server_ping(job.server_id):
                return f"вже на {job.server_id}"
            job.server_id = self.ensure_server()
            return f"сервер {job.server_id}"

        def dns():
            ip = w.servers[job.server_id].ip
            if w.dns.get(job.domain) == ip:
                return "DNS вже вказує куди треба"
            w.set_dns(job.domain, job.server_id)
            return f"A → {ip}"

        def deploy():
            site = w.sites.get(job.domain)
            if site and site.server_id == job.server_id and site.version == self.release:
                return "вже розгорнуто"
            w.deploy(job.domain, job.server_id, self.release, job.template)
            w.backup(job.domain)
            return f"шаблон {job.template}, реліз {self.release}"

        def tls():
            if job.domain in w.certs:
                return "сертифікат вже є"
            w.issue_cert(job.domain)
            return "Let's Encrypt OK"

        def smoke():
            r = w.probe(job.domain)
            if not r.ok:
                raise ProviderError(f"smoke-тест: {r.error}")
            return f"HTTP {r.code}, {r.ms} мс"

        return [("register", register), ("server", place), ("dns", dns), ("deploy", deploy),
                ("tls", tls), ("smoke", smoke)]

    def run(self, domain: str, template: str = "landing") -> Job:
        job = self.jobs.get(domain) or Job(domain, template)
        self.jobs[domain] = job
        if job.status == "done":
            return job
        job.status, job.error, job.steps = "running", "", []
        self.events.add("provision.start", f"Піднімаю {domain}", domain=domain)
        for name, fn in self._steps(job):
            for attempt in range(1, MAX_TRIES + 1):
                try:
                    detail = fn()
                    job.steps.append({"step": name, "ok": True, "detail": detail, "attempt": attempt})
                    break
                except ProviderError as e:
                    if attempt == MAX_TRIES:
                        job.steps.append({"step": name, "ok": False, "detail": str(e), "attempt": attempt})
                        job.status, job.error = "failed", f"{name}: {e}"
                        self.events.add("provision.failed", f"{domain}: крок «{name}» не вдався — {e}", "error",
                                        domain=domain)
                        self.notifier.send(f"⚠️ Не вдалося підняти {domain}: {e}. Повторний запуск продовжить "
                                           f"з того ж місця.")
                        return job
        job.status = "done"
        self.events.add("provision.done", f"{domain} працює на {job.server_id}", "ok", domain=domain)
        self.notifier.send(f"🟢 Сайт {job.domain} готовий: https://{job.domain}")
        return job
