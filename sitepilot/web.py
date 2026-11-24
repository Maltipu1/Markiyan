"""Live dashboard: `uvicorn sitepilot.web:app` and open http://127.0.0.1:8000

The simulation advances one minute per real second. Buttons on the page inject failures so you
can watch the platform react.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .fleet import Platform
from .triage import triage

STATIC = Path(__file__).parent / "static"
SAMPLES = Path(__file__).resolve().parent.parent / "samples" / "emails"

app = FastAPI(title="SitePilot")
lock = threading.Lock()
platform = Platform()
platform.seed()
running = {"on": True}


def _ticker() -> None:
    while True:
        time.sleep(1)
        if running["on"]:
            with lock:
                platform.tick()


threading.Thread(target=_ticker, daemon=True).start()


class ProvisionIn(BaseModel):
    domain: str = Field(pattern=r"^[a-z0-9-]{1,40}\.test$")


class RolloutIn(BaseModel):
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    healthy: bool = True
    dry_run: bool = False


class MailIn(BaseModel):
    text: str = Field(min_length=10, max_length=20000)


def _site(domain: str):
    if domain not in platform.world.sites:
        raise HTTPException(404, "немає такого сайту")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def state():
    with lock:
        return {**platform.state(), "running": running["on"]}


@app.post("/api/chaos/site/{domain}")
def break_site(domain: str):
    with lock:
        _site(domain)
        w = platform.world
        if domain in w.down_sites:
            w.down_sites.discard(domain)
            return {"down": False}
        w.down_sites.add(domain)
        return {"down": True}


@app.post("/api/chaos/blip/{domain}")
def blip(domain: str):
    with lock:
        _site(domain)
        platform.world.flaky[domain] = 1
    return {"ok": True}


@app.post("/api/chaos/server/{server_id}")
def kill_server(server_id: str):
    with lock:
        srv = platform.world.servers.get(server_id)
        if not srv:
            raise HTTPException(404, "немає такого сервера")
        srv.healthy = False
    return {"ok": True}


@app.post("/api/chaos/glitch")
def glitch():
    with lock:
        platform.world.glitch_until = platform.clock.now() + 4 * 60
    return {"ok": True}


@app.post("/api/provision")
def provision(body: ProvisionIn):
    with lock:
        return platform.provisioner.run(body.domain).to_dict()


@app.post("/api/rollout")
def rollout(body: RolloutIn):
    with lock:
        platform.world.releases.setdefault(body.version, body.healthy)
        return platform.rollouts.run(body.version, dry_run=body.dry_run).to_dict()


@app.get("/api/samples")
def samples():
    return [{"name": f.stem, "text": f.read_text(encoding="utf-8")} for f in sorted(SAMPLES.glob("*.txt"))]


@app.post("/api/triage")
def triage_mail(body: MailIn):
    result = triage(body.text)          # may call Claude — keep it outside the lock
    with lock:
        return platform.triage_email(body.text, result)


@app.post("/api/pause")
def pause():
    running["on"] = not running["on"]
    return {"running": running["on"]}


@app.post("/api/reset")
def reset():
    global platform
    with lock:
        platform = Platform()
        platform.seed()
    return {"ok": True}
