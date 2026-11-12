"""Request intake: question-form asks, multi-part asks, learning from false alarms, context kept. Real processes on scratch state."""

import json

import pytest
from conftest import SCRIPTS  # noqa: F401  (puts the scripts on sys.path)

STOP = {"hook_event_name": "Stop", "session_id": "sess-hub"}
MULTI = "do three things: fix the log, tidy the docs, and reply to the thread"
LIST = "please handle these\n1. delete the old branch\n2. rename the release tag\n3. update the changelog"


def reqs(env):
    p = env.state / "work.json"
    return [i for i in (json.loads(p.read_text()) if p.exists() else []) if i["kind"] == "request"]


def age(env, minutes):
    items = json.loads((env.state / "work.json").read_text())
    for i in items:
        if i["kind"] == "request":
            i["added"] -= minutes * 60
    (env.state / "work.json").write_text(json.dumps(items))


@pytest.mark.parametrize("ask", [
    "can you delete the old branch?",
    "could you remove the stale tags?",
    "would you rename the release tag to v2?",
    "hey, can you please push the hotfix branch?",
    "can u clean the temp folder?",
    "could you also update the changelog?",
    "pls restart the worker service?",
    "will you investigate the flaky login test?",
])
def test_a_question_form_ask_with_a_request_verb_opens_a_request(env, ask):
    env.bind()
    r = env.prompt(ask)
    assert "tracking request" in r.stdout
    [item] = reqs(env)
    assert item["quote"] == ask


@pytest.mark.parametrize("question", [
    "can you tell me why the build failed?",
    "could you explain the retry logic",
    "what is the status",
    "can you show me the diff?",
    "could you check if the build passed?",
    "would you describe the deploy flow?",
    "can you think about the naming?",
    "could you remind me what the plan was?",
    "can you confirm the release went out?",
    "how do I delete the old branch?",
])
def test_an_information_question_opens_nothing(env, question):
    env.bind()
    env.prompt(question)
    assert reqs(env) == []


def test_colon_list_becomes_parts_and_the_request_closes_with_its_last_part(env):
    env.bind()
    r = env.prompt(MULTI)
    assert "3 parts" in r.stdout
    [item] = reqs(env)
    assert [p["text"] for p in item["parts"]] == ["fix the log", "tidy the docs", "reply to the thread"]
    assert all(p["state"] == "open" and p["evidence"] == "" for p in item["parts"]) and item["parts"][2]["n"] == 3
    rid = item["id"]
    refused = env.run("req", "done", rid, "--evidence", "everything is done now")
    assert refused.returncode == 3 and "NEXT: hubkit req done" in refused.stderr and "tidy the docs" in refused.stderr
    assert len(reqs(env)) == 1
    ok = env.run("req", "done", rid, "--part", "1", "--evidence", "commit 1111111 fixes the log")
    assert ok.returncode == 0 and "1/3 parts done" in ok.stdout
    assert "part 1 done: commit 1111111" in reqs(env)[0]["notes"][0]["text"]  # a closed part counts as progress
    assert env.run("req", "drop", rid, "--part", "2", "--quote", "skip the docs").returncode == 0
    assert env.run("req", "drop", rid, "--part", "2", "--quote", "skip the docs").returncode == 3  # already closed
    assert env.run("req", "done", rid, "--part", "9", "--evidence", "no such part here").returncode == 4
    assert env.run("req", "drop", rid, "--part", "3").returncode == 2  # a drop needs the user's words
    last = env.run("req", "done", rid, "--part", "3", "--evidence", "reply posted at 14:02")
    assert last.returncode == 0 and "done, archived" in last.stdout and reqs(env) == []
    arch = [json.loads(x) for f in (env.state / "archive").glob("archive_*.jsonl") for x in f.read_text().splitlines()]
    assert arch[0]["outcome"] == "done" and "commit 1111111" in arch[0]["done_note"] and "reply posted" in arch[0]["done_note"]


def test_a_numbered_list_is_parts_and_all_dropped_parts_drop_the_request(env):
    env.bind()
    env.prompt(LIST)
    [item] = reqs(env)
    assert [p["n"] for p in item["parts"]] == [1, 2, 3]
    for n in (1, 2, 3):
        env.run("req", "drop", item["id"], "--part", str(n), "--quote", "forget it")
    assert reqs(env) == []
    arch = [json.loads(x) for f in (env.state / "archive").glob("archive_*.jsonl") for x in f.read_text().splitlines()]
    assert arch[0]["outcome"] == "dropped"


@pytest.mark.parametrize("text", [
    "fix the log: the stack trace is cut off",
    "do these: fix the log and the docs",
    "update things: fix the log, the docs and tests",
    "fix these:\n1. the log\n2. the docs",
    "fix the log\n- delete the cache\n",
    "please fix: fix a, fix b, fix c, fix d, fix e, fix f, fix g, fix h, fix i",
])
def test_unsure_detection_keeps_one_item_with_no_parts(env, text):
    env.bind()
    env.prompt(text)
    items = reqs(env)
    assert all("parts" not in i for i in items)


