"""Tasks he gives are priority: opened from his typed prompt, held until he or evidence finishes them, with workers, their own
check clock and a stop gate (his 25 Nov 13:58 ask). Every behaviour runs the real hooks and CLI against scratch state."""

import json
import os
import time

import pytest
from conftest import INTENTS
from test_hubkit import PR1, SEND, send, start_watch

DAY = 24 * 3600


def work(env):
    p = env.state / "work.json"
    return json.loads(p.read_text()) if p.exists() else []


def stop_text(env, last="done", transcript=None):
    payload = {"hook_event_name": "Stop", "session_id": "sess-hub", "last_assistant_message": last}
    if transcript:
        payload["transcript_path"] = str(transcript)
    return env.hook("stop", payload).stdout


def strict(env):
    """The live setting: no prompt opens an order by itself (intents.toml auto_open = false)."""
    mine = env.root / "intents.toml"
    mine.write_text(INTENTS.read_text().replace("[intake]\n", "[intake]\nauto_open = false\n", 1))
    env.cfg.write_text(env.cfg.read_text().replace(str(INTENTS), str(mine)))


def tasks(env):
    return [i for i in work(env) if i.get("task")]


def titles(env):
    return [i["title"] for i in tasks(env)]


def age(env, minutes, *keys):
    """Move a task's clocks into the past (a real file edit)."""
    rows = work(env)
    for r in rows:
        for k in keys or ("added", "checked"):
            if r.get(k):
                r[k] -= minutes * 60
        for w in r.get("workers", []):
            w["t"] -= minutes * 60
    (env.state / "work.json").write_text(json.dumps(rows))


def stop(env, last="Routed it.", **extra):
    r = env.hook("stop", {"hook_event_name": "Stop", "session_id": "sess-hub", "last_assistant_message": last, **extra})
    assert r.returncode == 0
    return json.loads(r.stdout) if r.stdout.strip() else None


def give(env, text="tasks: fix the login bug"):
    env.bind()
    env.prompt(text)
    return tasks(env)


# ---- 1. what opens a task ----


def test_a_tasks_prompt_opens_one_priority_item_per_task_and_leads(env):
    strict(env)
    env.bind()
    out = env.prompt("tasks: 1. fix the login bug 2. update the docs 3. rotate the staging key").stdout
    assert titles(env) == ["fix the login bug", "update the docs", "rotate the staging key"]
    assert all(i["kind"] == "priority" and i["task"] and i["lead"] for i in tasks(env))
    assert [x.split()[2] for x in out.splitlines()[:3]] == ["W1", "W2", "W3"] and out.startswith("hubkit TASK W1")
    assert "NOT an order" in out and "authorises no merge, release, deploy or message" in out
    assert not [o for o in env.rows("orders.jsonl") if o.get("op") == "open"]


@pytest.mark.parametrize("text", ["taks: fix x", "tsaks: fix x", "Task: fix x", "TODO: fix x", "todos: fix x", "to-do: fix x", "new task fix x", "Tasks\n- fix x"])
def test_task_words_and_their_typos_open_a_task(env, text):
    strict(env)
    assert len(give(env, text)) == 1


def test_a_list_introduced_as_tasks_is_split_even_without_a_colon_or_numbers(env):
    strict(env)
    give(env, "here are your tasks\n- fix the login bug\n- update the docs\n  and the changelog")
    assert titles(env) == ["fix the login bug", "update the docs and the changelog"]


def test_a_prompt_that_names_no_task_makes_one_item_quoting_it(env):
    strict(env)
    (t,) = give(env, "I am giving you tasks, keep going until the backlog is empty")
    assert "keep going until the backlog is empty" in t["title"] and t["quotes"]


