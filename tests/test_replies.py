"""A reply owed to a person closes the loop: GitHub says the PR is done, the person is told, the ask closes.

The only stand-in is `gh` (tests/test_hubkit.py `fake_gh`): the tests cannot reach GitHub. Everything else is real files and processes."""

import json
import time

from test_hubkit import PR1, fake_gh, gh_calls, review, send, stop

H = "a" * 40
H2 = "b" * 40


def setup(env, head=H, **gh):
    fake_gh(env, head=head)
    env.cfg.write_text(env.cfg.read_text() + '[routed]\nrepo="acme/repo"\n')
    env.bind()
    data = {"headRefOid": head, "author": {"login": "pr-author"}, "reviews": [], "state": "OPEN", **gh}
    set_gh(env, data)
    return data


def set_gh(env, data):
    (env.root / "gh.json").write_text(json.dumps(data))


def ingest(env, tmp_path, lines):
    f = tmp_path / "inbox.txt"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert env.run("inbound", "ingest", "--file", str(f)).returncode == 0


def ask(msg_id, who, text="can you review #101?", where="#eng > API"):
    return f"{msg_id} · {who} · {where} · {text} · reply"


def work(env):
    return json.loads((env.state / "work.json").read_text())


def tick(env):
    return env.run("tick", "--no-notify").stdout


def test_a_reply_whose_pr_is_merged_is_due_now_and_alerts_with_the_state_and_link(env, tmp_path):
    data = setup(env)
    ingest(env, tmp_path, [ask("501", "dana")])
    assert "answer due" not in tick(env)  # open and unreviewed: only the age alert can come
    data["state"] = "MERGED"
    set_gh(env, data)
    out = tick(env)
    assert "answer due now to dana" in out and "merged" in out and PR1 in out and "hubkit inbound replied W1" in out
    assert work(env)[0]["due"] is True


def test_a_review_at_the_current_head_makes_the_answer_due_and_one_at_an_old_head_does_not(env, tmp_path):
    data = setup(env)
    ingest(env, tmp_path, [ask("501", "dana")])
    data["reviews"] = [review(H2)]
    set_gh(env, data)
    assert "answer due" not in tick(env)
    data["reviews"] = [review(H)]
    set_gh(env, data)
    assert "answer due now to dana" in tick(env)


def test_two_asks_and_a_routed_item_on_one_pr_read_gh_once_per_tick(env, tmp_path):
    setup(env)
    env.run("tick", "--no-notify")
    send(env, "peer-a", f"review {PR1}", "review 101")
    ingest(env, tmp_path, [ask("501", "dana"), ask("502", "erin")])
    before = gh_calls(env)
    env.run("tick", "--no-notify")
    assert gh_calls(env) - before == 1


def test_a_reply_naming_two_prs_is_due_only_when_both_are_done(env, tmp_path):
    data = setup(env, state="MERGED")
    ingest(env, tmp_path, [ask("501", "dana", "please look at #101 and #102")])
    assert "answer due now" in tick(env)  # the stand-in answers every PR alike
    set_gh(env, {**data, "state": "OPEN"})
    env.run("tick", "--no-notify")
    assert work(env)[0]["due"] is False


def test_the_stop_gate_holds_a_merged_claim_while_a_person_asked_about_that_pr(env, tmp_path):
    setup(env)
    ingest(env, tmp_path, [ask("501", "dana")])
    held = stop(env, last_assistant_message=f"{PR1} is merged.")
    assert held and "dana asked about it in #eng > API" in held["reason"] and "reply is owed" in held["reason"]
    assert stop(env, last_assistant_message="PR #101 is not merged yet.") is None  # negated: asserts nothing
    assert env.run("inbound", "replied", "501", "--note", "told them").returncode == 0
    assert stop(env, last_assistant_message=f"{PR1} is merged.") is None


def test_inbound_ls_shows_who_asked_the_pr_state_and_whether_the_reply_is_due(env, tmp_path):
    data = setup(env, state="MERGED")
    ingest(env, tmp_path, [ask("501", "dana"), ask("502", "erin", "got a minute for the roadmap?")])
    out = env.run("inbound", "ls").stdout
    assert "2 open asks, 1 ready to answer" in out
    dana, erin = [x for x in out.splitlines() if x.startswith("W")]
    assert "dana" in dana and "#eng > API" in dana and "#101 merged" in dana and "reply due: yes" in dana
    assert "erin" in erin and "no PR named" in erin and "reply due: no" in erin
    set_gh(env, {**data, "state": "OPEN"})
    assert "#101 open, no review at its head" in env.run("inbound", "ls").stdout


