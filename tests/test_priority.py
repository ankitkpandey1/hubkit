"""What he says is urgent leads (class priority-not-applied): intake, lead lines, folded old lines, sleep gap, inbox ask."""

import json
import os
import time

import pytest


def work(env):
    p = env.state / "work.json"
    return json.loads(p.read_text()) if p.exists() else []


def lead(env):
    return [i for i in work(env) if i.get("kind") != "request" and i.get("lead")]


def stop(env, last="done", transcript=None):
    payload = {
        "hook_event_name": "Stop",
        "session_id": "sess-hub",
        "last_assistant_message": last,
    }
    if transcript:
        payload["transcript_path"] = str(transcript)
    return env.hook("stop", payload).stdout


def age_routed(env, minutes):
    """Shift the routed clocks into the past (a real file edit)."""
    rows = work(env)
    for r in rows:
        for k in ("added", "routed_at", "last_heard"):
            if r.get(k):
                r[k] -= minutes * 60
    (env.state / "work.json").write_text(json.dumps(rows))


def route_old_item(env, minutes=700):
    env.run(
        "work",
        "add",
        "--kind",
        "routed",
        "--owner",
        "peer-a",
        "--title",
        "check the billing fix and report back",
    )
    age_routed(env, minutes)


@pytest.mark.parametrize(
    "text",
    [
        "sync with all agaents and check msgs, some need urgent attention",
        "code red",
        "top priotity item",
        "this is top priority",
        "do it ASAP",
        "its urgnt",
        "Urgent!",
    ],
)
def test_urgent_words_and_typos_open_one_priority_item_quoting_him(env, text):
    env.bind()
    out = env.prompt(text).stdout
    items = lead(env)
    assert len(items) == 1 and items[0]["kind"] == "priority"
    assert text[:30] in items[0]["title"]
    assert out.splitlines()[0].startswith("hubkit PRIORITY W")


@pytest.mark.parametrize(
    "text",
    [
        "prioritize the docs cleanup",
        "what is the priority field in the schema",
        "fix the docs",
    ],
)
def test_everyday_words_open_nothing(env, text):
    env.bind()
    env.prompt(text)
    assert lead(env) == []


def test_prompts_within_ten_minutes_merge_into_one_item(env):
    env.bind()
    for text in (
        "sync with all agaents and check msgs, some need urgent attention",
        "is dana msg done?",
        "code red",
        "top priotity item",
    ):
        env.prompt(text)
    items = lead(env)
    assert len(items) == 1
    assert len(items[0]["quotes"]) == 4 and items[0]["prompts"] == 4
    assert "dana" in items[0]["title"] and "priotity" in items[0]["title"]


def test_a_new_item_opens_after_the_old_one_was_acted_on_and_ten_minutes_passed(env):
    env.bind()
    env.prompt("urgent: look at the thing")
    assert env.run("work", "act", "W1", "--note", "routed it").returncode == 0
    rows = work(env)
    rows[0]["last_prompt"] -= 11 * 60
    (env.state / "work.json").write_text(json.dumps(rows))
    env.prompt("urgent again")
    assert len(lead(env)) == 2


def test_it_is_not_an_order_and_authorises_no_act(env):
    env.bind()
    out = env.prompt("this is urgent, top priority please").stdout
    assert (
        "NOT an order" in out
        and "authorises no merge, release, deploy or message" in out
    )
    assert not [o for o in env.rows("orders.jsonl") if o.get("op") == "open"]


def test_prompt_leads_with_the_item_and_folds_old_lines_to_a_count(env):
    env.bind()
    route_old_item(env)
    plain = env.prompt("how is the weather").stdout
    assert (
        "hubkit STALLED" in plain
    )  # without an urgent prompt the old line prints as before
    out = env.prompt("top priotity item").stdout
    lines = out.splitlines()
    assert lines[0].startswith("hubkit PRIORITY W")
    assert "hubkit STALLED" not in out
    assert any(
        x.startswith(
            "hubkit: 1 older STALLED/OVERDUE line(s) held under the priority item"
        )
        for x in lines[1:]
    )


def test_the_old_lines_return_once_the_item_has_a_first_action(env):
    env.bind()
    route_old_item(env)
    env.prompt("urgent")
    assert (
        env.run(
            "work", "act", lead(env)[0]["id"], "--note", "read it, routed it"
        ).returncode
        == 0
    )
    out = env.prompt("how is the weather").stdout
    assert "PRIORITY" not in out and "hubkit STALLED" in out


def test_stop_hook_leads_with_the_item_until_a_first_action(env):
    env.bind()
    route_old_item(env)
    env.prompt("urgent")
    reason = json.loads(stop(env))["reason"]
    lines = reason.splitlines()
    assert lines[0].startswith("hubkit PRIORITY W")
    assert "hubkit STALLED" not in reason and "older STALLED/OVERDUE" in reason
    env.run("work", "act", lead(env)[0]["id"], "--note", "routed it")
    assert "PRIORITY" not in stop(env)


def test_the_watchdog_alerts_on_an_item_with_no_first_action(env):
    env.bind()
    env.prompt("urgent")
    rows = work(env)
    rows[0]["added"] -= 400
    (env.state / "work.json").write_text(json.dumps(rows))
    assert (
        "his urgent words have no first action" in env.run("tick", "--no-notify").stdout
    )


# ---- (3) an asleep gap does not count against stall clocks ----