@pytest.mark.parametrize(
    "text",
    [
        "what tasks are running?",
        "how many tasks are open",
        "show me the tasks",
        "tasks are running fine",
        "if i say tasks it must mean priority",
        'the word "tasks: fix x" is in quotes',
        "list tasks",
        "I think it should also add task.md to track task? A sort of stack?",
        "add the tasks/ folder to the repo",
        "why did you do the tasks",
        "tasks done?",
        "check the subtask: y",
        "<pasted_content>tasks: fix x</pasted_content>",
    ],
)
def test_an_ordinary_mention_opens_nothing(env, text):
    strict(env)
    assert give(env, text) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": 'tasks: fix x', "origin": {"kind": "peer"}},
        {"prompt": '<cross-session-message from-name="peer-a">tasks: fix x</cross-session-message>'},
        {"prompt": "<system-reminder>tasks: fix x</system-reminder>"},
        {"prompt": "[system] tasks: fix x"},
    ],
)
def test_peer_and_harness_text_never_opens_a_task(env, payload):
    strict(env)
    env.bind()
    env.hook("prompt", {"hook_event_name": "UserPromptSubmit", "session_id": "sess-hub", **payload})
    assert tasks(env) == []


def test_the_same_task_given_twice_is_one_item(env):
    strict(env)
    give(env, "tasks: fix the login bug")
    env.prompt("task: Fix the login bug!")
    assert len(tasks(env)) == 1 and tasks(env)[0]["prompts"] == 2


def test_tasks_that_name_a_merge_or_a_deploy_authorise_neither(env):  # default intake: auto_open is on, so only the hook rule keeps orders closed
    out = give(env, "tasks: 1. merge PR 41 2. deploy to prod 3. message Sam")
    assert len(out) == 3 and not [o for o in env.rows("orders.jsonl") if o.get("op") == "open"]
    assert all("NOT an order" in i["note"] for i in out)


def test_a_task_prompt_opens_no_second_urgent_item(env):
    strict(env)
    give(env, "urgent tasks: fix the login bug")
    assert len([i for i in work(env) if i["kind"] == "priority"]) == 1


# ---- 2. criterion and finishing ----


def test_a_done_when_in_the_prompt_is_the_criterion(env):
    strict(env)
    (a,) = give(env, "task: fix the login bug, done when the login test passes")
    assert a["title"] == "fix the login bug" and a["done_when"] == "the login test passes"


def test_a_criterion_on_its_own_line_covers_every_task_of_the_list(env):
    strict(env)
    give(env, "tasks:\n1. fix the login bug\n2. update the docs\nFinish when both are merged")
    assert [i["done_when"] for i in tasks(env)] == ["both are merged"] * 2


def test_done_with_no_criterion_and_no_word_is_refused_naming_both_ways(env):
    strict(env)
    give(env)
    r = env.run("work", "done", "W1", "--note", "looks fine")
    assert r.returncode == 2 and "typed word" in r.stderr and "work criterion W1" in r.stderr
    assert len(tasks(env)) == 1


def test_a_criterion_set_by_the_cli_still_needs_evidence(env):
    strict(env)
    give(env)
    assert env.run("work", "criterion", "W1", "--done-when", "the report exists").returncode == 0
    r = env.run("work", "done", "W1")
    assert r.returncode == 2 and "--evidence" in r.stderr and len(tasks(env)) == 1
    assert tasks(env)[0]["done_when"] == "the report exists"


def test_evidence_files_must_exist_and_postdate_the_task(env):
    strict(env)
    give(env)
    env.run("work", "criterion", "W1", "--done-when", "the report exists")
    assert "does not exist" in env.run("work", "done", "W1", "--evidence", f"file:{env.root}/none.md").stderr
    old = env.root / "old.md"
    old.write_text("x")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    assert "older than the task" in env.run("work", "done", "W1", "--evidence", f"file:{old}").stderr
    assert env.run("work", "done", "W1", "--evidence", "ftp://x").returncode == 2
    new = env.root / "new.md"
    new.write_text("report")
    r = env.run("work", "done", "W1", "--evidence", f"file:{new}")
    assert r.returncode == 0 and tasks(env) == []
    row = [x for x in env.rows(f"archive/archive_{time.strftime('%Y-%m-%d', time.gmtime())}.jsonl") if x.get("id") == "W1"][-1]
    assert row["outcome"] == "done" and "evidence file:" in row["done_proof"]


