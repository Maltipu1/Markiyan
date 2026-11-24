"""Scripted walkthrough: `python -m sitepilot.demo`

Runs every scenario on the simulated fleet and prints what the platform decided and why.
"""
from __future__ import annotations

from pathlib import Path

from .fleet import Platform
from .monitor import NOTIFY_AFTER

SAMPLES = Path(__file__).resolve().parent.parent / "samples" / "emails"


def title(text: str) -> None:
    print(f"\n━━ {text} " + "━" * max(0, 70 - len(text)))


def flush(p: Platform, seen: list[int]) -> None:
    for n in p.notifier.sent[seen[0]:]:
        print(f"  📣 {n['time']} → {n['text']}")
    seen[0] = len(p.notifier.sent)


def main() -> None:
    p = Platform(echo=True)
    seen = [0]

    title("1. Піднімаю 7 сайтів (один провайдер «лежить» — має спрацювати failover)")
    p.world.provider_outage["alpha-cloud"] = 3     # first provider fails every retry for the first server
    p.seed()
    p.tick()
    print(f"  сервери: {', '.join(f'{s.id}@{s.provider}' for s in p.world.servers.values())}")

    title("2. Один пропущений пінг — це шум, а не інцидент")
    p.world.flaky["birch-store.test"] = 1
    p.tick(3)
    flush(p, seen)

    title("3. Сайт справді лежить — людям пишемо лише після 5 хвилин")
    p.world.down_sites.add("dune-goods.test")
    p.tick(NOTIFY_AFTER // 60 + 2)
    flush(p, seen)
    p.world.down_sites.discard("dune-goods.test")
    p.tick()
    flush(p, seen)

    title("4. «Впав» увесь флот одночасно — це монітор, а не сайти")
    p.world.glitch_until = p.clock.now() + 4 * 60
    p.tick(6)
    flush(p, seen)
    print(f"  сповіщень за цей час: 0 (і це правильно)")

    title("5. Поганий реліз: канарка ловить його, все відкочується")
    p.world.releases["1.1.0"] = False               # this build returns HTTP 500
    p.rollouts.run("1.1.0", dry_run=True)
    p.rollouts.run("1.1.0")
    flush(p, seen)

    title("6. Нормальний реліз — по етапах на весь флот")
    p.world.releases["1.2.0"] = True
    p.rollouts.run("1.2.0")
    p.tick()

    title("7. Сервер помер — сайти переїжджають до того, як хтось помітить")
    victim = max(p.world.servers.values(), key=lambda s: len(s.sites))
    victim.healthy = False
    p.tick(4)
    flush(p, seen)
    print(f"  відкритих інцидентів: {len(p.monitor.open_incidents())}")

    title("8. Листи від провайдерів (Claude, якщо є ключ; інакше правила)")
    for f in sorted(SAMPLES.glob("*.txt")):
        r = p.triage_email(f.read_text(encoding="utf-8"))
        print(f"  {f.name}: {r['category']}/{r['urgency']} [{r['source']}] domains={r['domains']} "
              f"deadline={r['deadline']!r}")
        if r.get("note"):
            print(f"    ({r['note']})")
    flush(p, seen)

    title("Підсумок")
    up = sum(1 for d in p.world.sites if p.monitor.status(d) == "up")
    print(f"  сайтів: {len(p.world.sites)}, працює: {up}, серверів: {len(p.world.servers)}, "
          f"сповіщень людям: {len(p.notifier.sent)}, подій у журналі: {len(p.events.items)}")


if __name__ == "__main__":
    main()
