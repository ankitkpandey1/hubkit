"""Threads: one piece of work, one entry. Linking rules, the `mine` view, closing the root, and the declared check for work with no PR.

Real CLI, hooks and files on scratch state. The `gh` binary is not used: nothing here reads GitHub (PR refs are text)."""

import json

from test_hubkit import age_routed, routed_items, send
from test_peer_reports import deliver, peer_entry
from test_replies import ask, ingest

PR = "https://github.com/acme/repo/pull/12"
ASK1 = "delete the stale branches"
BRIEF = "Delete the stale branches in the sandbox repo. List them first, keep anything touched in the last month, and remove the rest one by one. " * 2


def work(env):
    return json.loads((env.state / "work.json").read_text())


def by_id(env, wid):
    return next(i for i in work(env) if i["id"] == wid)


def request_id(env):
    return next(i["id"] for i in work(env) if i["kind"] == "request")


def open_request(env, text=ASK1):
    env.bind()
    env.prompt(text)
    return request_id(env)


def add_request(env, text):
    """A second open ask, opened by hand (the intake would not take every wording)."""
    before = {i["id"] for i in work(env)}
    assert env.run("req", "add", text).returncode == 0
    return next(i["id"] for i in work(env) if i["id"] not in before)


# ---- (1) linking ----

def test_a_job_routed_while_exactly_one_ask_is_open_links_to_it(env):
    rid = open_request(env)
    out = send(env, "peer-a", BRIEF + "\nDone-when: no stale branch is listed.").stdout
    (item,) = routed_items(env)
    assert item["thread"] == rid and f"linked under {rid}" in out and "hubkit thread unlink" in out


def test_two_open_asks_that_both_fit_mean_no_link(env):
    open_request(env)
    add_request(env, "delete the stale branches older than a year please")
    out = send(env, "peer-a", BRIEF).stdout
    (item,) = routed_items(env)
    assert "thread" not in item and "ambiguous" in out


def test_key_words_pick_the_one_ask_that_fits_among_several(env):
    rid = open_request(env)
    add_request(env, "rotate the database credentials for staging")
    send(env, "peer-a", BRIEF)
    (item,) = routed_items(env)
    assert item["thread"] == rid


def test_one_shared_word_or_a_small_share_of_the_ask_is_not_a_fit(env):
    rid = open_request(env)
    add_request(env, "stale logs")  # shares one word with the brief
    add_request(env, "stale branches tickets about billing invoices")  # shares two words, a third of its own
    send(env, "peer-a", BRIEF)
    (item,) = routed_items(env)
    assert item["thread"] == rid


def test_the_only_open_ask_naming_other_prs_is_not_a_link(env):
    env.bind()
    env.prompt(f"please rebase {PR} on main")
    send(env, "peer-a", "Review https://github.com/acme/repo/pull/99 and send the verdict with the findings and the sha you read.")
    (item,) = routed_items(env)
    assert "thread" not in item


def test_a_for_line_links_explicitly_and_wins_over_the_automatic_rule(env):
    open_request(env)
    other = add_request(env, "rotate the database credentials for staging")
    send(env, "peer-a", BRIEF + f"\nFor: {other}\n")
    (item,) = routed_items(env)
    assert item["thread"] == other


def test_a_for_line_naming_nothing_links_nothing_and_says_so(env):
    open_request(env)
    out = send(env, "peer-a", BRIEF + "\nFor: W99\n").stdout
    (item,) = routed_items(env)
    assert "names no other record" in out
    assert "thread" not in item  # the For: line wins even when it fails: no guess from the only open ask


def test_a_reply_and_an_order_naming_the_same_pr_share_a_thread(env, tmp_path):
    env.bind()
    env.prompt(f"hotfix {PR} now, finish it asap")
    oid = next(r["id"] for r in env.rows("orders.jsonl") if r["op"] == "open")
    ingest(env, tmp_path, [ask("501", "dana", text=f"can you look at {PR}?")])
    (reply,) = [i for i in work(env) if i["kind"] == "reply"]
    assert reply["thread"] == oid


def test_a_reply_is_the_root_when_nothing_older_exists_and_adopts_the_job(env, tmp_path):
    env.bind()
    send(env, "peer-a", f"Review {PR} and send the verdict with the findings and the sha you read, then stop.")
    ingest(env, tmp_path, [ask("501", "dana", text=f"can you look at {PR}?")])
    (reply,) = [i for i in work(env) if i["kind"] == "reply"]
    (job,) = routed_items(env)
    assert job["thread"] == reply["id"] and "thread" not in reply


