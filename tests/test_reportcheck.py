"""A final message with a UTC time while the user has another zone is held; unordered tickets and schedules are warned."""

import json

import pytest
from test_hubkit import stop


def zone(env, tz="Europe/Berlin", extra=""):
    env.cfg.write_text(env.cfg.read_text().replace('timezone="UTC"', f'timezone="{tz}"') + extra)


def last(env, text):
    return stop(env, last_assistant_message=text)


@pytest.mark.parametrize("said", ["The deploy landed at 14:32 UTC.", "Started 2026-11-05T17:33Z and ran long.", "Stamp 09:10:11 +00:00 in the log.", "It broke at 8:05 GMT."])
def test_a_utc_time_is_held_when_the_user_has_another_zone(env, said):
    zone(env)
    env.bind()
    out = last(env, said)
    assert out and "hubkit UTC-TIME" in out["reason"] and "NEXT: give the time in Europe/Berlin" in out["reason"]
    assert last(env, said) and stop(env, last_assistant_message=said, stop_hook_active=True) is None  # held once per stop


def test_utc_in_code_or_for_a_utc_user_or_with_the_switch_off_is_not_held(env):
    zone(env)
    env.bind()
    assert last(env, "Logs say `14:32 UTC` and\n```\n2026-11-05T17:33Z\n```\nso it was 15:32 Berlin time.") is None
    assert last(env, "The UTC clock is fine; no time stated.") is None
    zone(env, extra="[report]\nzone_check=false\n")
    assert last(env, "It was 14:32 UTC.") is None


def test_a_utc_user_is_never_held(env):
    env.bind()
    assert last(env, "It was 14:32 UTC.") is None


def ptool(env, tool, tool_input):
    r = env.hook("pretool", {"hook_event_name": "PreToolUse", "session_id": "sess-hub", "tool_name": tool, "tool_input": tool_input})
    assert r.returncode == 0 and "permissionDecision" not in r.stdout
    return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] if r.stdout.strip() else ""


TICKET = '[guards]\nneeds_order=["jira\\\\s+create", "gh\\\\s+issue\\\\s+create"]\n'


def test_a_ticket_command_with_no_typed_ask_is_warned_and_an_ask_clears_it(env):
    zone(env, "UTC", TICKET)
    env.bind()
    assert "hubkit WARNING NEEDS-ORDER" in ptool(env, "Bash", {"command": "gh issue create --title x"})
    assert ptool(env, "Bash", {"command": "gh issue list"}) == ""
    env.prompt("please file a ticket for the flaky test")
    assert ptool(env, "Bash", {"command": "gh issue create --title x"}) == ""


def test_recurring_work_with_no_typed_ask_is_warned_and_the_hubs_own_watch_is_exempt(env):
    env.bind()
    for tool, inp in (("CronCreate", {"schedule": "0 * * * *"}), ("ScheduleWakeup", {"delaySeconds": 3600}), ("Bash", {"command": "crontab -l | crontab -"})):
        assert "hubkit WARNING UNORDERED-SCHEDULE" in ptool(env, tool, inp), tool
    assert ptool(env, "Bash", {"command": "python3 hubkit watch --interval 120 (crontab)"}) == ""
    env.prompt("keep an eye on the release every hour")
    assert ptool(env, "CronCreate", {"schedule": "0 * * * *"}) == ""