def test_inbound_replied_closes_by_work_id_or_message_id_and_needs_a_note(env, tmp_path):
    setup(env)
    ingest(env, tmp_path, [ask("501", "dana"), ask("502", "erin")])
    assert env.run("inbound", "replied", "W1").returncode == 2
    assert env.run("inbound", "replied", "999", "--note", "x").returncode != 0
    assert env.run("inbound", "replied", "502", "--note", "sent the link").returncode == 0
    assert env.run("inbound", "replied", "W1", "--note", "sent the link").returncode == 0
    assert work(env) == []
    ingest(env, tmp_path, [ask("501", "dana")])
    assert work(env) == []  # an answered ask is not reopened


def pre_bash(env, command):
    r = env.hook("pretool", {"hook_event_name": "PreToolUse", "session_id": "sess-hub", "tool_name": "Bash", "tool_input": {"command": command}})
    assert r.returncode == 0


def test_a_send_command_naming_the_person_marks_their_ask_replied(env, tmp_path):
    setup(env)
    ingest(env, tmp_path, [ask("501", "dana"), ask("502", "erin")])
    pre_bash(env, "echo dana")  # not a send command
    assert len(work(env)) == 2
    pre_bash(env, 'mep send --to dana "the review is done"')  # the built-in default send command
    assert [i["owner"] for i in work(env)] == ["erin"]
    pre_bash(env, "mep send --reply-to 502 ok")  # a message id names it too
    assert work(env) == []


def test_the_send_command_regex_comes_from_config(env, tmp_path):
    setup(env)
    env.cfg.write_text(env.cfg.read_text().replace("[tools]\n", '[tools]\nsend_regex="\\\\bchat-send\\\\b"\n'))
    ingest(env, tmp_path, [ask("501", "dana")])
    pre_bash(env, "mep send --to dana hi")  # no longer the send command
    assert len(work(env)) == 1
    pre_bash(env, "chat-send dana hi")
    assert work(env) == []


def close_after(env, tmp_path, who, minutes, n):
    """Ask n times and close each ask `minutes` after it was made (a real edit of the added stamp)."""
    for k in range(n):
        ingest(env, tmp_path, [ask(f"9{k}{len(who)}", who, "hello")])
        rows = work(env)
        for r in rows:
            r["added"] = time.time() - minutes * 60
        (env.state / "work.json").write_text(json.dumps(rows))
        assert env.run("work", "done", rows[-1]["id"], "--note", "answered").returncode == 0


def test_a_person_always_answered_fast_alerts_sooner_than_one_with_no_history(env, tmp_path):
    setup(env)
    close_after(env, tmp_path, "dana", 12, 3)
    ingest(env, tmp_path, [ask("701", "dana", "hello again"), ask("702", "erin", "hello")])
    rows = work(env)
    for r in rows:
        r["added"] = time.time() - 20 * 60
    (env.state / "work.json").write_text(json.dumps(rows))
    out = tick(env)
    assert "reply owed to dana for 20 min, usually answered in 12 min" in out  # their median 12 < the 60 ceiling
    assert "reply owed to erin" not in out  # no history: the configured 60
    assert "usually answered in 12m" in env.run("inbound", "ls").stdout


def test_the_learned_limit_never_goes_below_the_floor(env, tmp_path):
    setup(env)
    close_after(env, tmp_path, "dana", 1, 3)
    ingest(env, tmp_path, [ask("701", "dana", "hello again")])
    rows = work(env)
    rows[0]["added"] = time.time() - 6 * 60
    (env.state / "work.json").write_text(json.dumps(rows))
    assert "reply owed to dana" not in tick(env)  # median 1 min, floor 10
    rows[0]["added"] = time.time() - 11 * 60
    (env.state / "work.json").write_text(json.dumps(rows))
    assert "reply owed to dana" in tick(env)


def test_two_samples_are_too_few_to_learn_from(env, tmp_path):
    setup(env)
    close_after(env, tmp_path, "dana", 12, 2)
    ingest(env, tmp_path, [ask("701", "dana", "hello again")])
    rows = work(env)
    rows[0]["added"] = time.time() - 20 * 60
    (env.state / "work.json").write_text(json.dumps(rows))
    assert "reply owed to dana" not in tick(env)


def test_the_status_block_lists_asks_with_the_due_one_first(env, tmp_path):
    data = setup(env, state="MERGED")
    ingest(env, tmp_path, [ask("501", "dana")])
    tick(env)
    block = env.run("status").stdout
    assert "reply W1 dana" in block and "ANSWER DUE NOW" in block
    assert data
