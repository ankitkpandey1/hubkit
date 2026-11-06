"""A blocker on ordered work is solved, not reported: records, alerts, the Stop gate and the queue warning."""

import json

from test_hubkit import HOTFIX, LONG, PR1, send, stop

FINAL_BLOCKED = "Smoke is blocked: the host is down, so I am waiting on the agent."


def open_order(env):
    env.bind()
    env.prompt(HOTFIX)
    (oid,) = [r["id"] for r in env.rows("orders.jsonl") if r["op"] == "open"]
    env.run("order", "move", oid, "--note", "started")
    return oid


def order_line(env):
    return env.run("order", "ls").stdout


def test_a_blocker_without_a_fix_blocks_the_stop_and_names_it(env):
    oid = open_order(env)
    assert env.run("order", "block", oid, "--on", "smoke has no host").returncode == 0
    reason = stop(env)["reason"]
    assert "hubkit BLOCKER" in reason and "smoke has no host" in reason and "no fix running" in reason
    assert "BLOCKED on smoke has no host" in order_line(env) and "NO FIX RUNNING" in order_line(env)


def test_a_fix_running_clears_the_gate_until_it_outlives_its_limit(env):
    oid = open_order(env)
    env.run("order", "block", oid, "--on", "smoke has no host", "--fix", "provision a host", "--holder", "peer-b")
    assert "hubkit BLOCKER" not in (stop(env) or {}).get("reason", "")
    env.backdate(5)  # the fix has run 5 minutes with no result
    assert "fix has run 5 min with no result" in stop(env)["reason"]


def test_only_a_recorded_result_clears_a_blocker_and_notes_do_not_reset_idle(env):
    oid = open_order(env)
    env.run("order", "block", oid, "--on", "smoke has no host", "--fix", "provision a host")
    env.backdate(8)
    env.run("order", "move", oid, "--note", "still on it")
    line = order_line(env)
    assert "idle 8m" in line and "BLOCKED" in line  # the note did not restart the idle clock
    assert "fix has run 8 min" in stop(env)["reason"]
    assert env.run("order", "result", oid, "--note", "host up, smoke passed").returncode == 0
    assert "BLOCKED" not in order_line(env) and "idle 0m" in order_line(env)
    assert "hubkit BLOCKER" not in (stop(env) or {}).get("reason", "")


def test_the_tick_alerts_on_a_blocker_with_no_fix(env):
    oid = open_order(env)
    env.run("order", "block", oid, "--on", "smoke has no host")
    out = env.run("tick", "--no-notify").stdout
    assert f"ALERT {oid} is blocked on smoke has no host with no fix running" in out


def test_a_message_calling_an_open_order_blocked_is_held_until_a_fix_is_on_record(env):
    oid = open_order(env)
    reason = stop(env, last_assistant_message=FINAL_BLOCKED)["reason"]
    assert "says work is blocked" in reason and "Solve it, do not report it" in reason
    env.run("order", "block", oid, "--on", "smoke has no host", "--fix", "provision a host")
    assert "says work is blocked" not in (stop(env, last_assistant_message=FINAL_BLOCKED) or {}).get("reason", "")


def test_the_blocked_words_come_from_intents_toml(env):
    from conftest import INTENTS

    open_order(env)
    intents = env.root / "intents.toml"
    head, _, tail = INTENTS.read_text().partition("blocked_words = [")
    intents.write_text(head + 'blocked_words = ["wedged"]\n' + tail.partition("\n")[2])
    env.cfg.write_text(env.cfg.read_text().replace(f'intents="{INTENTS}"', f'intents="{intents}"'))
    assert "says work is blocked" in stop(env, last_assistant_message="The deploy is wedged.")["reason"]
    assert "says work is blocked" not in (stop(env, last_assistant_message=FINAL_BLOCKED) or {}).get("reason", "")


def test_a_blocked_claim_with_no_open_order_is_not_held(env):
    env.bind()
    assert stop(env, last_assistant_message=FINAL_BLOCKED) is None