def fake_pr(env, state):
    """A stand-in gh binary (the tests cannot read a real PR); the state it reports is a file the test rewrites."""
    gh = env.root / "gh"
    (env.root / "gh.json").write_text(json.dumps({"headRefOid": "aaa1111", "reviews": [], "state": state}))
    gh.write_text(f"#!/bin/sh\ncat {env.root / 'gh.json'}\n")
    gh.chmod(0o755)
    if "[tools]" not in env.cfg.read_text():
        env.cfg.write_text(env.cfg.read_text() + f'[tools]\ngh="{gh}"\n')


def test_pr_evidence_is_checked_on_github_and_must_be_merged(env):
    strict(env)
    give(env)
    env.run("work", "criterion", "W1", "--done-when", "the fix is merged")
    fake_pr(env, "OPEN")
    r = env.run("work", "done", "W1", "--evidence", PR1)
    assert r.returncode == 2 and "OPEN" in r.stderr and len(tasks(env)) == 1
    fake_pr(env, "MERGED")
    assert env.run("work", "done", "W1", "--evidence", PR1).returncode == 0 and tasks(env) == []


def test_pr_evidence_that_github_cannot_read_is_refused(env):
    strict(env)
    give(env)
    env.run("work", "criterion", "W1", "--done-when", "the fix is merged")
    (env.root / "gh").write_text("#!/bin/sh\nexit 1\n")
    (env.root / "gh").chmod(0o755)
    env.cfg.write_text(env.cfg.read_text() + f'[tools]\ngh="{env.root / "gh"}"\n')
    assert "could not be read" in env.run("work", "done", "W1", "--evidence", PR1).stderr


def test_his_typed_word_naming_a_task_finishes_it_with_no_criterion(env):
    strict(env)
    give(env, "tasks: 1. fix the login bug 2. update the docs")
    out = env.prompt("mark W1 finished").stdout
    assert titles(env) == ["update the docs"] and "closed W1" in out
    assert "update the docs is done" and env.prompt("the update the docs task is done").stdout is not None
    assert tasks(env) == []


def test_his_word_by_title_closes_the_one_task_it_names(env):
    strict(env)
    give(env, "tasks: 1. fix the login bug 2. update the docs")
    env.prompt("the login bug fix is done")
    assert titles(env) == ["update the docs"]


@pytest.mark.parametrize("text", ["is W1 done?", "W1 is done?", "has the login bug fix finished?", "W1 is not done", "the docs are fine"])
def test_a_question_or_a_denial_closes_nothing(env, text):
    strict(env)
    give(env, "tasks: 1. fix the login bug 2. update the docs")
    env.prompt(text)
    assert len(tasks(env)) == 2


def test_a_peer_saying_it_is_done_closes_nothing(env):
    strict(env)
    give(env)
    env.hook("prompt", {"hook_event_name": "UserPromptSubmit", "session_id": "sess-hub", "prompt": "mark W1 finished", "origin": {"kind": "peer"}})
    env.prompt('<cross-session-message from-name="peer-a">W1 is done</cross-session-message>')
    assert len(tasks(env)) == 1


def test_a_name_that_fits_two_tasks_closes_neither(env):
    strict(env)
    give(env, "tasks: 1. fix the login bug 2. fix the login page")
    out = env.prompt("the login fix is done").stdout
    assert len(tasks(env)) == 2 and "fits 2 open tasks" in out


# ---- 3. workers ----


def agent_brief(env, text, **ti):
    return env.hook("routed", {**SEND, "tool_name": "Agent", "tool_input": {"description": "work", "prompt": text, "run_in_background": True, **ti},
                               "tool_response": {"agentId": "a1b2c3d4e5f6a7b8"}})


def test_a_brief_naming_the_task_id_makes_its_recipient_a_worker(env):
    strict(env)
    give(env)
    r = send(env, "peer-a", "Please take W1 and report back when it is done." + " x" * 120)
    (t,) = tasks(env)
    assert [w["name"] for w in t["workers"]] == ["peer-a"] and t["first_action"] and "task worker recorded" in r.stdout


