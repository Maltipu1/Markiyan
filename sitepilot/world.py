"""Simulated infrastructure: hosting providers, servers, DNS, sites and fault injection.

Everything the platform does goes through this object, the same way a real system would
call provider APIs, a DNS registrar and HTTP. Swapping it for real adapters is the only
change needed to point SitePilot at live infrastructure.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from .core import Clock

PROVIDERS = ("alpha-cloud", "beta-hosting", "gamma-vps")


class ProviderError(Exception):
    """A transient error from a provider API (rate limit, 5xx, timeout)."""


@dataclass
class Server:
    id: str
    provider: str
    ip: str
    capacity: int = 3
    healthy: bool = True
    draining: bool = False
    sites: set[str] = field(default_factory=set)

    @property
    def free(self) -> int:
        return self.capacity - len(self.sites)

    def to_dict(self) -> dict:
        return {"id": self.id, "provider": self.provider, "ip": self.ip, "capacity": self.capacity,
                "healthy": self.healthy, "draining": self.draining, "sites": sorted(self.sites)}


@dataclass
class Site:
    domain: str
    server_id: str | None = None
    version: str | None = None
    template: str = "landing"

    def to_dict(self) -> dict:
        return {"domain": self.domain, "server": self.server_id, "version": self.version,
                "template": self.template}


@dataclass
class Probe:
    ok: bool
    code: int
    ms: int
    error: str = ""


class World:
    def __init__(self, clock: Clock):
        self.clock = clock
        self.servers: dict[str, Server] = {}
        self.sites: dict[str, Site] = {}
        self.registered: set[str] = set()
        self.dns: dict[str, str] = {}             # domain -> server ip
        self.certs: set[str] = set()
        self.backups: dict[str, str] = {}         # domain -> last backed-up version
        self.releases: dict[str, bool] = {"1.0.0": True}   # version -> does it actually work
        # fault injection
        self.down_sites: set[str] = set()
        self.flaky: dict[str, int] = {}           # domain -> how many next probes fail
        self.glitch_until = -1.0                  # monitor's own network is broken until then
        self.provider_outage: dict[str, int] = {} # provider -> how many next create calls fail
        self._ids = itertools.count(1)

    # ---- provider API -------------------------------------------------------------
    def create_server(self, provider: str, capacity: int = 3) -> Server:
        if self.provider_outage.get(provider, 0) > 0:
            self.provider_outage[provider] -= 1
            raise ProviderError(f"{provider}: 503 Service Unavailable")
        n = next(self._ids)
        srv = Server(f"srv-{n:02d}", provider, f"10.{PROVIDERS.index(provider)}.0.{n}", capacity)
        self.servers[srv.id] = srv
        return srv

    def server_ping(self, server_id: str) -> bool:
        srv = self.servers.get(server_id)
        return bool(srv and srv.healthy)

    # ---- registrar / DNS / TLS ----------------------------------------------------
    def register_domain(self, domain: str) -> None:
        self.registered.add(domain)

    def set_dns(self, domain: str, server_id: str) -> None:
        if domain not in self.registered:
            raise ValueError(f"{domain} is not registered")
        self.dns[domain] = self.servers[server_id].ip

    def issue_cert(self, domain: str) -> None:
        if domain not in self.dns:
            raise ProviderError(f"ACME: {domain} does not resolve yet")
        self.certs.add(domain)

    # ---- hosting ------------------------------------------------------------------
    def deploy(self, domain: str, server_id: str, version: str, template: str = "landing") -> None:
        if version not in self.releases:
            raise ValueError(f"unknown release {version}")
        srv = self.servers[server_id]
        site = self.sites.get(domain) or Site(domain)
        if site.server_id and site.server_id != server_id:
            self.servers[site.server_id].sites.discard(domain)
        site.server_id, site.version, site.template = server_id, version, template
        srv.sites.add(domain)
        self.sites[domain] = site

    def backup(self, domain: str) -> None:
        site = self.sites[domain]
        self.backups[domain] = site.version

    def restore(self, domain: str, server_id: str) -> str:
        """Bring a site up on another server from its last backup (source may be dead)."""
        version = self.backups.get(domain) or self.sites[domain].version
        self.deploy(domain, server_id, version, self.sites[domain].template)
        return version

    # ---- HTTP ---------------------------------------------------------------------
    def probe(self, domain: str) -> Probe:
        if self.clock.now() < self.glitch_until:
            return Probe(False, 0, 15000, "timeout (15s)")
        if self.flaky.get(domain, 0) > 0:
            self.flaky[domain] -= 1
            return Probe(False, 0, 15000, "timeout (15s)")
        site = self.sites.get(domain)
        if not site or domain not in self.dns:
            return Probe(False, 0, 0, "DNS: NXDOMAIN")
        srv = self.servers.get(site.server_id or "")
        if not srv or self.dns[domain] != srv.ip:
            return Probe(False, 0, 15000, "timeout: DNS points to a server without this site")
        if not srv.healthy:
            return Probe(False, 0, 15000, "connection refused")
        if domain in self.down_sites:
            return Probe(False, 500, 180, "HTTP 500")
        if not self.releases.get(site.version or "", False):
            return Probe(False, 500, 210, f"HTTP 500 after release {site.version}")
        if domain not in self.certs:
            return Probe(False, 0, 40, "TLS: certificate missing")
        return Probe(True, 200, 120 + sum(map(ord, domain)) % 80)

    def snapshot(self) -> dict:
        return {"servers": [s.to_dict() for s in self.servers.values()],
                "sites": [s.to_dict() for s in self.sites.values()],
                "releases": self.releases}
