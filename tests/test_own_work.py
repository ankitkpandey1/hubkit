"""The hub's own work carries an estimate; a missing or overrun one is said out loud."""

import json


def age_work(env, minutes):
    p = env.state / "work.json"
    rows = json.loads(p.read_text())
    for r in rows:
        r["added"] -= minutes * 60
    p.write_text(json.dumps(rows))


def test_own_work_without_an_estimate_alerts_and_an_estimate_silences_it(env):
    env.run("work", "add", "--kind", "active", "--title", "tidy the docs")
    assert "no estimate" not in env.run("tick", "--no-notify").stdout
    age_work(env, 6)
    assert "own work with no estimate" in env.run("tick", "--no-notify").stdout
    assert env.run("work", "estimate", "W1", "--minutes", "20", "--why", "two files").returncode == 0
    assert "no estimate" not in env.run("tick", "--no-notify").stdout


def test_own_work_past_its_estimate_alerts(env):
    env.run("work", "add", "--kind", "active", "--title", "tidy the docs", "--estimate", "5")
    assert "spent" not in env.run("tick", "--no-notify").stdout
    age_work(env, 7)
    assert "7 min spent, estimate 5" in env.run("tick", "--no-notify").stdout