def test_by_hand_link_and_unlink_and_their_refusals(env):
    rid = open_request(env)
    env.run("work", "add", "--kind", "active", "--title", "triage")
    wid = next(i["id"] for i in work(env) if i["kind"] == "active")
    assert env.run("thread", "link", wid, rid).returncode == 0 and by_id(env, wid)["thread"] == rid
    assert env.run("thread", "link", rid, wid).returncode == 3  # a root cannot go under its own member
    assert env.run("thread", "link", wid, "W99").returncode == 4
    assert env.run("thread", "link", wid).returncode == 2
    assert env.run("thread", "unlink", rid).returncode == 3  # a root with members
    assert env.run("thread", "unlink", wid).returncode == 0 and "thread" not in by_id(env, wid)


# ---- (2) the view ----

def test_mine_prints_one_entry_per_thread_and_not_its_members_again(env, tmp_path):
    rid = open_request(env)
    send(env, "peer-a", BRIEF)
    out = env.run("mine").stdout.splitlines()
    assert sum(x.startswith(f"  {rid} ") for x in out) == 1
    head = next(x for x in out if x.startswith(f"  {rid} "))
    assert "delete the stale branches" in head and "W2 with peer-a (routed)" in head and "(on peer-a)" in head
    assert "    W2 routed peer-a routed" in out
    peers = out.index(next(x for x in out if x.startswith("ON PEERS AND AGENTS")))
    assert out[peers] == "ON PEERS AND AGENTS (1)" and head in out[peers:]
    assert not any(x.startswith("  W2 ") for x in out)  # the job is not listed again on its own
    assert "ON THE HUB (0)" in out


def test_an_unthreaded_item_prints_as_before(env):
    env.bind()
    env.run("work", "add", "--kind", "active", "--title", "triage")
    out = env.run("mine").stdout
    assert "ON THE HUB (1)" in out and "  W1 0m: [active] triage" in out


def test_mine_json_has_threads_as_records_and_thread_ls_show_agree(env):
    rid = open_request(env)
    send(env, "peer-a", BRIEF)
    doc = json.loads(env.run("mine", "--json").stdout)
    (t,) = doc["threads"]
    assert t["id"] == rid and t["group"] == "peers" and [m["id"] for m in t["members"]] == ["W2"] and "lines" in doc
    assert json.loads(env.run("thread", "ls", "--json").stdout)["threads"][0]["id"] == rid
    assert f"{rid} 0m: delete the stale branches" in env.run("thread", "ls").stdout
    shown = env.run("thread", "show", "W2").stdout  # any id in the thread shows the whole chain
    assert shown.splitlines()[0].startswith(f"{rid} \"delete the stale branches\"") and "routed peer-a: W2" in shown and "now: W2 with peer-a" in shown
    assert json.loads(env.run("thread", "show", rid, "--json").stdout)["thread"]["id"] == rid
    assert env.run("thread", "show", "W99").returncode == 4


def test_a_reply_that_is_due_moves_the_thread_to_the_hub_and_says_who_is_owed(env, tmp_path):
    env.bind()
    env.prompt(f"hotfix {PR} now, finish it asap")
    oid = next(r["id"] for r in env.rows("orders.jsonl") if r["op"] == "open")
    ingest(env, tmp_path, [ask("501", "dana", text=f"can you look at {PR}?")])
    f = env.state / "work.json"
    items = work(env)
    for i in items:
        if i["kind"] == "reply":
            i["due"] = True  # what replies.note_states stamps when the PR is settled (it reads GitHub, which a test cannot)
    f.write_text(json.dumps(items))
    out = env.run("mine").stdout
    assert "ON THE HUB (1)" in out and f"  {oid} " in out and "reply owed to dana: the PR work is settled" in out
    assert "    W1 reply owed to dana due" in out


# ---- (3) closing ----

def test_when_the_last_step_closes_the_thread_says_so_with_the_close_command(env):
    rid = open_request(env)
    send(env, "peer-a", BRIEF)
    r = env.run("work", "done", "W2", "--note", "review APPROVED at abc1234def")
    assert r.returncode == 0
    msg = f'{rid} "delete the stale branches": every step under it is closed (W2 routed done: review APPROVED at abc1234def). NEXT: hubkit req done {rid} --evidence'
    assert msg in r.stdout
    assert msg in env.run("tick", "--no-notify").stdout  # and the tick alerts
    assert "every step is closed" in env.run("mine").stdout  # the thread stays in view until the hub closes it
    assert env.run("req", "done", rid, "--evidence", "branches gone, checked").returncode == 0
    assert "every step under it is closed" not in env.run("tick", "--no-notify").stdout


def test_an_open_step_keeps_the_thread_quiet(env):
    rid = open_request(env)
    send(env, "peer-a", BRIEF)
    send(env, "peer-b", BRIEF + "\nFor: " + rid)
    out = env.run("work", "done", "W2", "--note", "done and checked by hand").stdout
    assert "every step under it is closed" not in out
    assert "every step under it is closed" not in env.run("tick", "--no-notify").stdout