def test_a_brief_naming_the_title_or_the_pr_of_the_task_does_too(env):
    strict(env)
    give(env, "tasks: 1. fix the login bug 2. check PR #101")
    send(env, "peer-a", "Fix the login bug in the auth module and push, then report. " + "x" * 200)
    send(env, "peer-b", "Look at #101 now. " + "y" * 200)
    t1, t2 = tasks(env)
    assert [w["name"] for w in t1["workers"]] == ["peer-a"] and [w["name"] for w in t2["workers"]] == ["peer-b"]


def test_an_unrelated_brief_adds_no_worker(env):
    strict(env)
    give(env)
    send(env, "peer-a", "Please rebase the billing branch and report back to me. " + "x" * 200)
    assert tasks(env)[0]["workers"] == []


def test_an_agent_brief_records_the_agent_id(env):
    strict(env)
    give(env)
    agent_brief(env, "Do W1. Output: a note. Done-when: the login test passes")
    assert [w["name"] for w in tasks(env)[0]["workers"]] == ["a1b2c3d4e5f6a7b8"]


def test_work_assign_records_a_worker_and_a_task_without_one_says_so(env):
    strict(env)
    give(env)
    assert "NO WORKER" in env.run("status").stdout and "NO WORKER" in env.run("tasks").stdout
    assert env.run("work", "assign", "W1", "--owner", "peer-a").returncode == 0
    out = env.run("status").stdout
    assert "workers: peer-a" in out and "NO WORKER" not in out
    assert env.run("work", "assign", "W9", "--owner", "x").returncode == 4


def test_a_task_sent_to_a_busy_owner_is_warned_to_go_to_a_free_one(env):
    strict(env)
    give(env)
    send(env, "peer-a", f"Review {PR1} and report. " + "x" * 200)
    r = send(env, "peer-a", "Now also W1: fix it. Done-when: the login test passes. " + "x" * 200)
    assert "never queues behind other work" in r.stdout


# ---- 4. checks ----


def tick(env):
    return env.run("tick", "--no-notify").stdout


def test_a_task_check_is_due_at_the_default_15_minutes_and_the_tick_alerts(env):
    strict(env)
    give(env)
    age(env, 14)
    assert "TASK CHECK DUE" not in tick(env)
    age(env, 2)
    out = tick(env)
    assert "ALERT hubkit TASK CHECK DUE W1" in out
    assert "TASK CHECK DUE: 1 task(s)" in env.prompt("how is the weather").stdout


def test_the_watch_exits_with_the_alert_when_a_task_check_is_due(env):
    strict(env)
    give(env)
    age(env, 16)
    r = env.run("watch", "--interval", "1", "--max-cycles", "3", timeout=30)
    assert r.returncode == 0 and "ALERT hubkit TASK CHECK DUE W1" in r.stdout


def test_checkup_interval_sets_the_task_clock_and_config_sets_its_default(env):
    strict(env)
    give(env)
    age(env, 8)
    assert "TASK CHECK DUE" not in tick(env)
    assert "5 min" in env.run("tasks", "interval", "5").stdout
    assert "W1" in tick(env)
    assert "5 min" in env.run("tasks", "interval").stdout and env.run("tasks", "interval", "0").returncode == 2


def test_the_config_default_task_interval(env):
    strict(env)
    env.cfg.write_text(env.cfg.read_text() + "[tasks]\ninterval_min = 20\n")
    give(env)
    age(env, 16)
    assert "TASK CHECK DUE" not in tick(env)
    age(env, 5)
    assert "TASK CHECK DUE" in tick(env)


def test_work_every_sets_one_tasks_clock(env):
    strict(env)
    give(env, "tasks: 1. fix the login bug 2. update the docs")
    assert env.run("work", "every", "W2", "--minutes", "3").returncode == 0
    age(env, 4)
    out = tick(env)
    assert "TASK CHECK DUE W2" in out and "W1" not in out


