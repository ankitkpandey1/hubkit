"""End of day on a clock: after report.eod_time the tick writes the eod once; a failure or a missing file alerts once.

Time-dependent cases call eodclock.run in this process with a fixed `now` (a day in March 2026, zone UTC) against the scratch
state of the `env` fixture; the CLI case runs the real tick and doctor."""

import os
import time

import pytest

from hubcore import config, eodclock, store

T0 = 1773100800.0  # 2026-03-10 00:00:00 UTC


@pytest.fixture
def cfg(env, monkeypatch):
    for k, v in (("HUBKIT_STATE", env.state), ("HUBKIT_CONFIG", env.cfg), ("HUBKIT_SESSIONS", env.sessions), ("XDG_CONFIG_HOME", env.root / "xdg")):
        monkeypatch.setenv(k, str(v))
    env.cfg.write_text(env.cfg.read_text() + '[report]\neod_time="18:30"\neod_check_min=30\n')
    return config.load()


def eod_file(cfg, day="2026-03-10"):
    return store.state(cfg) / "eod" / f"eod_{day}.md"


def test_nothing_runs_before_the_time_then_the_tick_writes_the_eod_once(cfg):
    assert eodclock.run(cfg, T0 + 18 * 3600 + 29 * 60) == [] and not eod_file(cfg).exists()
    assert eodclock.run(cfg, T0 + 18 * 3600 + 31 * 60) == [] and eod_file(cfg).exists()
    eod_file(cfg).unlink()
    assert eodclock.run(cfg, T0 + 18 * 3600 + 40 * 60) == [] and not eod_file(cfg).exists()  # run once for the day, not on every tick


def test_an_eod_already_written_by_someone_else_is_left_alone(cfg):
    eod_file(cfg).write_text("written by cron")
    assert eodclock.run(cfg, T0 + 20 * 3600) == [] and eod_file(cfg).read_text() == "written by cron"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write into a read-only directory")
def test_a_failing_eod_raises_one_alert_with_the_command(cfg):
    d = store.state(cfg) / "eod"
    d.chmod(0o500)  # a real write failure
    try:
        out = eodclock.run(cfg, T0 + 18 * 3600 + 35 * 60)  # 5 min after the time: well inside the check window
        assert len(out) == 1 and "failed" in out[0]["msg"] and "NEXT: hubkit eod --day 2026-03-10" in out[0]["msg"]
        assert eodclock.run(cfg, T0 + 20 * 3600) == []  # one alert, not one per tick
    finally:
        d.chmod(0o700)


def test_an_eod_file_that_is_missing_check_min_after_the_time_alerts_once(cfg):
    assert eodclock.run(cfg, T0 + 18 * 3600 + 31 * 60) == []  # written here
    eod_file(cfg).unlink()  # then it is gone
    assert eodclock.run(cfg, T0 + 18 * 3600 + 45 * 60) == []  # inside the check window
    out = eodclock.run(cfg, T0 + 19 * 3600 + 5 * 60)
    assert len(out) == 1 and "no eod file 30 min after 18:30" in out[0]["msg"] and "NEXT: hubkit eod --day 2026-03-10" in out[0]["msg"]
    assert eodclock.run(cfg, T0 + 19 * 3600 + 10 * 60) == []


def test_a_new_day_runs_again_and_a_bad_time_is_off(cfg):
    eodclock.run(cfg, T0 + 19 * 3600)
    eodclock.run(cfg, T0 + 86400 + 19 * 3600)
    assert eod_file(cfg, "2026-03-10").exists() and eod_file(cfg, "2026-03-11").exists()
    cfg["report"]["eod_time"] = "25:30"
    assert eodclock.parse(cfg) is None and eodclock.run(cfg, T0 + 3 * 86400 + 19 * 3600) == [] and not eod_file(cfg, "2026-03-13").exists()
    assert "INVALID" in eodclock.doctor_line(cfg)


def test_doctor_does_not_ask_for_a_cron_eod_entry_while_the_clock_is_on(env):
    from test_hubkit import fake_crontab

    path = fake_crontab(env, "* * * * * hubkit tick\n")  # a scheduler that runs the tick but not `hubkit eod`
    assert "runs `hubkit eod`" in env.run("doctor", **path).stdout
    env.cfg.write_text(env.cfg.read_text() + '[report]\neod_time="18:00"\n')
    assert "runs `hubkit eod`" not in env.run("doctor", **path).stdout


def test_the_real_tick_runs_it_and_doctor_says_whether_the_clock_is_on(env):
    assert "eod clock: off" in env.run("doctor").stdout
    env.run("tick", "--no-notify")
    assert not list((env.state / "eod").glob("eod_*.md"))  # off by default
    env.cfg.write_text(env.cfg.read_text() + '[report]\neod_time="00:00"\n')
    env.run("tick", "--no-notify")
    assert (env.state / "eod" / f"eod_{time.strftime('%Y-%m-%d', time.gmtime())}.md").exists()
    assert "eod clock: on at 00:00" in env.run("doctor").stdout
