"""Requests (the user's plain asks, tracked to done) and the `mine` view. Everything runs as real processes on scratch state."""

import json
import time

from conftest import SCRIPTS  # noqa: F401  (puts the scripts on sys.path)

ASK = "delete the stale branches"
STOP = {"hook_event_name": "Stop", "session_id": "sess-hub"}


def work(env):
    p = env.state / "work.json"
    return json.loads(p.read_text()) if p.exists() else []


def reqs(env):
    return [i for i in work(env) if i["kind"] == "request"]


def age(env, minutes):
    """Move every request's opened time (and notes) into the past: a real edit of work.json."""
    items = work(env)
    for i in items:
        if i["kind"] == "request":
            i["added"] -= minutes * 60
    (env.state / "work.json").write_text(json.dumps(items))


def archive_done(env, minutes_each, n):
    """n finished requests in the archive, each taking minutes_each (rows shaped like the ones `req done` writes)."""
    arch = env.state / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    now = time.time()
    rows = [{"type": "work", "kind": "request", "id": f"W{90 + k}", "title": "x", "added": now - 3600 - minutes_each * 60, "ts": now - 3600, "outcome": "done"} for k in range(n)]
    (arch / "archive_2000-01-01.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_plain_imperative_opens_one_request(env):
    env.bind()
    r = env.prompt(ASK)
    assert "tracking request" in r.stdout and "req done" in r.stdout
    [item] = reqs(env)
    assert item["quote"] == ASK and item["chases"] == 0 and item["kind"] == "request"
    ls = env.run("req", "ls")
    assert ls.returncode == 0 and "delete the stale branches" in ls.stdout


def test_question_pasted_text_and_peer_text_open_no_request(env):
    env.bind()
    env.prompt("should we delete the stale branches?")
    env.prompt("delete the stale branches, right?")
    env.prompt("<pasted_content>delete the stale branches now please</pasted_content>")
    env.prompt("<teammate-message>delete the stale branches</teammate-message>")
    env.prompt("thanks, that looks fine")
    assert reqs(env) == []


def test_a_prompt_that_opens_an_order_opens_no_request(env):
    env.bind()
    env.prompt("we need a hotfix for the billing bug now, finish it asap")
    assert reqs(env) == []


def test_the_same_ask_again_is_a_chase_not_a_second_request(env):
    env.bind()
    env.prompt(ASK)
    r = env.prompt("delete the stale branches please")
    [item] = reqs(env)
    assert item["chases"] == 1 and "chasing request" in r.stdout and ASK in r.stdout


def test_a_short_chase_counts_and_prints_the_requests_line(env):
    env.bind()
    env.prompt(ASK)
    env.prompt("also rename the release tag to v2")
    r = env.prompt("is the branch delete done?")
    first, second = sorted(reqs(env), key=lambda i: i["added"])
    assert first["chases"] == 1 and second["chases"] == 0
    assert first["id"] in r.stdout and "chased 1x" in r.stdout and "stale branches" in r.stdout


def test_a_chase_that_names_no_request_counts_nothing(env):
    env.bind()
    env.prompt(ASK)
    env.prompt("is the weather report done?")
    assert reqs(env)[0]["chases"] == 0


def test_done_needs_evidence_drop_needs_the_quote_note_records(env):
    env.bind()
    env.prompt(ASK)
    rid = reqs(env)[0]["id"]
    bad = env.run("req", "done", rid)
    assert bad.returncode == 2 and "--evidence" in bad.stderr and len(reqs(env)) == 1
    assert env.run("req", "drop", rid).returncode == 2
    short = env.run("req", "done", rid, "--evidence", "ok")  # given but too thin to be evidence: refused by the logic, not by argparse
    assert short.returncode == 2 and "evidence" in short.stderr and len(reqs(env)) == 1
    blank = env.run("req", "drop", rid, "--quote", "   ")
    assert blank.returncode == 2 and "req drop needs --quote" in blank.stderr and len(reqs(env)) == 1
    assert env.run("req", "note", rid, "--note", "listed 12 branches").returncode == 0
    assert reqs(env)[0]["notes"][0]["text"] == "listed 12 branches"
    ok = env.run("req", "done", rid, "--evidence", "git branch -r shows 0 stale branches, run 14:02")
    assert ok.returncode == 0 and "done" in ok.stdout and reqs(env) == []
    arch = [json.loads(x) for f in (env.state / "archive").glob("archive_*.jsonl") for x in f.read_text().splitlines()]
    assert arch[0]["outcome"] == "done" and "0 stale branches" in arch[0]["done_note"]
    assert env.run("req", "done", rid, "--evidence", "again and again").returncode == 4


def test_drop_with_the_users_words_archives_it(env):
    env.bind()
    env.prompt(ASK)
    rid = reqs(env)[0]["id"]
    assert env.run("req", "drop", rid, "--quote", "forget the branches").returncode == 0
    assert reqs(env) == []


def test_status_block_lists_requests_oldest_first_with_age_and_chases(env):
    env.bind()
    env.prompt(ASK)
    age(env, 125)
    env.prompt("rename the release tag to v2")
    out = env.run("status").stdout
    lines = [x for x in out.splitlines() if "stale branches" in x or "release tag" in x]
    assert "open requests: 2" in out and len(lines) == 2
    assert "stale branches" in lines[0] and "2h" in lines[0] and "chased 0x" in lines[0]


def test_status_block_caps_the_request_lines(env):
    env.bind()
    for w in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"):
        env.prompt(f"delete the {w} folder")
    out = env.run("status").stdout
    assert "open requests: 7" in out and "... 2 more" in out
    assert sum("folder" in x for x in out.splitlines()) == 5


def test_done_claim_with_an_old_unnoted_request_blocks_the_stop(env):
    env.bind()
    env.prompt(ASK)
    quiet = env.hook("stop", {**STOP, "last_assistant_message": "All done."})
    assert "decision" not in quiet.stdout or "request(s) of the user" not in quiet.stdout
    age(env, 90)
    r = env.hook("stop", {**STOP, "last_assistant_message": "All done."})
    assert "request(s) of the user" in json.loads(r.stdout)["reason"] and "req done" in r.stdout
    other = env.hook("stop", {**STOP, "last_assistant_message": "Routed it."})
    assert "request(s) of the user" not in other.stdout
    rid = reqs(env)[0]["id"]
    env.run("req", "note", rid, "--note", "12 branches listed, deleting")
    after = env.hook("stop", {**STOP, "last_assistant_message": "All done."})
    assert "request(s) of the user" not in after.stdout


def test_overdue_request_alerts_once_with_backoff(env):
    env.bind()
    env.prompt(ASK)
    assert "request" not in env.run("tick", "--no-notify").stdout
    age(env, 130)
    first = env.run("tick", "--no-notify").stdout
    assert "request" in first and "past 120 min" in first
    assert "request" not in env.run("tick", "--no-notify").stdout


def test_overdue_limit_learns_from_finished_requests(env):
    env.bind()
    archive_done(env, 20, 3)  # three finished requests took 20 min: overdue after 2 x 20 = 40 min, not the configured 120
    env.prompt(ASK)
    age(env, 50)
    out = env.run("tick", "--no-notify").stdout
    assert "past 40 min" in out


def test_too_little_history_keeps_the_configured_limit(env):
    env.bind()
    archive_done(env, 20, 2)
    env.prompt(ASK)
    age(env, 50)
    assert "request" not in env.run("tick", "--no-notify").stdout


def test_eod_counts_requests_and_their_chases(env):
    env.bind()
    env.prompt(ASK)
    env.prompt("is the branch delete done?")
    env.prompt("is the branch delete done?")
    out = env.run("eod").stdout
    assert "requests opened 1, done 0, still open 1; chases 2 (2.0 per request)" in out


def test_mine_groups_everything_open_by_whose_court(env):
    env.bind()
    env.prompt(ASK)
    env.run("work", "add", "--kind", "ask", "--title", "which region?", "--note", "blocks the deploy")
    env.run("work", "add", "--kind", "routed", "--owner", "peer-a", "--title", "fix the flaky test", "--pr", "https://github.com/o/r/pull/12")
    env.run("work", "add", "--kind", "reply", "--title", "can you look at this", "--owner", "someone")
    env.run("work", "add", "--kind", "active", "--title", "write the summary")
    out = env.run("mine").stdout
    heads = [x for x in out.splitlines() if x[0] != " "]
    assert heads == ["ON THE USER (1)", "ON THE HUB (2)", "ON PEERS AND AGENTS (1)", "ON OTHER PEOPLE (1)"]
    user, hub, peers, others = out.split("ON ")[1:]
    assert "which region?" in user and "stale branches" in hub and "write the summary" in hub
    assert "peer-a" in peers and "pull/12" in peers and "someone asked" in others


def test_mine_is_read_only(env):
    env.bind()
    env.prompt(ASK)
    before = (env.state / "work.json").read_text()
    env.run("mine")
    assert (env.state / "work.json").read_text() == before


def test_digest_points_at_mine(env):
    env.bind()
    assert "hubkit mine" in env.run("digest").stdout