def test_parts_show_in_ls_json_status_gate_and_a_chase_names_the_part(env):
    env.bind()
    env.prompt(MULTI)
    rid = reqs(env)[0]["id"]
    age(env, 90)
    gate = env.hook("stop", {**STOP, "last_assistant_message": "All done."})
    assert "0/3 parts done (open: 1 fix the log" in json.loads(gate.stdout)["reason"]
    env.run("req", "done", rid, "--part", "1", "--evidence", "commit 2222222 fixes the log")
    ls = env.run("req", "ls").stdout
    assert "1/3 parts done" in ls and "2 tidy the docs" in ls and "3 reply to the thread" in ls and "1 fix the log" not in ls
    doc = json.loads(env.run("req", "ls", "--json").stdout)
    assert doc["requests"][0]["parts"][0]["state"] == "done" and len(doc["requests"][0]["parts"]) == 3
    assert "1/3 parts done" in env.run("status").stdout
    chase = env.prompt("did you reply to the thread yet?")
    assert "part 3" in chase.stdout and reqs(env)[0]["chases"] == 1


def test_the_request_keeps_the_full_text_and_notes_append_and_show_prints_them(env):
    env.bind()
    env.prompt(MULTI)
    rid = reqs(env)[0]["id"]
    env.run("req", "note", rid, "--note", "log fix started")
    env.run("req", "note", rid, "--note", "log fix needs a test")
    item = reqs(env)[0]
    assert item["text"] == MULTI and item["title"] == MULTI and [n["text"] for n in item["notes"]] == ["log fix started", "log fix needs a test"]
    assert all("t" in n for n in item["notes"])
    out = env.run("req", "show", rid).stdout
    assert f"text: {MULTI}" in out and "part 2 [open]: tidy the docs" in out and "note 2" in out and "log fix needs a test" in out and "chased 0x" in out
    doc = json.loads(env.run("req", "show", rid, "--json").stdout)["request"]
    assert doc["text"] == MULTI and doc["parts"][1]["text"] == "tidy the docs" and len(doc["notes"]) == 2
    assert env.run("req", "show", "W999").returncode == 4


def test_text_is_capped(env):
    env.bind()
    long = "delete the old branch " + "and its stale tags " * 14  # under the 300 character intake limit
    assert len(long) < 300
    env.prompt(long)
    assert 0 < len(reqs(env)[0]["text"]) <= 600


def drop_false(env, verbs):
    for v in verbs:
        env.prompt(f"{v}")
    for i in reqs(env):
        assert env.run("req", "drop", i["id"], "--not-a-request").returncode == 0


def test_three_false_alarms_of_one_verb_mute_it_and_relearn_restores_it(env):
    env.bind()
    drop_false(env, ["fix the alpha", "fix the bravo", "fix the charlie"])
    assert reqs(env) == []  # dropped as false: no quote needed
    arch = [json.loads(x) for f in (env.state / "archive").glob("archive_*.jsonl") for x in f.read_text().splitlines()]
    assert len(arch) == 3 and all(a["outcome"] == "dropped" and a["false_intake"] for a in arch)
    env.prompt("fix the delta")
    assert reqs(env) == []  # muted
    env.prompt("delete the echo branch")  # other verbs are untouched
    assert len(reqs(env)) == 1
    learned = env.run("req", "ls", "--learned")
    assert "muted: fix" in learned.stdout and "3 of 3" in learned.stdout and "req relearn fix" in learned.stdout
    assert json.loads(env.run("req", "ls", "--learned", "--json").stdout)["muted"]["fix"]["false"] == 3
    env.prompt("fix the config now")  # still opens through the imperative-word path
    assert len(reqs(env)) == 2
    assert env.run("req", "relearn", "fix").returncode == 0
    env.prompt("fix the foxtrot")
    assert len(reqs(env)) == 3
    assert "no verb muted" in env.run("req", "ls", "--learned").stdout
    assert env.run("req", "relearn", "fix").returncode == 4  # not muted any more


def test_two_false_alarms_or_a_low_ratio_do_not_mute(env):
    env.bind()
    drop_false(env, ["fix the alpha", "fix the bravo"])
    env.prompt("fix the charlie")
    assert len(reqs(env)) == 1
    for w in ("delta", "echo", "foxtrot", "golf"):  # opens that stay real: 3 false of 7 opens is under half
        env.prompt(f"fix the {w}")
    [env.run("req", "drop", i["id"], "--not-a-request") for i in reqs(env)[:1]]
    out = env.run("req", "ls", "--learned").stdout
    assert "no verb muted" in out


def test_req_add_opens_by_hand_and_the_miss_shows_in_eod(env):
    env.bind()
    r = env.run("req", "add", "ping the vendor about the invoice")
    assert r.returncode == 0 and "intake miss" in r.stdout
    [item] = reqs(env)
    assert item["text"] == "ping the vendor about the invoice"
    assert env.run("req", "add").returncode == 2
    assert "intake misses 1" in env.run("eod").stdout
    assert "misses logged (req add): 1" in env.run("req", "ls", "--learned").stdout
    assert env.run("req", "drop", item["id"], "--not-a-request").returncode == 0
    assert "false alarms 1" in env.run("eod").stdout


def test_false_alarms_older_than_the_window_do_not_count(env):
    env.bind()
    old = 20 * 86400  # past the 14 day window; a real edit of the learning log
    now = __import__("time").time()
    rows = [{"type": t, "key": "fix", "id": "W1", "ts": now - old} for t in ("open", "open", "open", "false", "false", "false")]
    (env.state / "intake-learn.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    env.prompt("fix the zulu")
    assert len(reqs(env)) == 1
    assert "no verb muted" in env.run("req", "ls", "--learned").stdout