def sleep_state(env, quiet_min):
    """Every heartbeat file last touched `quiet_min` ago, as after a night with the laptop off."""
    for f in (env.state / "seen").glob("*"):
        t = time.time() - quiet_min * 60
        os.utime(f, (t, t))


def test_a_gap_with_no_tick_is_not_counted_as_stall_time(env):
    env.bind()
    env.run("tick", "--no-notify")
    route_old_item(env, 700)
    sleep_state(env, 690)
    out = env.prompt("how is the weather").stdout
    assert "nothing for 700 min" not in out and "hubkit STALLED" not in out
    gaps = env.rows("gaps.jsonl")
    assert len(gaps) == 1 and gaps[0]["end"] - gaps[0]["start"] > 600 * 60


def test_stall_time_outside_the_gap_still_counts(env):
    env.bind()
    env.run("tick", "--no-notify")
    route_old_item(env, 700)
    sleep_state(
        env, 300
    )  # off for 300 min, but silent for 700: 400 awake minutes are still a stall
    out = env.prompt("how is the weather").stdout
    assert "hubkit STALLED" in out and "nothing for 4" in out


def test_a_short_quiet_spell_is_no_gap(env):
    env.bind()
    env.run("tick", "--no-notify")
    sleep_state(env, 20)
    env.prompt("how is the weather")
    assert env.rows("gaps.jsonl") == []


def test_orders_clock_skips_the_gap_too(env):
    env.bind()
    env.run("tick", "--no-notify")
    env.run("order", "open", "--kind", "merge", "--quote", "merge it")
    env.backdate(700)
    sleep_state(env, 699)
    out = env.prompt("how is the weather").stdout
    assert "OVERDUE" not in out and "idle 699m" not in out and "idle 1m" in out


# ---- (4) "check msgs" is an ask whose first action is an inbox read ----


def transcript_with(env, *calls, stamp=None):
    p = env.transcripts / "hub.jsonl"
    rows = [
        {
            "type": "assistant",
            "timestamp": stamp or "2999-01-01T00:00:00Z",
            "message": {
                "content": [
                    {"type": "tool_use", "id": f"t{n}", "name": name, "input": inp}
                ]
            },
        }
        for n, (name, inp) in enumerate(calls)
    ]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p


def test_check_msgs_opens_an_inbox_ask_and_a_plain_act_does_not_clear_it(env):
    env.bind()
    out = env.prompt("check msgs").stdout
    item = lead(env)[0]
    assert item["needs_inbox"] and "read his messages" in out
    r = env.run("work", "act", item["id"], "--note", "looked around")
    assert r.returncode == 2 and "inbox read" in r.stderr
    assert (
        env.run(
            "work",
            "act",
            item["id"],
            "--inbox-read",
            "--note",
            "dana asked for a merge; routed",
        ).returncode
        == 0
    )
    assert lead_open(env) == []


def lead_open(env):
    return [i for i in lead(env) if not i.get("first_action")]


@pytest.mark.parametrize(
    "text",
    [
        "read msg",
        "can you check the messages",
        "is dana msg done?",
        "check my inbox",
    ],
)
def test_inbox_ask_wordings(env, text):
    env.bind()
    env.prompt(text)
    assert lead(env) and lead(env)[0]["needs_inbox"]


def test_stop_holds_a_final_message_that_did_not_read_the_inbox(env):
    env.bind()
    env.prompt("check msgs")
    bare = transcript_with(env, ("Bash", {"command": "ls"}))
    assert "read his messages" in json.loads(stop(env, "all fine", bare))["reason"]
    read = transcript_with(
        env,
        (
            "Agent",
            {"description": "inbox", "prompt": "run mep watch --cursor hub and triage"},
        ),
    )
    assert stop(env, "all fine", read) == ""
    assert lead_open(env) == []


def test_an_inbox_read_before_the_ask_does_not_count(env):
    env.bind()
    env.prompt("check msgs")
    old = transcript_with(
        env, ("Bash", {"command": "mep inbox"}), stamp="2001-01-01T00:00:00Z"
    )
    assert "PRIORITY" in json.loads(stop(env, "all fine", old))["reason"]


def test_hooks_still_exit_zero_on_a_broken_work_file(env):
    env.bind()
    (env.state / "work.json").write_text("{not json")
    for r in (
        env.prompt("urgent"),
        env.hook(
            "stop",
            {
                "hook_event_name": "Stop",
                "session_id": "sess-hub",
                "last_assistant_message": "x",
            },
        ),
    ):
        assert r.returncode == 0


def test_the_overrun_check_skips_the_gap_too(env):
    env.bind()
    env.run("tick", "--no-notify")
    route_old_item(env, 700)
    sleep_state(env, 699)
    assert "OVERRUN" not in env.prompt("urgent").stdout + stop(env)


def test_a_read_through_a_chat_tool_named_in_config_is_an_inbox_read(env):
    env.cfg.write_text(env.cfg.read_text() + '\n[priority]\ninbox_tools = ["pigeon"]\n')
    env.bind()
    env.prompt("check msgs")
    other = transcript_with(env, ("mcp__carrier__search_messages", {}))
    assert "read his messages" in json.loads(stop(env, "all fine", other))["reason"]
    mail = transcript_with(env, ("mcp__pigeon__search_messages", {}))
    assert stop(env, "all fine", mail) == ""


def test_a_read_through_the_default_chat_tools_needs_no_config(env):
    env.bind()
    env.prompt("check msgs")
    mail = transcript_with(env, ("mcp__zulip__search_messages", {}))
    assert stop(env, "all fine", mail) == ""