def test_the_task_list_puts_no_worker_first_and_done_restarts_their_clock(env):
    strict(env)
    give(env, "tasks: 1. fix the login bug 2. update the docs")
    env.run("work", "assign", "W1", "--owner", "peer-a")
    rows = [x for x in env.run("tasks").stdout.splitlines() if x.startswith("  W")]
    assert rows[0].startswith("  W2") and "NO WORKER" in rows[0] and rows[1].startswith("  W1")
    age(env, 16)
    assert "ALERT" in tick(env)
    assert env.run("tasks", "done").returncode == 0
    assert "TASK CHECK DUE" not in tick(env)


# ---- 5. stop gate ----


def live_worker(env, name="worker-1"):
    env.session(sid="s-" + name, name=name)
    env.run("work", "assign", "W1", "--owner", name)


def test_an_open_task_with_no_worker_blocks_the_stop(env):
    strict(env)
    give(env)
    b = stop(env)
    assert b["decision"] == "block" and "TASK OPEN: W1" in b["reason"] and "NO WORKER" not in b["reason"] and "no agent or peer holds it" in b["reason"]


def test_a_live_worker_inside_its_eta_and_a_running_watch_let_the_stop_pass(env):
    strict(env)
    give(env)
    live_worker(env)
    assert "hubkit watch" in stop(env)["reason"]  # no watch: nothing will wake the hub
    start_watch(env)
    assert stop(env) is None


def test_a_worker_past_its_eta_or_not_live_blocks(env):
    strict(env)
    give(env)
    live_worker(env)
    start_watch(env)
    age(env, 40, "added", "checked")  # past the 30 min default ETA from when it took the task
    assert "past its ETA" in stop(env)["reason"]
    env.run("work", "assign", "W1", "--owner", "gone-peer")
    r = stop(env)
    assert "TASK OPEN" in r["reason"]


def test_a_task_waiting_on_an_ask_item_naming_it_passes(env):
    strict(env)
    give(env)
    start_watch(env)
    assert "TASK OPEN" in stop(env)["reason"]
    env.run("work", "add", "--kind", "ask", "--title", "W1: which database?", "--note", "blocks W1")
    assert stop(env) is None


def test_a_named_blocker_needs_a_fix_and_a_holder(env):
    strict(env)
    give(env)
    start_watch(env)
    assert env.run("work", "block", "W1", "--on", "no host").returncode == 2
    assert env.run("work", "block", "W1", "--on", "no host", "--fix", "start the box", "--holder", "box-a").returncode == 0
    assert stop(env) is None
    env.run("work", "block", "W1", "--on", "")
    assert "TASK OPEN" in stop(env)["reason"]


def test_the_gate_blocks_twice_for_the_same_reason_then_lets_the_turn_end_with_a_line(env):
    strict(env)
    give(env)
    start_watch(env)
    assert stop(env)["decision"] == "block" and stop(env)["decision"] == "block"
    r = stop(env)
    assert "decision" not in r and "tasks still open" in r["systemMessage"] and "W1" in r["systemMessage"]
    env.prompt("how is the weather")  # a new turn: blocks again
    assert stop(env)["decision"] == "block"


def test_stop_hook_active_is_let_through(env):
    strict(env)
    give(env)
    assert stop(env, stop_hook_active=True) is None


def test_his_stop_holds_the_tasks_and_resume_frees_them(env):
    strict(env)
    give(env, "tasks: 1. fix the login bug 2. update the docs")
    start_watch(env)
    out = env.prompt("stop").stdout
    assert "holds ALL 2 open task(s)" in out and all(i["held"] for i in tasks(env))
    assert stop(env) is None and "HELD" in env.run("status").stdout
    age(env, 60)
    assert "TASK CHECK DUE" not in tick(env)
    assert "resumes" in env.prompt("resume the tasks").stdout and not any(i.get("held") for i in tasks(env))
    assert "TASK OPEN" in stop(env)["reason"]


@pytest.mark.parametrize("text", ["halt", "pause the tasks", "Stop!", "please stop all tasks"])
def test_stop_words_hold(env, text):
    strict(env)
    give(env)
    env.prompt(text)
    assert tasks(env)[0].get("held")