def test_after_a_stop_loss_only_a_recorded_result_lets_the_turn_end(env):
    oid = open_order(env)
    for _ in range(3):
        env.backdate(2)
        out = env.prompt("what is the hold up?").stdout
    assert "STOP-LOSS" in out and f"hubkit order result {oid}" in out
    assert "A reply is not a result" in stop(env, last_assistant_message="Replying with the status line.")["reason"]
    env.run("order", "move", oid, "--note", "a note is not a result")
    assert "A reply is not a result" in stop(env)["reason"]
    env.run("order", "result", oid, "--note", "ran the blocking step, it passed")
    assert "A reply is not a result" not in (stop(env) or {}).get("reason", "")


def test_a_second_job_to_a_busy_owner_while_an_order_is_open_warns_of_the_queue(env):
    open_order(env)
    first = send(env, "peer-a", f"check {PR1}. Done-when: reviewed. " + LONG, "first")
    assert "queues behind" not in first.stdout
    second = send(env, "peer-a", "Start the smoke on https://github.com/acme/repo/pull/102. Done-when: smoke passes. " + LONG, "second")
    assert "WARNING: peer-a already holds W1; W2 queues behind it" in second.stdout
    SECOND = "Start the smoke on https://github.com/acme/repo/pull/102. Done-when: smoke passes. " + LONG
    again = send(env, "peer-a", SECOND, "second")
    assert "queues behind" not in again.stdout  # the same job again is a refresh, not a new queue entry


def test_a_second_job_with_no_open_order_is_not_warned(env):
    env.bind()
    send(env, "peer-a", f"check {PR1}. Done-when: reviewed. " + LONG, "first")
    second = send(env, "peer-a", "Start the smoke on https://github.com/acme/repo/pull/102. Done-when: smoke passes. " + LONG, "second")
    assert "queues behind" not in second.stdout


def test_the_ledger_keeps_blocker_rows(env):
    oid = open_order(env)
    env.run("order", "block", oid, "--on", "no host", "--fix", "provision", "--holder", "peer-b")
    env.run("order", "result", oid, "--note", "provisioned and passed")
    ops = [r["op"] for r in env.rows("orders.jsonl")]
    assert ops.count("block") == 1 and ops.count("result") == 1
    assert json.dumps(env.rows("orders.jsonl")).count("peer-b") == 1


def test_negated_wording_is_not_a_blocked_claim_but_a_true_one_still_is(env):
    open_order(env)
    for ok in ("Nothing is stuck right now.", "No blocker; the run is not blocked.", "No runs failed, nothing is down."):
        assert "says work is blocked" not in (stop(env, last_assistant_message=ok) or {}).get("reason", ""), ok
    assert "says work is blocked" in stop(env, last_assistant_message="Smoke is blocked on a host.")["reason"]
    assert "says work is blocked" in stop(env, last_assistant_message="Nothing ran. The deploy is stuck.")["reason"]


def test_a_wait_the_user_ordered_is_not_a_blocker_to_solve(env):
    open_order(env)
    for ok in ("Still waiting for your word after you've checked user activity.", "Paused as you told me to hold; nothing is running.",
               "The release is on hold, awaiting your go."):
        assert "says work is blocked" not in (stop(env, last_assistant_message=ok) or {}).get("reason", ""), ok
    assert "says work is blocked" in stop(env, last_assistant_message="Still waiting for the smoke host.")["reason"]


def test_a_chase_with_no_order_prints_the_job_lines(env):
    from test_hubkit import send  # noqa: PLC0415

    env.bind()
    send(env, "peer-a", "check https://github.com/acme/repo/pull/101")  # a routed job, no order
    out = env.prompt("any progress?").stdout
    assert "no order is live" in out and "job W1" in out
    assert "no order is live" not in env.prompt("tell me about the design of the cache layer in detail please, thanks").stdout
