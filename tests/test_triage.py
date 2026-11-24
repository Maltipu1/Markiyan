import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sitepilot import triage as t
from sitepilot.fleet import Platform

SAMPLES = Path(__file__).resolve().parent.parent / "samples" / "emails"


def sample(name):
    return (SAMPLES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def no_ai(monkeypatch):
    monkeypatch.setenv("SITEPILOT_AI", "0")


def test_abuse_with_suspension_threat_is_critical():
    r = t.triage_rules(sample("01_abuse.txt"))
    assert (r.category, r.urgency) == ("abuse", "critical")
    assert r.domains == ["cobalt-market.test"]
    assert r.deadline == "within 24 hours"
    assert r.needs_alert


def test_invoice_and_maintenance_do_not_page_anyone():
    assert t.triage_rules(sample("02_invoice.txt")).category == "billing"
    m = t.triage_rules(sample("03_maintenance.txt"))
    assert (m.category, m.urgency) == ("maintenance", "low")
    assert not m.needs_alert


def test_injected_instructions_do_not_lower_urgency_and_are_flagged():
    r = t.triage_rules(sample("04_injection.txt"))
    assert r.urgency == "critical"
    assert len(r.red_flags) == 2
    assert "Нічого не надсилати" in r.action


def test_normal_mail_has_no_red_flags():
    assert t.triage_rules(sample("01_abuse.txt")).red_flags == []


def test_platform_alerts_only_on_urgent_mail():
    p = Platform()
    p.triage_email(sample("03_maintenance.txt"))
    assert p.notifier.sent == []
    p.triage_email(sample("01_abuse.txt"))
    assert len(p.notifier.sent) == 1 and "cobalt-market.test" in p.notifier.sent[0]["text"]


# ---- Claude path, with a fake client (no network in tests) ----------------------------

class FakeClient:
    def __init__(self, payload=None, stop="end_turn", fail=None):
        self.calls = []
        self.payload, self.stop, self.fail = payload, stop, fail
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise self.fail
        return SimpleNamespace(stop_reason=self.stop,
                               content=[SimpleNamespace(type="text", text=json.dumps(self.payload))])


def test_claude_answer_is_schema_constrained_and_email_is_wrapped():
    payload = {"category": "abuse", "urgency": "critical", "domains": ["cobalt-market.test"],
               "deadline": "24 години", "summary": "Скарга на фото", "action": "Прибрати фото",
               "red_flags": []}
    fake = FakeClient(payload)
    r = t.triage_claude(sample("01_abuse.txt"), client=fake)
    assert r.source == "claude" and r.category == "abuse"
    call = fake.calls[0]
    assert call["output_config"]["format"]["schema"] is t.SCHEMA
    assert call["messages"][0]["content"].startswith("<email>")
    assert "untrusted" in call["system"]


def test_refusal_is_not_silently_accepted():
    with pytest.raises(RuntimeError):
        t.triage_claude("text", client=FakeClient({}, stop="refusal"))


def test_api_failure_falls_back_to_rules(monkeypatch):
    monkeypatch.setenv("SITEPILOT_AI", "1")
    monkeypatch.setattr(t, "triage_claude", lambda text: (_ for _ in ()).throw(ConnectionError("down")))
    r = t.triage(sample("01_abuse.txt"))
    assert r.source == "rules" and "ConnectionError" in r.note