def test_stop_in_a_sentence_holds_nothing(env):
    strict(env)
    give(env)
    env.prompt("stop the build from failing on main")
    assert not tasks(env)[0].get("held")


# ---- 6. fast track ----


def test_the_nudge_is_re_brief_after_one_missed_check(env):
    strict(env)
    give(env)
    live_worker(env)
    age(env, 40, "added", "checked")
    first = env.run("tasks").stdout
    assert "SendMessage the worker" in first and "re-brief" not in first
    env.run("tasks", "done")
    assert "re-brief W1 to another free owner now" in env.run("tasks").stdout


def test_a_task_leads_the_status_block_before_other_priority_items(env):
    strict(env)
    env.bind()
    env.run("work", "add", "--kind", "priority", "--title", "an older priority")
    env.prompt("task: fix the login bug")
    lines = env.run("status").stdout.splitlines()
    assert next(x for x in lines if x.startswith("task:") or x.startswith("priority:")).startswith("task: W2")
    assert env.run("work", "ls").stdout.splitlines()[0].startswith("W2 [task]")


# ---- 7. nothing existing is lost ----


def test_live_items_orders_and_the_bind_survive_and_legacy_items_are_not_gated(env):
    env.bind()
    env.run("work", "add", "--kind", "priority", "--title", "look at the old thing")
    env.run("work", "add", "--kind", "routed", "--owner", "peer-a", "--title", "check the billing fix and report back")
    env.run("work", "add", "--kind", "tracked", "--title", "a tracked thing")
    env.run("order", "open", "--kind", "hotfix", "--quote", "hotfix now")
    before = work(env)
    orders_before = env.run("order", "ls").stdout
    for cmd in (("status",), ("tasks",), ("tick", "--no-notify"), ("doctor",)):
        assert env.run(*cmd).returncode == 0
    env.prompt("how is the weather")
    assert work(env) == before and env.run("order", "ls").stdout == orders_before
    assert "bound" not in env.run("doctor").stdout and "bind: live" in env.run("doctor").stdout
    assert not [r for r in env.rows("hook-errors.jsonl")]
    assert "TASK OPEN" not in (stop(env) or {}).get("reason", "")


def test_a_legacy_priority_item_is_not_merged_into_or_gated_as_a_task(env):
    strict(env)
    env.bind()
    env.run("work", "add", "--kind", "priority", "--title", "look at the old thing")
    env.prompt("tasks: fix the login bug")
    old, new = work(env)
    assert not old.get("task") and new["task"] and old["id"] != new["id"]


def test_a_legacy_priority_item_alone_does_not_hold_the_stop_as_a_task(env):
    strict(env)
    env.bind()
    env.run("work", "add", "--kind", "priority", "--title", "look at the old thing")
    assert "TASK OPEN" not in json.dumps(stop(env))


def test_a_covered_task_is_not_counted_as_a_missed_nudge(env):
    strict(env)
    give(env)
    env.run("work", "block", "W1", "--on", "no host", "--fix", "start the box", "--holder", "box-a")
    env.run("tasks", "done")
    env.run("tasks", "done")
    assert not [i for i in tasks(env) if i.get("nudges")]


def test_a_bare_stop_says_it_holds_all_and_naming_one_holds_only_that_one(env):
    strict(env)
    give(env, "tasks: 1. fix the login bug 2. update the docs")
    out = env.prompt("stop W2").stdout
    assert "holds W2" in out and "only" in out
    held = {i["id"]: bool(i.get("held")) for i in tasks(env)}
    assert held == {"W1": False, "W2": True}
    env.prompt("resume W2")
    out = env.prompt("pause the login bug").stdout
    assert {i["id"]: bool(i.get("held")) for i in tasks(env)} == {"W1": True, "W2": False} and "only" in out
    env.prompt("resume all")
    assert "holds ALL 2 open" in env.prompt("stop").stdout and all(i["held"] for i in tasks(env))