def test_an_order_root_gets_the_order_close_form_with_the_evidence_names_and_the_sha(env):
    env.bind()
    env.prompt(f"merge {PR} now")
    oid = next(r["id"] for r in env.rows("orders.jsonl") if r["op"] == "open")
    send(env, "peer-a", f"Merge {PR} with the squash method and report the merge commit sha, nothing else, then stop and wait for me.\nFor: {oid}")
    msg = env.run("work", "done", "W1", "--note", "merged as 0123abcd4567").stdout
    assert f'NEXT: hubkit order close {oid} --evidence "merged={PR} 0123abcd4567"' in msg


def test_closing_a_root_with_open_steps_warns_and_still_closes(env):
    rid = open_request(env)
    send(env, "peer-a", BRIEF)
    r = env.run("req", "done", rid, "--evidence", "did it myself, branches gone")
    assert r.returncode == 0
    assert f"hubkit WARNING: {rid} is closed but 1 step(s) under it are still open: W2 routed peer-a (routed). NEXT: hubkit thread show {rid}" in r.stdout
    assert request_id_gone(env, rid)


def request_id_gone(env, rid):
    return all(i["id"] != rid for i in work(env))


# ---- (4) declared check for work with no PR ----

def test_a_done_when_line_is_stored_and_a_brief_without_one_is_warned(env):
    env.bind()
    out = send(env, "peer-a", BRIEF + "\nDone-when: ls shows no old-* branch\n").stdout
    (item,) = routed_items(env)
    assert item["done_when"] == "ls shows no old-* branch" and "no `Done-when:` line" not in out
    out = send(env, "peer-b", BRIEF.replace("stale", "old")).stdout
    assert "this job names no PR and no `Done-when:` line, so nothing can show it is done. NEXT: add `Done-when: <command, file or URL that shows it>` to the brief" in out


def test_the_warning_can_be_switched_off_and_a_pr_or_a_review_needs_none(env):
    env.bind()
    out = send(env, "peer-a", f"Rebase {PR} on main and push, then report the new head sha and the CI state, nothing else, and stop.").stdout
    assert "Done-when" not in out
    env.cfg.write_text(env.cfg.read_text() + '[routed]\nrequire_done_when="off"\n')
    assert "Done-when" not in send(env, "peer-b", BRIEF).stdout


def test_a_done_report_names_the_declared_check_and_work_done_needs_a_note(env):
    env.bind()
    send(env, "peer-a", BRIEF + "\nDone-when: ls shows no old-* branch\n")
    age_routed(env, 40)
    shown = deliver(env, peer_entry("peer-a", "Done, the branches are deleted. Output: old-1, old-2."))
    nudge = "W1: peer-a reported done. Check: ls shows no old-* branch. NEXT: run the check, then hubkit work done W1 --note \"<what the check showed>\""
    assert nudge in shown
    assert nudge in env.run("status").stdout and nudge in env.run("tick", "--no-notify").stdout
    r = env.run("work", "done", "W1")
    assert r.returncode == 3 and "NEXT: hubkit work done W1 --note \"<what the check showed>\"" in r.stderr
    assert env.run("work", "done", "W1", "--note", "ls showed none").returncode == 0


def test_a_no_pr_item_without_a_declared_check_still_closes_without_a_note(env):
    env.bind()
    send(env, "peer-a", BRIEF)
    assert env.run("work", "done", "W1").returncode == 0


# ---- the other ways a record joins a thread ----

def test_what_a_standing_rule_opens_joins_the_thread_of_its_pr(env, tmp_path):
    env.bind()
    env.prompt(f"hotfix {PR} now, finish it asap")
    oid = next(r["id"] for r in env.rows("orders.jsonl") if r["op"] == "open")
    assert env.run("standing", "add", "--on", "inbound", "--quote", "tell me about every ask", "--action", "look at the PR").returncode == 0
    ingest(env, tmp_path, [ask("501", "dana", text=f"can you look at {PR}?")])
    fired = [i for i in work(env) if i["kind"] == "active" and i["title"].startswith("standing S1")]
    assert len(fired) == 1 and fired[0]["thread"] == oid


def test_the_tick_links_a_job_to_an_ask_opened_after_it_when_they_name_the_same_pr(env):
    env.bind()
    send(env, "peer-a", f"Review {PR} and send the verdict with the findings and the sha you read, then stop.")
    (item,) = routed_items(env)
    assert "thread" not in item
    env.prompt(f"hotfix {PR} now, finish it asap")
    oid = next(r["id"] for r in env.rows("orders.jsonl") if r["op"] == "open")
    env.run("tick", "--no-notify")
    assert routed_items(env)[0]["thread"] == oid
