"""Asks need a blocked order and stop after three tries; own work is verified against reality."""

import json

from test_hubkit import stop


def test_an_ask_needs_a_note_naming_what_it_blocks(env):
    r = env.run("work", "add", "--kind", "ask", "--title", "Which colour?")
    assert r.returncode == 2 and "an ask needs --note" in r.stderr
    assert env.run("work", "add", "--kind", "ask", "--title", "Which colour?", "--note", "blocks the logo order").returncode == 0


def test_the_third_unanswered_ask_of_the_same_question_stops_the_asking(env):
    env.bind()
    for _ in range(3):
        env.run("work", "add", "--kind", "ask", "--title", "Merge #101 now?", "--note", "blocks the release")
    rows = [i for i in json.loads((env.state / "work.json").read_text()) if i["kind"] == "ask"]
    assert len(rows) == 1 and rows[0]["asked"] == 3
    assert "asked 3 times with no answer" in stop(env, last_assistant_message="Working on it.")["reason"]


def test_two_asks_are_not_a_stop(env):
    env.bind()
    for _ in range(2):
        env.run("work", "add", "--kind", "ask", "--title", "Merge #101 now?", "--note", "blocks the release")
    assert stop(env, last_assistant_message="Working on it.") is None


def test_own_work_not_checked_against_reality_for_hours_alerts_until_verified(env):
    env.run("work", "add", "--kind", "active", "--title", "ship the docs", "--estimate", "600")
    p = env.state / "work.json"
    rows = json.loads(p.read_text())
    rows[0]["added"] -= 181 * 60
    p.write_text(json.dumps(rows))
    assert "last checked against reality" in env.run("tick", "--no-notify").stdout
    assert env.run("work", "verify", "W1").returncode == 0
    a = env.state / "alerts.jsonl"
    a.write_text("".join(json.dumps({**json.loads(ln), "ts": json.loads(ln)["ts"] - 3600}) + "\n" for ln in a.read_text().splitlines()))  # the back-off has passed
    assert "last checked against reality" not in env.run("tick", "--no-notify").stdout


def test_five_clean_accepts_earn_auto_and_a_reject_is_shown(env):
    env.bind()
    for _ in range(4):
        env.run("trust", "record", "--class", "tidy-docs", "--outcome", "accept")
    assert env.run("trust", "ls").stdout.strip() == "tidy-docs: ask"
    assert "tidy-docs: accept recorded -> auto" in env.run("trust", "record", "--class", "tidy-docs", "--outcome", "accept").stdout
    env.run("trust", "record", "--class", "send-mail", "--outcome", "reject")
    env.run("tick", "--no-notify")
    out = env.hook("session", {"session_id": "sess-hub", "source": "compact"}).stdout
    assert "auto (do it, then tell him): tidy-docs" in out and "he has rejected: send-mail" in out
    env.run("trust", "record", "--class", "tidy-docs", "--outcome", "edit")
    assert "tidy-docs: ask" in env.run("trust", "ls").stdout
    assert env.run("trust", "record", "--class", "x", "--outcome", "maybe").returncode == 2


def test_a_merge_order_closes_only_on_the_merge_commit_sha(env):
    env.bind()
    env.prompt("merge https://github.com/acme/repo/pull/101 now")
    oid = env.run("order", "ls").stdout.split()[0]
    r = env.run("order", "close", oid, "--evidence", "merged=PR 101 merged")
    assert r.returncode == 2 and "merge commit sha" in r.stderr
    assert env.run("order", "close", oid, "--evidence", "merged=PR 101 merged as 1a2b3c4d").returncode == 0


def test_a_chase_with_no_order_is_counted_in_the_day_metrics(env):
    env.bind()
    env.prompt("any progress?")
    env.prompt("what is the hold up?")
    assert json.loads(env.run("order", "metrics").stdout)["unordered_chases"] == 2


def test_digest_lists_what_finished_and_what_waits_on_the_user(env):
    env.bind()
    env.run("work", "add", "--kind", "active", "--title", "Fix the importer")
    wid = env.run("work", "ls").stdout.split()[0]
    env.run("work", "done", wid, "--note", "merged as 1a2b3c4")
    env.run("work", "add", "--kind", "ask", "--title", "Merge #101 now?", "--note", "blocks the release")
    out = env.run("digest", "--since", "1h").stdout
    assert "finished" in out and "Fix the importer" in out and "merged as 1a2b3c4" in out
    assert "waits on you" in out and "Merge #101 now?" in out
