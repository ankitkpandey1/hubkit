"""Estimates, overruns and the SLOW check: real state files in a scratch HUBKIT_STATE, no mocks."""

import json
import time

from test_hubkit import PR1, age_routed, routed_items, send, start_watch, stop


def brief(env, estimate=None, to="peer-a"):
    extra = f"\nEstimate: {estimate}m" if estimate else ""
    return send(env, to, f"Please re-check {PR1} at the new head.{extra}", "re-check 101")


def tick(env):
    r = env.run("tick", "--no-notify")
    assert r.returncode == 0, r.stderr
    return r.stdout


def peer_transcript(env, sid, entries):
    (env.transcripts / f"{sid}.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def test_a_brief_without_an_estimate_gets_the_usual_time_and_tells_the_hub(env):
    env.bind()
    r = brief(env)
    (item,) = routed_items(env)
    assert item["estimate_defaulted"] is True and item["estimate_min"] == 15  # no history: the stall limit
    assert "DEFAULTED" in r.stdout and "hubkit work estimate W1" in r.stdout
    assert "stall limit, 0 samples" in item["estimate_basis"]


def test_an_estimate_line_in_the_brief_is_kept_and_not_defaulted(env):
    env.bind()
    r = brief(env, estimate=7)
    (item,) = routed_items(env)
    assert item["estimate_min"] == 7 and item["estimate_defaulted"] is False and "DEFAULTED" not in r.stdout


def test_a_job_within_its_estimate_stays_quiet(env):
    env.bind()
    start_watch(env)
    brief(env, estimate=30)
    age_routed(env, 10)
    assert "SLOW" not in tick(env)
    assert stop(env) is None


def test_an_overrun_blocks_the_stop_and_raises_a_slow_line_with_why(env):
    env.bind()
    start_watch(env)
    brief(env, estimate=5)
    age_routed(env, 9)
    out = tick(env)
    assert "SLOW W1 work 9m, estimate 5m (estimate); holder peer-a; why: " in out and "NEXT:" in out
    reason = stop(env)["reason"]
    assert "OVERRUN: SLOW W1" in reason and "hubkit work estimate W1" in reason


def test_chasing_the_holder_addresses_an_overrun(env):
    env.bind()
    start_watch(env)
    brief(env, estimate=5)
    age_routed(env, 9)
    assert stop(env) is not None
    send(env, "peer-a", "Status on #101 please?")
    assert stop(env) is None


def test_a_new_estimate_with_a_reason_addresses_it_and_keeps_the_old_one(env):
    env.bind()
    start_watch(env)
    brief(env, estimate=5)
    age_routed(env, 9)
    low = env.run("work", "estimate", "W1", "--minutes", "8", "--why", "too low")
    assert low.returncode == 2 and "must exceed" in low.stderr  # 8 is under the 9 spent
    assert env.run("work", "estimate", "W1", "--minutes", "20", "--why", "needs a rebase first").returncode == 0
    (item,) = routed_items(env)
    assert [e["min"] for e in item["estimates"]] == [5.0, 20.0] and item["estimates"][1]["why"]
    assert stop(env) is None
    assert env.run("work", "estimate", "W1", "--minutes", "30", "--why", " ").returncode == 2


def test_a_rebrief_to_another_owner_addresses_the_old_item(env):
    env.bind()
    start_watch(env)
    brief(env, estimate=5)
    age_routed(env, 9)
    send(env, "peer-b", f"Take over {PR1}. Done-when: reviewed\nEstimate: 10m", "take over 101")
    assert not any("OVERRUN" in r for r in (stop(env) or {"reason": ""})["reason"].splitlines() if "W1" in r)


def test_why_names_the_running_tool_and_unread_messages_but_never_their_text(env):
    env.bind()
    start_watch(env)
    env.session(sid="sess-peer", name="peer-a")
    now = time.time()
    secret = "PRIVATE-CONTENT-MARKER"
    peer_transcript(env, "sess-peer", [
        {"type": "assistant", "timestamp": iso(now - 360), "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": secret}}]}},
        {"type": "queue-operation", "operation": "enqueue", "content": secret},
        {"type": "queue-operation", "operation": "enqueue", "content": secret},
    ])
    brief(env, estimate=5)
    age_routed(env, 9)
    out = tick(env)
    assert "why: Bash running 6m; 2 messages unread" in out and secret not in out
    assert secret not in env.state.joinpath("alerts.jsonl").read_text()


def test_a_cause_with_no_signal_says_unknown(env):
    env.bind()
    start_watch(env)
    env.session(sid="sess-peer", name="peer-a")
    brief(env, estimate=5)
    age_routed(env, 9)
    assert "why: unknown (session " in tick(env)


def test_the_same_cause_repeats_only_with_back_off(env):
    env.bind()
    start_watch(env)
    brief(env, estimate=5)
    age_routed(env, 9)
    assert "SLOW" in tick(env)
    assert "SLOW" not in tick(env)  # same cause, inside the back-off window


def test_an_item_from_before_estimates_alerts_against_its_usual_time_but_never_blocks_the_stop(env):
    env.bind()
    start_watch(env)
    brief(env, estimate=30)
    p = env.state / "work.json"
    items = json.loads(p.read_text())
    for i in items:
        for k in ("estimate_min", "estimates", "estimate_defaulted", "estimate_basis", "addressed"):
            i.pop(k)
    p.write_text(json.dumps(items))
    age_routed(env, 40)
    assert "SLOW W1 work 40m, usual 15m (limit, 0 samples)" in tick(env)
    assert "OVERRUN" not in (stop(env) or {"reason": ""})["reason"]


def test_the_status_block_shows_each_live_job_with_status_time_estimate_and_holder(env):
    env.bind()
    brief(env, estimate=30)
    tick(env)
    block = env.run("status").stdout
    assert "job W1 work 0m/30m holder peer-a" in block


def test_an_order_gets_an_estimate_at_its_first_move_and_the_eod_compares_it_with_the_actual(env):
    env.run("order", "open", "--kind", "merge", "--quote", "merge it")
    oid = env.rows("orders.jsonl")[0]["id"]
    r = env.run("order", "move", oid, "--note", "started")
    assert "estimate defaulted to 30m" in r.stdout
    env.run("order", "move", oid, "--note", "again")
    assert [x["op"] for x in env.rows("orders.jsonl")].count("estimate") == 1
    env.run("order", "cancel", oid, "--quote", "drop it")
    assert env.run("work", "estimate", oid, "--minutes", "40", "--why", "x").returncode == 2  # already archived
    eod = env.run("eod").stdout
    assert "estimate vs actual" in eod and "order:merge: 1 done, estimated 30m" in eod
