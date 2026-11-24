from fastapi.testclient import TestClient

from sitepilot import web

client = TestClient(web.app)


def test_dashboard_and_state():
    assert client.get("/").status_code == 200
    s = client.get("/api/state").json()
    assert len(s["sites"]) == 7 and s["servers"]


def test_actions_are_validated():
    assert client.post("/api/provision", json={"domain": "../etc/passwd"}).status_code == 422
    assert client.post("/api/rollout", json={"version": "latest"}).status_code == 422
    assert client.post("/api/chaos/site/nope.test").status_code == 404


def test_provision_and_rollout_roundtrip():
    client.post("/api/reset")
    j = client.post("/api/provision", json={"domain": "web-check.test"}).json()
    assert j["status"] == "done"
    r = client.post("/api/rollout", json={"version": "3.0.0", "healthy": False}).json()
    assert r["status"] == "rolled_back"