def test_a_done_when_after_a_semicolon_covers_every_task_of_the_line_and_says_so(env):
    strict(env)
    out = give(env, "tasks: 1. fix the login bug 2. update the docs; done when the report file exists")
    assert [t["done_when"] for t in out] == ["the report file exists"] * 2
    assert "covers all 2 tasks" in env.prompt("hello").stdout


def test_a_done_when_inside_one_task_belongs_to_that_task_only(env):
    strict(env)
    out = give(env, "tasks: 1. fix the login bug 2. update the docs done when the page is live")
    assert [t["done_when"] for t in out] == ["", "the page is live"]


def test_evidence_other_than_a_merged_pr_or_a_file_is_refused_with_how_to_give_a_file(env):
    strict(env)
    give(env)
    env.run("work", "criterion", "W1", "--done-when", "the page is live")
    r = env.run("work", "done", "W1", "--evidence", "https://example.org/status")
    assert r.returncode == 2 and "file:<path>" in r.stderr and len(tasks(env)) == 1


def test_a_criterion_the_hub_wrote_is_marked_as_not_his(env):
    strict(env)
    give(env, "tasks: 1. fix the login bug 2. update the docs done when the page is live")
    env.run("work", "criterion", "W1", "--done-when", "the report exists")
    out = env.run("status").stdout
    assert "the report exists [written by the hub" in out and "the page is live [written" not in out


QUOTED = [
    'I might later say "stop" if it goes wrong',
    'I might later say "mark W1 finished" once it is merged',
    'if I type "W1 is done" then close it',
    'the word "finish W1" is a trigger',
    'I could write "W1 done" later',
    'later I will say "stop W1" for one of them',
    'say "pause the login bug" to hold one',
]


@pytest.mark.parametrize("text", QUOTED)
def test_quoted_words_close_and_hold_nothing(env, text):
    strict(env)
    give(env, "tasks: 1. fix the login bug 2. update the docs")
    env.prompt(text)
    assert len(tasks(env)) == 2 and not any(i.get("held") for i in tasks(env))


@pytest.mark.parametrize("text", ['I might say "resume" later', 'say "resume W1" when ready'])
def test_quoted_resume_frees_nothing(env, text):
    strict(env)
    give(env, "tasks: 1. fix the login bug 2. update the docs")
    env.prompt("stop")
    env.prompt(text)
    assert all(i.get("held") for i in tasks(env))


QUOTED2 = [
    "I might say 'mark W1 finished' later",
    "later I will say 'stop' if it goes wrong",
    "say 'stop W1' to hold one",
    "> mark W1 finished",
    "&gt; stop",
    "he wrote:\n> W1 is done\nok",
]


@pytest.mark.parametrize("text", QUOTED2)
def test_single_quoted_and_quoted_reply_words_do_nothing(env, text):
    strict(env)
    give(env, "tasks: 1. fix the login bug 2. update the docs")
    env.prompt(text)
    assert len(tasks(env)) == 2 and not any(i.get("held") for i in tasks(env))


def test_apostrophes_are_not_quotes(env):
    strict(env)
    give(env, "tasks: 1. fix the login bug 2. update the docs")
    env.prompt("stop W1, it's Sam's call, don't ask")
    assert any(i.get("held") for i in tasks(env))


NEGATED = [
    "don't mark W1 finished",
    "please don\u2019t mark W1 finished yet",
    "do not mark W1 finished",
    "never mark W1 finished",
    "W1 isn't done",
    "W1 wasn't finished",
    "you can't call W1 done",
    "it doesn't mean W1 is done",
]


@pytest.mark.parametrize("text", NEGATED)
def test_a_negated_finish_closes_nothing(env, text):
    strict(env)
    give(env, "tasks: 1. fix the login bug 2. update the docs")
    env.prompt(text)
    assert len(tasks(env)) == 2


def test_the_plain_finish_still_closes_it(env):
    strict(env)
    give(env, "tasks: 1. fix the login bug 2. update the docs")
    env.prompt("mark W1 finished")
    assert [i["id"] for i in tasks(env)] == ["W2"]
