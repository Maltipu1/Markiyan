import pytest

from sitepilot.fleet import Platform
from sitepilot.monitor import NOTIFY_AFTER
from sitepilot.world import PROVIDERS


@pytest.fixture
def p():
    p = Platform()
    p.seed()
    p.tick()
    return p


def texts(p):
    return [n["text"] for n in p.notifier.sent]


# ---- provisioning ------------------------------------------------------------------

def test_seed_brings_every_site_up(p):
    assert len(p.world.sites) == 7
    assert all(p.monitor.status(d) == "up" for d in p.world.sites)
    assert all(s.free >= 0 for s in p.world.servers.values())


def test_provisioning_fails_over_to_next_provider():
    p = Platform()
    p.world.provider_outage[PROVIDERS[0]] = 99
    job = p.provisioner.run("solo.test")
    assert job.status == "done"
    assert p.world.servers[job.server_id].provider == PROVIDERS[1]
    assert p.events.of_kind("provider.failover")


def test_provisioning_is_resumable():
    p = Platform()
    for prov in PROVIDERS:
        p.world.provider_outage[prov] = 99
    job = p.provisioner.run("later.test")
    assert job.status == "failed"
    assert "later.test" in p.world.registered          # finished steps stay done
    p.world.provider_outage.clear()
    job = p.provisioner.run("later.test")
    assert job.status == "done"
    assert job.steps[0]["detail"] == "вже зареєстровано"


# ---- monitor -----------------------------------------------------------------------

def test_single_failed_check_is_not_an_incident(p):
    p.world.flaky["birch-store.test"] = 1
    p.tick(3)
    assert not p.monitor.open_incidents()
    assert p.notifier.sent == []
    assert p.events.of_kind("site.blip")


def test_people_are_told_only_after_five_minutes(p):
    p.world.down_sites.add("dune-goods.test")
    p.tick(2)
    assert p.monitor.open_incidents()                 # incident is open...
    assert p.notifier.sent == []                      # ...but nobody is paged yet
    p.tick(NOTIFY_AFTER // 60)
    assert len(p.notifier.sent) == 1 and "dune-goods.test" in p.notifier.sent[0]["text"]
    p.tick(5)
    assert len(p.notifier.sent) == 1                  # no repeats while it stays down
    p.world.down_sites.clear()
    p.tick()
    assert "знову працює" in p.notifier.sent[-1]["text"]
    assert not p.monitor.open_incidents()


def test_short_outage_produces_no_messages_at_all(p):
    p.world.down_sites.add("dune-goods.test")
    p.tick(3)
    p.world.down_sites.clear()
    p.tick()
    assert p.notifier.sent == []                      # no "recovered" for something nobody heard about


def test_mass_failure_is_treated_as_monitor_glitch(p):
    p.world.glitch_until = p.clock.now() + 10 * 60
    p.tick(12)
    assert p.notifier.sent == []
    assert not p.monitor.open_incidents()
    assert p.events.of_kind("monitor.glitch")
    assert p.events.of_kind("monitor.glitch_over")


# ---- self-healing ------------------------------------------------------------------

def test_dead_server_is_evacuated_before_anyone_is_paged(p):
    victim = max(p.world.servers.values(), key=lambda s: len(s.sites))
    moved = set(victim.sites)
    victim.healthy = False
    p.tick(NOTIFY_AFTER // 60 + 2)
    for d in moved:
        assert p.world.sites[d].server_id != victim.id
        assert p.monitor.status(d) == "up"
    assert not p.monitor.open_incidents()
    assert len(p.notifier.sent) == 1 and "впав" in p.notifier.sent[0]["text"]


def test_evacuation_restores_the_current_release_not_an_old_one(p):
    p.world.releases["1.2.0"] = True
    assert p.rollouts.run("1.2.0").status == "done"
    victim = next(iter(p.world.servers.values()))
    moved = set(victim.sites)
    victim.healthy = False
    p.tick(3)
    assert all(p.world.sites[d].version == "1.2.0" for d in moved)


def test_no_capacity_anywhere_means_a_human_is_asked(p):
    for prov in PROVIDERS:
        p.world.provider_outage[prov] = 99
    for s in p.world.servers.values():
        s.capacity = len(s.sites)                      # nowhere to move to
    victim = next(iter(p.world.servers.values()))
    victim.healthy = False
    p.tick(3)
    assert "потрібна людина" in p.notifier.sent[0]["text"]


def test_one_missed_ping_does_not_move_production(p):
    srv = next(iter(p.world.servers.values()))
    srv.healthy = False
    p.healer.tick()
    srv.healthy = True
    p.healer.tick()
    assert not srv.draining


# ---- rollouts ----------------------------------------------------------------------

def test_bad_release_is_stopped_at_canary_and_rolled_back(p):
    p.world.releases["9.9.9"] = False
    res = p.rollouts.run("9.9.9")
    assert res.status == "rolled_back"
    assert res.updated == []
    assert all(s.version == "1.0.0" for s in p.world.sites.values())


def test_failure_in_later_batch_rolls_back_everything_touched(p):
    p.world.releases["2.0.0"] = True
    plan = p.rollouts.plan("2.0.0")
    bad = plan[1][1]                     # second site of the first regular batch
    real_deploy = p.world.deploy

    def deploy(domain, server_id, version, template="landing"):
        real_deploy(domain, server_id, version, template)
        if domain == bad:                # this one site can't run the new release
            (p.world.down_sites.add if version == "2.0.0" else p.world.down_sites.discard)(domain)

    p.world.deploy = deploy
    res = p.rollouts.run("2.0.0")
    assert res.status == "rolled_back" and res.failed_on == bad
    assert len(res.updated) == 3         # two canaries + one site of the batch went fine
    assert all(s.version == "1.0.0" for s in p.world.sites.values())
    assert all(p.world.probe(d).ok for d in p.world.sites)


def test_good_release_goes_out_in_stages(p):
    p.world.releases["1.2.0"] = True
    res = p.rollouts.run("1.2.0")
    assert res.status == "done"
    assert len(res.stages[0]) == 2
    assert all(s.version == "1.2.0" for s in p.world.sites.values())


def test_dry_run_changes_nothing(p):
    p.world.releases["1.2.0"] = True
    res = p.rollouts.run("1.2.0", dry_run=True)
    assert res.status == "planned"
    assert all(s.version == "1.0.0" for s in p.world.sites.values())


def test_single_blip_does_not_block_a_release(p):
    p.world.releases["1.2.0"] = True
    p.world.flaky["ember-deals.test"] = 1
    assert p.rollouts.run("1.2.0").status == "done"


def test_rollout_refuses_to_touch_a_site_that_is_already_down(p):
    p.world.releases["1.2.0"] = True
    p.world.down_sites.add("ember-deals.test")
    res = p.rollouts.run("1.2.0")
    assert res.status == "rejected"
    assert all(s.version == "1.0.0" for s in p.world.sites.values())
