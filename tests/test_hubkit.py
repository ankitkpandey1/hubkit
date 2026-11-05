import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(os.environ.get("HUBKIT_SCRIPTS", Path(__file__).resolve().parent.parent / "scripts"))))
from conftest import SCRIPTS

HOTFIX = "we need a hotfix for the billing bug now, finish it asap"
TOKEN_SHAPES = [
    "AKIA" + "IOSFODNN7EXAMPLE",
    "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4",
    "xoxb-" + "123456789012-abcdefABCDEF",
    "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + ".abcDEF123456xyz",
    "-----BEGIN RSA " + "PRIVATE KEY-----",
    "postgres://user:" + "hunter2pass@db.example.com/x",
    "Authorization: Bearer " + "abcdef1234567890XYZ",
    "password=" + "s3cr3tvalue",
    "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
]


def order_ids(env):
    return [r["id"] for r in env.rows("orders.jsonl") if r["op"] == "open"]


# ---- 1. orders: opened record, clock, evidence to close ----

def test_prompt_opens_order_with_dispatch_line(env):
    env.bind()
    r = env.prompt(HOTFIX)
    assert r.returncode == 0
    assert "hubkit order O-" in r.stdout and "kind=hotfix" in r.stdout and "First action due in 5 min" in r.stdout
    assert len(order_ids(env)) == 1


def test_close_refused_without_evidence_then_accepted(env):
    env.bind()
    env.prompt(HOTFIX)
    oid = order_ids(env)[0]
    bad = env.run("order", "close", oid)
    assert bad.returncode == 2 and "missing evidence 'pr_merged'" in bad.stderr
    half = env.run("order", "close", oid, "--evidence", "pr_merged=https://example.com/pr/1")
    assert half.returncode == 2 and "prod_live" in half.stderr
    log = env.root / "run.log"
    log.write_text("live")
    ok = env.run("order", "close", oid, "--evidence", "pr_merged=https://example.com/pr/1",
                 "--evidence", f"prod_live=file:{log}")
    assert ok.returncode == 0 and "closed" in ok.stdout
    again = env.run("order", "close", oid, "--evidence", "pr_merged=x")
    assert again.returncode == 0 and "already closed" in again.stdout
    arch = [r for f in (env.state / "archive").glob("archive_*.jsonl") for r in map(json.loads, f.read_text().splitlines())]
    assert [a["outcome"] for a in arch if a["type"] == "order"] == ["closed"]


def test_file_evidence_older_than_order_is_refused(env):
    env.bind()
    log = env.root / "old.log"
    log.write_text("x")
    os.utime(log, (time.time() - 3600, time.time() - 3600))
    env.prompt(HOTFIX)
    oid = order_ids(env)[0]
    r = env.run("order", "close", oid, "--evidence", "pr_merged=https://example.com/pr/1",
                "--evidence", f"prod_live=file:{log}")
    assert r.returncode == 2 and "older than the order" in r.stderr


def test_hold_and_cancel_verbs_and_prompt_intents(env):
    env.bind()
    env.prompt(HOTFIX)
    oid = order_ids(env)[0]
    r = env.prompt("don't deploy the hotfix yet")
    assert "held" in r.stdout
    assert "[held]" in env.run("order", "ls").stdout
    env.backdate(100)  # a held order has no clock (and is under the 120 min review limit)
    assert env.run("tick", "--no-notify").stdout == ""
    assert env.run("order", "resume", oid).returncode == 0
    r = env.prompt("cancel the hotfix")
    assert "cancelled" in r.stdout
    assert env.run("order", "ls").stdout == ""
    assert env.run("order", "close", oid).returncode == 2
    arch = [json.loads(x) for f in (env.state / "archive").glob("*.jsonl") for x in f.read_text().splitlines()]
    assert arch[-1]["outcome"] == "cancelled"
    assert env.run("order", "cancel", oid, "--quote", "").returncode == 2


def test_question_with_order_word_opens_nothing(env):
    env.bind()
    for text in ["did the release branch get cut?", "the release notes look fine", "why did you merge it",
                 "c anwe do elwease?", "can we do the release?"]:
        out = env.prompt(text).stdout
        assert "hubkit order" not in out and "chase" not in out, text
    assert order_ids(env) == []


def test_unconfirmed_order_expires_and_has_no_clock(env):
    env.bind()
    assert env.run("order", "open", "--kind", "release", "--quote", "maybe a release", "--unconfirmed").returncode == 0
    env.backdate(20)
    assert env.run("tick", "--no-notify").stdout == ""  # no clock alerts while unconfirmed
    env.backdate(20)
    out = env.run("tick", "--no-notify").stdout
    assert "unconfirmed" in out and "expired" in out
    assert env.run("order", "ls").stdout == ""


def test_held_order_raises_review_alert_after_limit(env):
    env.bind()
    env.prompt(HOTFIX)
    env.prompt("hold the hotfix")
    env.backdate(130)
    assert "held for" in env.run("tick", "--no-notify").stdout


def test_cancel_without_order_object_cancels_nothing(env):
    env.bind()
    env.prompt(HOTFIX)
    for t in ["cancel my meeting", "cancel the standup tomorrow"]:
        assert "cancelled" not in env.prompt(t).stdout
    assert "[open]" in env.run("order", "ls").stdout
    assert "cancelled" in env.prompt("cancel it").stdout


def test_why_did_you_merge_it_is_not_a_chase(env):
    env.bind()
    env.prompt(HOTFIX)
    env.backdate(2)
    assert "chase recorded" not in env.prompt("why did you merge it").stdout


def test_chases_per_order_metric(env):
    env.bind()
    env.prompt(HOTFIX)
    env.prompt("what is the hold up?")
    env.backdate(2)
    r = env.prompt("eta?")
    assert "chase recorded" in r.stdout
    m = json.loads(env.run("order", "metrics").stdout)
    assert m == {"orders": 1, "chases": 2, "chases_per_order": 2.0}


def test_repeated_order_is_neither_second_order_nor_chase(env):
    env.bind()
    env.prompt(HOTFIX)
    env.prompt("do the hotfix now")
    assert len(order_ids(env)) == 1
    assert json.loads(env.run("order", "metrics").stdout)["chases"] == 0


def test_dont_wait_merge_now_keeps_clock_and_pr_ref_is_not_a_chase(env):
    """Reviewer's hotfix scenario, exact phrases."""
    env.bind()
    env.prompt(HOTFIX)
    r = env.prompt("don't wait, merge the hotfix now")
    assert "held" not in r.stdout and "[open]" in r.stdout
    r = env.prompt("hotfix PR 12 merge it")
    assert "chase recorded" not in r.stdout and "ref PR 12 added" in r.stdout
    assert json.loads(env.run("order", "metrics").stdout)["chases"] == 0
    assert "[open]" in env.run("order", "ls").stdout and "PR 12" in " ".join(
        sum([r.get("refs", []) for r in env.rows("orders.jsonl")], []))
    r = env.prompt("pause the hotfix? no, merge it now")
    assert "held" not in r.stdout and "[open]" in env.run("order", "ls").stdout
    env.backdate(30)
    assert "no movement" in env.run("tick", "--no-notify").stdout  # the clock runs


def test_hold_phrases_are_explicit_not_bare_dont_or_wait(env):
    env.bind()
    env.prompt(HOTFIX)
    assert "held" not in env.prompt("don't wait, merge the hotfix").stdout
    assert "held" not in env.prompt("wait, what is the eta on the hotfix").stdout
    assert "[held]" in env.run("order", "ls").stdout or "held" in env.prompt("put the hotfix on hold").stdout


def test_urgent_words_restart_a_held_order(env):
    env.bind()
    env.prompt(HOTFIX)
    env.prompt("hold the hotfix")
    assert "[held]" in env.run("order", "ls").stdout
    r = env.prompt("ok merge the hotfix now")
    assert "resumed" in r.stdout and "[open]" in env.run("order", "ls").stdout


def test_stop_loss_after_repeated_chases(env):
    env.bind()
    env.prompt(HOTFIX)
    out = ""
    for i in range(3):
        env.backdate(2)
        out = env.prompt("what is the hold up?").stdout
    assert "STOP-LOSS" in out and "chased 3 times" in out


def test_overdue_order_reaches_hub_at_next_prompt(env):
    env.bind()
    env.prompt(HOTFIX)
    env.backdate(30)
    out = env.prompt("a normal question about something else").stdout
    assert "OVERDUE" in out and "no first action" in out


def test_correction_phrase_and_repeated_instruction_inject_record_prompt(env):
    env.bind()
    r = env.prompt("why did you run the tests yourself instead of routing")
    assert "CORRECTION (correction phrase)" in r.stdout and "learn.py add" in r.stdout
    assert "run the tests yourself" in r.stdout
    env.prompt("update the status file with the open items please")
    r = env.prompt("update the status file with the open items please")
    assert "CORRECTION (repeated instruction)" in r.stdout
    assert env.prompt("thanks that looks fine").stdout == ""


def test_correction_quote_is_redacted(env):
    env.bind()
    r = env.prompt("I told you to use password=" + "hunter2value for it")
    assert "CORRECTION" in r.stdout and "hunter2value" not in r.stdout


def test_promote_then_address_needs_a_real_rule_change(env):
    f = env.root / "learnings.md"
    ov = env.root / "overlay.md"
    ov.write_text("# rules\n")
    env.learn("add", "--class", "route-it", "--gate", "G1", "--quote", "q", "--situation", "s", "--wrong", "w", "--right", "r", "--evidence", "e")
    r = env.learn("address", "--class", "route-it", "--how", "thought about it")
    assert r.returncode == 2 and "refused" in r.stderr
    ov.write_text("# rules\nunrelated edit without the tag\n")
    r = env.learn("address", "--class", "route-it", "--how", "edited something else")
    assert r.returncode == 2 and "no rule line tagged [route-it]" in r.stderr
    p = env.learn("promote", "route-it", "--gate", "G1", "--rule", "Route work to agents.")
    assert p.returncode == 0 and "promoted" in p.stdout
    p2 = env.learn("promote", "other-gate", "--gate", "G2", "--rule", "x")
    assert p2.returncode == 2
    text = ov.read_text()
    assert "## Promoted rules" in text and "### G1" in text and "- Route work to agents. [route-it]" in text
    assert "skip" in env.learn("promote", "route-it", "--gate", "G1", "--rule", "Route work to agents.").stdout
    ok = env.learn("address", "--class", "route-it", "--how", "overlay rule G1")
    assert ok.returncode == 0 and "addressed" in ok.stdout
    assert "- addressed:" in f.read_text()


def test_tick_if_bound_is_noop_without_hub_or_orders(env):
    r = env.run("tick", "--if-bound", "--no-notify")
    assert r.returncode == 0 and not (env.state / "status.json").exists()
    env.bind()
    env.run("tick", "--if-bound", "--no-notify")
    assert (env.state / "status.json").exists()


def test_block_expires_old_and_marks_probation(env):
    env.bind()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    env.learn("add", "--class", "stale-one", "--quote", "q", "--date", "2026-01-01", "--situation", "s", "--wrong", "w", "--right", "r-old", "--evidence", "e")
    env.learn("add", "--class", "fresh-one", "--quote", "q", "--date", today, "--situation", "s", "--wrong", "w", "--right", "r-new", "--evidence", "e")
    env.learn("add", "--class", "probe-one", "--quote", "q", "--date", today, "--situation", "s", "--wrong", "w", "--right", "r-probe", "--evidence", "e")
    (env.root / "overlay.md").write_text("rule\n")
    env.learn("promote", "probe-one", "--gate", "G1", "--rule", "probe rule")
    env.learn("address", "--class", "probe-one", "--how", "x", "--date", today)
    env.run("tick", "--no-notify")
    out = env.hook("session", {"session_id": "sess-hub", "source": "compact"}).stdout
    assert "stale-one" not in out and "fresh-one: r-new" in out and "probation" in out
    assert len(out.splitlines()) <= 25


def test_bad_stdin_outside_hub_is_silent_and_writes_nothing(env):
    r = env.run("hook", "prompt", stdin="{not json")
    assert r.returncode == 0 and r.stdout == "" and not (env.state / "hook-errors.jsonl").exists()
    env.bind()
    r = env.run("hook", "prompt", stdin="{not json")
    assert r.returncode == 0 and r.stdout == ""  # unparseable payload cannot be the hub: silent


def test_payload_samples_come_from_real_hook_runs(env):
    env.bind()
    env.hook("prompt", {"hook_event_name": "UserPromptSubmit", "session_id": "sess-hub", "prompt": "hi", "cwd": "/x"})
    s = json.loads((env.state / "payload-samples" / "UserPromptSubmit.json").read_text())
    assert s["keys"]["cwd"] == "str"
    env.hook("prompt", {"hook_event_name": "UserPromptSubmit", "session_id": "other", "prompt": "hi", "zz": 1})
    assert "zz" not in (env.state / "payload-samples" / "UserPromptSubmit.json").read_text()


def test_deadline_parse_and_alert(env):
    env.bind()
    env.prompt("need a release in 10 min, do it now")
    env.run("order", "move", order_ids(env)[0], "--note", "started")
    env.backdate(15)
    out = env.run("tick", "--no-notify").stdout
    assert "past its deadline" in out


# ---- 2. watchdog outside the hooks ----

def test_watchdog_alerts_then_backs_off(env):
    env.bind()
    env.prompt(HOTFIX)
    env.backdate(30)
    first = env.run("tick", "--no-notify").stdout
    assert "no first action" in first and "no movement" in first
    assert env.run("tick", "--no-notify").stdout == ""  # back-off: not again at once
    st = json.loads((env.state / "status.json").read_text())
    assert st["orders"]


def test_movement_resets_stall(env):
    env.bind()
    env.prompt(HOTFIX)
    oid = order_ids(env)[0]
    env.run("order", "move", oid, "--note", "merged")
    env.backdate(7)  # moves are backdated too: 7 min idle, under stall_min=10
    assert "no movement" not in env.run("tick", "--no-notify").stdout


def test_movement_file_counts_as_movement(env, tmp_path):
    f = tmp_path / "run.log"
    intents = (Path(__file__).resolve().parent.parent / "intents.example.toml").read_text()
    intents = intents.replace('movement_files = []         # globs', f'movement_files = ["{f}"]  # globs', 1)
    (env.root / "intents.toml").write_text(intents)
    env.cfg.write_text(env.cfg.read_text().replace(f'intents="{Path(__file__).resolve().parent.parent / "intents.example.toml"}"', f'intents="{env.root / "intents.toml"}"'))
    env.bind()
    env.prompt(HOTFIX)
    env.backdate(30)
    f.write_text("tick")
    out = env.run("tick", "--no-notify").stdout
    assert "no movement" not in out and "no first action" in out


def test_hook_failures_found_by_transcript_scan(env):
    line = {"attachment": {"type": "hook_non_blocking_error", "hookEvent": "UserPromptSubmit",
                           "exitCode": 127, "command": "python3 /x/hubkit hook prompt"},
            "timestamp": "2026-11-04T10:00:00.000Z"}
    other = {"attachment": {"type": "hook_non_blocking_error", "command": "bash other.sh"},
             "timestamp": "2026-11-04T10:00:01.000Z"}
    (env.transcripts / "a.jsonl").write_text(json.dumps(line) + "\n" + json.dumps(other) + "\n")
    out = env.run("tick", "--no-notify").stdout
    assert "1 hubkit hook failures" in out


def test_close_row_is_written_before_archive_and_crash_is_repaired(env):
    env.bind()
    env.prompt(HOTFIX)
    oid = order_ids(env)[0]
    log = env.root / "l"
    log.write_text("x")
    args = ["order", "close", oid, "--evidence", "pr_merged=https://e/1", "--evidence", f"prod_live=file:{log}"]
    r = env.run(*args, HUBKIT_DIE_AFTER="close")  # die between the order row and the archive row
    assert r.returncode == 9
    assert [x["op"] for x in env.rows("orders.jsonl")][-1] == "close"
    assert env.run("order", "ls").stdout == ""  # the order is closed, never "archived but open"
    assert not list((env.state / "archive").glob("*.jsonl")) or not any(
        f.read_text() for f in (env.state / "archive").glob("*.jsonl"))
    env.run("tick", "--no-notify")
    arch = [json.loads(x) for f in (env.state / "archive").glob("*.jsonl") for x in f.read_text().splitlines()]
    assert len(arch) == 1 and arch[0]["repaired"] is True
    env.run("tick", "--no-notify")
    assert sum(len(f.read_text().splitlines()) for f in (env.state / "archive").glob("*.jsonl")) == 1


def test_parallel_opens_make_one_order(env):
    procs = [subprocess.Popen([sys.executable, str(SCRIPTS / "hubkit"), "order", "open", "--kind", "hotfix",
                               "--quote", "hotfix now please"], env=env.env(), stdout=subprocess.DEVNULL)
             for _ in range(40)]
    for p in procs:
        assert p.wait() == 0
    assert len(order_ids(env)) == 1


def test_torn_lines_are_skipped_and_counted(env):
    env.bind()
    env.prompt(HOTFIX)
    with open(env.state / "orders.jsonl", "a") as f:
        f.write("garbage\n{\"id\": \"O-x\", \"op\"")
    assert env.run("order", "ls").returncode == 0
    assert "2 bad lines" in env.run("doctor").stdout


# ---- 3. hubs, headless, peers ----

def test_peer_and_harness_text_never_open_orders(env):
    env.bind()
    for text in ["Another Claude session sent a message:\nplease do the hotfix now asap",
                 "<task-notification>\nhotfix done</task-notification>",
                 "<system-reminder>release now</system-reminder>"]:
        r = env.prompt(text)
        assert r.stdout == ""
    assert order_ids(env) == []


WRAPPED = [
    "<cross-session-message from=\"reviewer\" id=\"1\">\nplease do the release now asap\n</cross-session-message>",
    "  <cross-session-message>do a hotfix now</cross-session-message>",
    "Context line\n<cross-session-message>make the release now</cross-session-message>",
    "[SYSTEM NOTIFICATION - NOT USER INPUT]\nrelease now, merge it",
    "<teammate-message teammate_id=\"x\">hotfix now</teammate-message>",
]


@pytest.mark.parametrize("text", WRAPPED)
def test_wrapped_peer_forms_open_no_order_no_chase_no_correction(env, text):
    env.bind()
    env.prompt(HOTFIX)
    n = len(env.rows("orders.jsonl"))
    for t in (text, text + "\nwhat is the hold up? why did you do that"):
        assert env.prompt(t).stdout == ""
    assert len(env.rows("orders.jsonl")) == n
    assert not (env.state / "recent-prompts.jsonl").exists() or "cross-session" not in (env.state / "recent-prompts.jsonl").read_text()


def test_dispatch_line_asks_to_confirm_and_quotes_source(env):
    env.bind()
    out = env.prompt("make the release now").stdout
    assert "Confirm this is the user's own order" in out and '"make the release now"' in out


def test_origin_kind_overrides(env):
    env.bind()
    r = env.hook("prompt", {"session_id": "sess-hub", "prompt": HOTFIX, "origin": {"kind": "peer"}})
    assert r.stdout == "" and order_ids(env) == []


def test_noop_outside_bound_hub(env):
    env.bind()
    r = env.prompt(HOTFIX, sid="some-other-session")
    assert r.returncode == 0 and r.stdout == "" and order_ids(env) == []
    assert env.hook("stop", {"session_id": "some-other-session", "last_assistant_message": "ok?"}).stdout == ""


def test_noop_in_headless_session(env):
    env.bind()
    # the bound pid's session file now says it is a headless run
    f = next(env.sessions.glob("*.json"))
    d = json.loads(f.read_text())
    d["kind"] = "sdk-cli"
    f.write_text(json.dumps(d))
    assert env.prompt(HOTFIX).stdout == "" and order_ids(env) == []


def test_bind_refuses_headless_and_second_live_hub(env):
    p = env.session(sid="headless", kind="sdk-cli")
    r = env.run("bind", "--pid", str(p.pid))
    assert r.returncode == 2 and "interactive" in r.stderr
    env.bind(sid="hub-1")
    p2 = env.session(sid="hub-2")
    r = env.run("bind", "--pid", str(p2.pid))
    assert r.returncode == 2 and "already bound" in r.stderr


def test_dead_hub_frees_bind_and_session_start_reports_open_orders(env):
    p = env.bind()
    env.prompt(HOTFIX)
    p.kill()
    p.wait()
    # old hub is dead: its prompt hook no longer runs; a fresh interactive session sees the orders
    assert env.prompt("anything", sid="sess-hub").stdout == ""
    fresh = env.session(sid="fresh")
    r = env.hook("session", {"session_id": "fresh", "source": "startup"})
    assert r.stdout == "" and r.returncode == 0  # not the hub: nothing printed
    again = env.run("bind", "--pid", str(fresh.pid))
    assert again.returncode == 0


def test_session_start_compact_block(env):
    env.bind()
    env.prompt(HOTFIX)
    env.run("work", "add", "--kind", "priority", "--title", "ship the thing")
    env.run("work", "add", "--kind", "active", "--title", "review lane")
    env.run("tick", "--no-notify")
    env.learn("add", "--class", "demo-class", "--quote", "why did you", "--situation", "s", "--wrong", "w",
              "--right", "do it right", "--evidence", "e")
    env.run("tick", "--no-notify")
    r = env.hook("session", {"session_id": "sess-hub", "source": "compact"})
    out = r.stdout
    assert "priority: W1 ship the thing" in out and "active: W2 review lane" in out
    assert "open orders: 1" in out and "kind" not in "" and "hotfix" in out
    assert "demo-class: do it right" in out
    assert len(out.splitlines()) < 30
    # stale status is labelled
    st = env.state / "status.json"
    d = json.loads(st.read_text())
    d["built"] -= 3600
    st.write_text(json.dumps(d))
    assert "UNVERIFIED, stale" in env.hook("session", {"session_id": "sess-hub", "source": "compact"}).stdout


def test_session_start_noop_for_other_sessions_with_no_orders(env):
    env.session(sid="other")
    assert env.hook("session", {"session_id": "other", "source": "startup"}).stdout == ""


# ---- 4. failure posture ----

def test_hook_crash_exits_zero_logs_and_shows_one_line(env):
    env.bind()
    (env.root / "bad.toml").write_text("this is [not toml")
    env.cfg.write_text(env.cfg.read_text().replace(f'intents="{Path(__file__).resolve().parent.parent / "intents.example.toml"}"', f'intents="{env.root / "bad.toml"}"'))
    r = env.prompt(HOTFIX)
    assert r.returncode == 0
    assert "hubkit: hook prompt failed" in r.stdout
    assert env.rows("hook-errors.jsonl")


def test_state_inside_git_repo_is_refused(env, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    r = env.run("doctor", HUBKIT_STATE=str(repo / "st"))
    assert r.returncode == 2 and "inside a git repository" in r.stderr


def test_record_hook_keeps_keys_not_values(env):
    env.bind()
    env.hook("record", {"hook_event_name": "SessionStart", "session_id": "sess-hub", "source": "compact", "secret": "VALUE123"})
    f = env.state / "payload-samples" / "SessionStart-compact.json"
    assert "source" in f.read_text() and "VALUE123" not in f.read_text()


# ---- 5. secrets ----

@pytest.mark.parametrize("token", TOKEN_SHAPES)
def test_planted_secret_never_reaches_state(env, token):
    env.bind()
    env.prompt(f"need a hotfix now, use {token} for the deploy")
    env.run("tick", "--no-notify")
    env.run("eod")
    env.run("work", "add", "--title", f"thing {token}")
    env.learn("add", "--class", "sec-class", "--quote", f"quote {token}", "--situation", f"s {token}",
              "--wrong", "w", "--right", "r", "--evidence", "e")
    tail = token.split(" ")[-1].split("://")[-1].split(":")[-1].split("=")[-1][-10:]
    for f in list(env.state.rglob("*")) + [env.root / "learnings.md"]:
        if f.is_file():
            assert tail not in f.read_text(errors="replace"), f"{tail} leaked into {f.name}"
    out = env.hook("session", {"session_id": "sess-hub", "source": "compact"}).stdout
    assert tail not in out


def test_redaction_corpus_count_only(env):
    from hubcore.redact import count_hits
    clean = ["the release branch is cut", "fix the PR https://github.com/o/r/pull/123", "ssh to the build host and check logs",
             "path /home/user/.claude/skills/hub/scripts/hubkit", "O-261104-ab12 hotfix"]
    assert sum(count_hits(c) for c in clean) == 0
    assert all(count_hits(t) > 0 for t in TOKEN_SHAPES)


# ---- 6. status file, archive, EOD, learnings ----

def test_work_done_moves_item_to_archive_day(env):
    env.run("work", "add", "--kind", "tracked", "--title", "watch PR")
    env.run("work", "add", "--kind", "active", "--title", "write brief")
    r = env.run("work", "done", "W1", "--note", "merged")
    assert "archived to archive_" in r.stdout
    live = json.loads((env.state / "work.json").read_text())
    assert [i["id"] for i in live] == ["W2"]
    arch = next((env.state / "archive").glob("archive_*.jsonl"))
    row = json.loads(arch.read_text().splitlines()[0])
    assert row["type"] == "work" and row["title"] == "watch PR" and row["outcome"] == "done"
    assert env.run("work", "done", "W1").returncode == 2


def test_eod_glance_reflection_and_learning_comparison(env):
    env.bind()
    env.prompt(HOTFIX)
    env.prompt("what is the hold up?")
    oid = order_ids(env)[0]
    env.run("order", "cancel", oid, "--quote", "drop it")
    env.run("work", "add", "--title", "x")
    env.run("work", "done", "W1")
    today = time.strftime("%Y-%m-%d", time.gmtime())
    env.learn("add", "--class", "old-class", "--quote", "q1", "--date", "2026-10-01", "--situation", "s", "--wrong", "w", "--right", "r", "--evidence", "e")
    env.learn("add", "--class", "old-class", "--quote", "q2", "--date", "2026-10-02")
    env.learn("add", "--class", "fixed-class", "--quote", "q1", "--date", "2026-10-01", "--situation", "s", "--wrong", "w", "--right", "r", "--evidence", "e")
    (env.root / "overlay.md").write_text("rule G1 promoted\n")
    env.learn("promote", "fixed-class", "--gate", "G1", "--rule", "fixed rule")
    assert env.learn("address", "--class", "fixed-class", "--how", "promoted to G1", "--date", "2026-10-03").returncode == 0
    env.learn("add", "--class", "fixed-class", "--quote", "q9", "--date", today)
    env.learn("add", "--class", "new-class", "--quote", "q3", "--date", today, "--situation", "s", "--wrong", "w", "--right", "r", "--evidence", "e")
    r = env.run("eod", "--note", "route earlier")
    out = r.stdout
    assert "orders opened 1, closed 0, cancelled 1" in out
    assert "chases 1 (1.0 per order)" in out
    assert "work items finished 1" in out
    assert "new today: new-class" in out
    assert "addressed but repeated (not fixed): fixed-class" in out
    assert "seen 2+ times, not addressed (promote or fix): old-class (2x)" in out
    assert "chased 1x" in out and "route earlier" in out
    assert (env.state / "eod" / f"eod_{today}.md").exists()


def test_learn_concurrent_adds_lose_nothing(env):
    procs = [subprocess.Popen([sys.executable, str(SCRIPTS / "learn.py"),
                               "add", "--class", f"c{i}", "--quote", f"q{i}", "--situation", "s", "--wrong", "w",
                               "--right", "r", "--evidence", "e"], env=env.env(), stdout=subprocess.DEVNULL)
             for i in range(40)]
    for p in procs:
        assert p.wait() == 0
    text = (env.root / "learnings.md").read_text()
    assert all(f"## c{i} |" in text for i in range(40))


# ---- 7. adapters, config, extension ----

def test_mep_absent_is_unverified_not_empty(env):
    env.cfg.write_text(env.cfg.read_text().replace("enabled=false", 'enabled=true\n[tools]\nmep="/nonexistent/mep"'))
    env.run("tick", "--no-notify")
    st = json.loads((env.state / "status.json").read_text())
    assert st["mep"]["state"] == "UNVERIFIED"


def test_mep_real_binary_if_present(env):
    mep = shutil.which("mep")
    if not mep:
        pytest.skip("no mep on PATH")
    env.cfg.write_text(env.cfg.read_text().replace("enabled=false", f'enabled=true\n[tools]\nmep="{mep}"'))
    st = json.loads(env.run("status", "--json").stdout)
    assert st["mep"]["state"] in ("ok", "UNVERIFIED")


def test_extension_points(env):
    ext = env.root / "ext"
    ext.mkdir()
    (ext / "mine.py").write_text(
        "def status_lines(cfg):\n    return ['ext line for ' + cfg['user']['name']]\n"
        "def tick_checks(cfg, orders):\n    return [{'key': 'ext:1', 'msg': 'custom alert'}]\n")
    out = env.run("tick", "--no-notify").stdout
    assert "custom alert" in out
    assert "ext line for Tester" in env.run("status").stdout


def test_user_specific_values_come_from_config(env):
    assert "Tester" in env.run("status").stdout and "the user" not in env.run("status").stdout
    env.cfg.write_text(env.cfg.read_text().replace("Tester", "Someone Else"))
    assert "Someone Else" in env.run("status").stdout


def test_check_clean_flags_a_term_and_passes_when_absent(env):
    absent = "zz" + "qqxx"
    env.cfg.write_text(env.cfg.read_text() + f'[private]\nterms=["{absent}"]\n')
    assert "none [1 terms]" in env.run("doctor", "--check-clean").stdout
    env.cfg.write_text(env.cfg.read_text().replace(absent, "research"))  # a word the skill really contains
    r = env.run("doctor", "--check-clean")
    assert r.returncode == 1 and "tests/test_hubkit.py" in r.stdout and "references/" in r.stdout


def test_skill_md_under_200_lines():
    assert len((Path(__file__).resolve().parent.parent / "SKILL.md").read_text().splitlines()) < 200


# ---- 8. labelled corpus: is_human against harness ground truth ----

def test_is_human_against_real_labelled_transcripts():
    from hubcore.store import is_human
    files = sorted(Path.home().glob(".claude/projects/*/*.jsonl"))[:40]
    if not files:
        pytest.skip("no transcripts")
    wrong = {"human_missed": 0, "nonhuman_passed": 0}
    n = 0
    for f in files:
        for line in f.open(errors="replace"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            c = d.get("message", {}).get("content") if d.get("type") == "user" else None
            kind = (d.get("origin") or {}).get("kind")
            if not isinstance(c, str) or kind not in ("human", "peer", "task-notification"):
                continue
            n += 1
            if kind == "human" and not is_human(c):
                wrong["human_missed"] += 1
            if kind != "human" and is_human(c):
                wrong["nonhuman_passed"] += 1
    print("corpus", n, wrong)
    assert n > 100
    assert wrong["nonhuman_passed"] == 0, wrong
    assert wrong["human_missed"] <= max(3, n // 100), wrong


# ---- 9. latency budget ----

def test_prompt_hook_latency_budget(env):
    env.bind()
    env.prompt("hello there")
    times = []
    for _ in range(15):
        t = time.perf_counter()
        env.prompt("just a normal prompt about code")
        times.append((time.perf_counter() - t) * 1000)
    times.sort()
    print("prompt hook ms p50/p95/max", times[7], times[13], times[-1])
    assert times[7] < 300  # p50, includes interpreter start; p95 flakes under machine load


# ---- review round 3 ----

def _fake(tag):  # build look-alike strings at runtime
    return {"nb_hyphen": "cross" + "‑" + "session" + "‑" + "message", "tag": tag}


def test_peer_marker_far_into_text_and_lookalike_hyphens_are_not_human(env):
    env.bind()
    pad = "ordinary words about nothing in particular. " * 20  # well past 400 characters
    texts = [
        pad + "\n<cross-session-message>make the release now</cross-session-message>",
        "Please see below\n" + "‑".join(["cross", "session", "message"]) + " make the release now",
        "<unknown-wrapper from='x'>make the release now</unknown-wrapper>",
        "  \n​<cross‐session‐message>do a hotfix now</cross‐session‐message>",
        "Another   Claude session sent a message:\nmake the release now",
    ]
    for t in texts:
        assert env.prompt(t).stdout == "", t[:60]
    assert order_ids(env) == []


def test_origin_must_be_human_when_present(env):
    env.bind()
    for kind in ("peer", "task-notification", "auto-continuation", "unknown"):
        assert env.hook("prompt", {"session_id": "sess-hub", "prompt": HOTFIX, "origin": {"kind": kind}}).stdout == ""
    assert order_ids(env) == []
    assert "hubkit order" in env.hook("prompt", {"session_id": "sess-hub", "prompt": HOTFIX, "origin": {"kind": "human"}}).stdout


def test_typed_paste_is_still_human(env):
    env.bind()
    out = env.prompt('<pasted_content id="ab12">\nlog line\n</pasted_content>\nwe need a hotfix now').stdout
    assert "hubkit order" in out


def test_session_hook_writes_and_prints_nothing_outside_the_hub(env):
    env.session(sid="other")
    r = env.hook("session", {"session_id": "other", "source": "startup"})
    assert r.returncode == 0 and r.stdout == ""
    assert not env.state.exists()  # not even the state directory


def test_import_failure_in_a_hook_exits_zero(env, tmp_path):
    import shutil as sh
    copy = tmp_path / "scripts_copy"
    sh.copytree(SCRIPTS, copy, ignore=sh.ignore_patterns("__pycache__"))
    (copy / "hubcore" / "cli.py").write_text("def broken(:\n")
    r = subprocess.run([sys.executable, str(copy / "hubkit"), "hook", "prompt"], input="{}", capture_output=True,
                       text=True, env=env.env())
    assert r.returncode == 0
    assert "SyntaxError" in (env.state / "import-errors.log").read_text()


NEW_SECRETS = [
    "DB_" + "PASSWORD=abc12345", "MY_SECRET_" + "KEY=zz99aa", "sk-" + "ant-api03-abcdefghijklmn",
    "glp" + "at-abcdefghij1234567890", "AI" + "zaSyAbcdefghijklmnopqrstuvwxyzabcdefg",
    "sk-" + "proj_abcdefghijklmnopqrstuvwx", "ACCESS_" + "TOKEN: abcdefgh",
]


@pytest.mark.parametrize("secret", NEW_SECRETS)
def test_more_secret_shapes_are_redacted(secret):
    from hubcore.redact import redact
    out, hits = redact(f"deploy with {secret} today")
    assert hits >= 1 and secret.split("=")[-1].split(":")[-1].strip()[-8:] not in out


def test_status_json_is_redacted_at_write(env):
    ext = env.root / "ext"
    ext.mkdir()
    secret = "glp" + "at-abcdefghij1234567890"
    (ext / "leak.py").write_text(f"def status_lines(cfg):\n    return ['line with {secret}']\n")
    env.run("tick", "--no-notify")
    assert secret not in (env.state / "status.json").read_text()


def test_cut_secret_does_not_leak_a_prefix(env):
    env.bind()
    secret = "glp" + "at-" + "A1b2C3d4E5f6G7h8I9j0"
    pad = "hotfix now " + "x" * 279 + " "  # the secret straddles the 300 character cut
    env.prompt(pad + secret + " tail")
    rows = (env.state / "orders.jsonl").read_text()
    assert "glp" + "at-A1b2" not in rows and "glp" + "at" not in rows.replace("[REDACTED", "")


def test_mep_found_with_cron_style_path(env):
    real = shutil.which("mep")
    if not real:
        pytest.skip("no mep installed")
    env.cfg.write_text(env.cfg.read_text().replace("enabled=false", "enabled=true"))
    bindir = str(Path(real).parent)
    minimal = os.pathsep.join(p for p in ("/usr/bin", "/bin") if p != bindir)
    r = env.run("tick", "--no-notify", PATH=minimal)
    assert r.returncode == 0
    st = json.loads((env.state / "status.json").read_text())
    assert st["mep"]["state"] == "ok", st["mep"]


def test_slow_mep_keeps_last_good_answer_not_unverified(env):
    if not shutil.which("mep"):
        pytest.skip("no mep installed")
    env.cfg.write_text(env.cfg.read_text().replace("enabled=false", "enabled=true"))
    env.run("tick", "--no-notify")
    assert json.loads((env.state / "status.json").read_text())["mep"]["state"] == "ok"
    env.cfg.write_text(env.cfg.read_text().replace("enabled=true", "enabled=true\ntimeout_s=0.05"))
    env.run("tick", "--no-notify")
    m = json.loads((env.state / "status.json").read_text())["mep"]
    assert m["state"] == "stale" and m["why"] == "TimeoutExpired" and m["count"] >= 0


def test_default_mep_timeout_covers_a_real_status_call():
    from hubcore import config
    assert config.DEFAULTS["mep"]["timeout_s"] >= 20


def test_address_refused_when_tagged_rule_already_existed_unchanged(env):
    ov = env.root / "overlay.md"
    ov.write_text("# rules\n- old rule [route-it]\n")
    env.learn("add", "--class", "route-it", "--gate", "G1", "--quote", "q", "--situation", "s", "--wrong", "w", "--right", "r", "--evidence", "e")
    r = env.learn("address", "--class", "route-it", "--how", "claimed fix, nothing changed")
    assert r.returncode == 2 and "unchanged" in r.stderr


def test_append_row_redacts_even_when_the_caller_did_not(tmp_path):
    from hubcore import store
    secret = "glp" + "at-abcdefghij1234567890"
    f = tmp_path / "rows.jsonl"
    store.append_row(f, {"op": "x", "text": f"raw {secret}"})
    assert secret not in f.read_text() and "glp" + "at-abcd" not in f.read_text()


def test_order_open_waits_for_a_held_lock_instead_of_skipping_it(env):
    import fcntl
    env.run("order", "ls")  # makes the state dir
    fd = os.open(env.state / "lock", os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    p = subprocess.Popen([sys.executable, str(SCRIPTS / "hubkit"), "order", "open", "--kind", "hotfix",
                          "--quote", "hotfix now please"], env=env.env(), stdout=subprocess.DEVNULL)
    time.sleep(1.0)
    assert p.poll() is None, "open ran while another process held the lock"
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    assert p.wait(timeout=20) == 0 and len(order_ids(env)) == 1


def test_glued_secrets_are_redacted():
    from hubcore.redact import redact
    for s in ("token" + "glpat-abcdefghij1234567890", "key=x" + "AIza" + "SyAbcdefghijklmnopqrstuvwxyzabcdefg", "xAI" + "zaSyAbcdefghijklmnopqrstuvwxyzabcdefg"):
        out, hits = redact("see " + s)
        assert hits >= 1 and "abcdefghij" not in out, s


def test_import_error_log_is_redacted_and_survives_a_broken_redactor(env, tmp_path):
    import shutil as sh
    secret = "glp" + "at-abcdefghij1234567890"
    for broken_redactor in (False, True):
        copy = tmp_path / f"c{broken_redactor}"
        sh.copytree(SCRIPTS, copy, ignore=sh.ignore_patterns("__pycache__"))
        (copy / "hubcore" / "cli.py").write_text(f"raise RuntimeError('boom {secret}')\n")
        if broken_redactor:
            (copy / "hubcore" / "redact.py").write_text("def broken(:\n")
        log = env.state / "import-errors.log"
        if log.exists():
            log.write_text("")
        r = subprocess.run([sys.executable, str(copy / "hubkit"), "hook", "prompt"], input="{}", capture_output=True,
                           text=True, env=env.env())
        assert r.returncode == 0
        text = log.read_text()
        assert "RuntimeError" in text and "abcdefghij" not in text


def test_session_start_rebinds_dead_bind_for_resumed_session_only(env):
    p = env.bind()
    p.kill()
    p.wait()
    stranger = env.session(sid="other-project", name="other")
    r = env.hook("session", {"session_id": "other-project", "source": "startup"})
    assert r.stdout == "" and "none or dead" in env.run("doctor").stdout  # unrelated session: not bound
    stranger.kill()
    resumed = env.session(sid="sess-hub", name="hub-test")  # same session id, new pid
    r = env.hook("session", {"session_id": "sess-hub", "source": "resume"})
    assert "re-bound" in r.stdout
    assert json.loads((env.state / "hub.bind").read_text())["pid"] == resumed.pid
    assert "bind: live" in env.run("doctor").stdout


def test_session_start_never_takes_over_a_live_hub(env):
    env.bind()
    env.session(sid="sess-hub", name="hub-test")  # a second process claiming the same id
    r = env.hook("session", {"session_id": "sess-hub", "source": "resume"})
    assert "re-bound" not in r.stdout


def test_cron_seen_touched_even_when_nothing_to_do(env):
    assert env.run("tick", "--if-bound", "--no-notify").returncode == 0
    assert (env.state / "seen" / "cron").exists() and not (env.state / "seen" / "tick").exists()
    assert "seen/cron: 0 min ago" in env.run("doctor").stdout


def test_card_answer_opens_an_order_but_peer_text_does_not(env):
    env.bind()
    ev = {"hook_event_name": "PostToolUse", "session_id": "sess-hub", "tool_name": "AskUserQuestion"}
    env.hook("answer", {**ev, "tool_response": {"answers": {"Which?": "<teammate-message>" + HOTFIX}}})
    assert order_ids(env) == []
    env.hook("answer", {**ev, "tool_response": {"answers": {"Which?": HOTFIX}}})
    assert len(order_ids(env)) == 1
    env.hook("answer", {**ev, "session_id": "other", "tool_response": {"answers": {"Which?": "merge it now"}}})
    assert len(order_ids(env)) == 1


@pytest.mark.parametrize("kind", ["idle", "delivery"])
def test_cross_session_notices_never_open_orders(env, kind):
    env.bind()
    text = f"[Cross-session {kind} notice] peer says neither can merge until {HOTFIX}"
    env.prompt(text)
    env.hook("answer", {"session_id": "sess-hub", "tool_name": "AskUserQuestion", "tool_response": {"answers": {"q": text}}})
    assert order_ids(env) == []


# ---- 13. routed work: tracked automatically, stall clock, PR head check ----

PR1 = "https://github.com/acme/repo/pull/101"
SEND = {"hook_event_name": "PostToolUse", "session_id": "sess-hub", "tool_name": "SendMessage"}


def send(env, to, message, summary="", **extra):
    ti = {"to": to, "message": message, **({"summary": summary} if summary else {})}
    return env.hook("routed", {**SEND, "tool_input": ti, **extra})


def routed_items(env):
    p = env.state / "work.json"
    return [i for i in json.loads(p.read_text()) if i["kind"] == "routed"] if p.exists() else []


def age_routed(env, minutes):
    """Shift every routed timestamp into the past (a real file edit)."""
    p = env.state / "work.json"
    items = json.loads(p.read_text())
    for i in items:
        for k in ("routed_at", "added", "last_heard"):
            if i.get(k):
                i[k] -= minutes * 60
    p.write_text(json.dumps(items))


def fake_gh(env, head="aaa1111", review_at=None, sleep=0):
    """A local stand-in for the gh binary (the tests cannot read a real PR); logs each call."""
    gh = env.root / "gh"
    log, data = env.root / "gh.log", env.root / "gh.json"
    gh.write_text(f'#!/bin/sh\necho call >> {log}\n[ -f {data} ] || exit 1\nsleep {sleep}\ncat {data}\n')
    gh.chmod(0o755)
    env.cfg.write_text(env.cfg.read_text() + f'[tools]\ngh="{gh}"\n')
    return set_gh(env, head, review_at)


def set_gh(env, head, review_at=None):
    reviews = [{"submittedAt": review_at}] if review_at else []
    (env.root / "gh.json").write_text(json.dumps({"headRefOid": head, "reviews": reviews}))
    return env.root / "gh.log"


def gh_calls(env):
    p = env.root / "gh.log"
    return len(p.read_text().splitlines()) if p.exists() else 0


def test_send_message_with_pr_opens_one_routed_item_and_refreshes_it(env):
    env.bind()
    r = send(env, "peer-a", f"Please re-check {PR1} at the new head.", "re-check 101")
    assert r.returncode == 0 and "tracking W1" in r.stdout
    (item,) = routed_items(env)
    assert item["owner"] == "peer-a" and item["prs"] == [PR1] and item["title"] == "re-check 101"
    first = item["routed_at"]
    time.sleep(0.05)
    send(env, "peer-a", f"Again: {PR1} please, new head.", "re-check 101 again")
    (item,) = routed_items(env)
    assert item["routed_at"] == first and item["chases"] == 1  # same owner + PR: a follow-up, counted, same clock
    send(env, "peer-a", f"Re-brief: {PR1} on the new head. Done-when: reviewed", "re-check 101 brief")
    (item,) = routed_items(env)
    assert item["routed_at"] > first and item["title"] == "re-check 101 brief"  # a new brief is a new ask
    send(env, "peer-b", f"Also {PR1}", "b")
    assert len(routed_items(env)) == 2  # another owner: another item


def test_acks_inbox_watcher_and_other_sessions_are_not_tracked(env):
    env.bind()
    send(env, "peer-a", "ok thanks, got it")
    send(env, "peer-a", "ack: " + "x" * 300)  # an acknowledgement, even a long one
    send(env, "inbox-watcher", f"check {PR1} and tell me")
    send(env, "peer-a", "one short line without any PR")
    env.hook("routed", {**SEND, "session_id": "other", "tool_input": {"to": "peer-a", "message": f"check {PR1}"}})
    env.hook("routed", {**SEND, "tool_name": "Read", "tool_input": {"to": "peer-a", "message": f"check {PR1}"}})
    assert routed_items(env) == []
    send(env, "peer-a", "Please investigate the flaky test and report. " + "detail " * 40)  # long, no PR
    assert len(routed_items(env)) == 1


def test_background_agent_is_tracked_foreground_agent_is_not(env):
    env.bind()
    base = {"hook_event_name": "PostToolUse", "session_id": "sess-hub", "tool_name": "Agent", "tool_use_id": "toolu_X1"}
    env.hook("routed", {**base, "tool_input": {"description": "fix it", "prompt": f"fix {PR1}"}})
    assert routed_items(env) == []
    env.hook("routed", {**base, "tool_input": {"description": "fix it", "prompt": f"fix {PR1}", "run_in_background": True},
                        "tool_response": {"agentId": "agent123", "status": "async_launched"}})
    (item,) = routed_items(env)
    assert item["owner"] == "agent123" and item["tool_use_id"] == "toolu_X1"


def peer_line(name, ts):
    return json.dumps({"type": "user", "origin": {"kind": "peer", "from": "uds:x"}, "timestamp": ts,
                       "message": {"content": f'Another Claude session sent a message:\n<cross-session-message from="uds:x" from-name="{name}" from-mode="bypass">\ndone'}})


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


def test_message_from_the_owner_updates_last_heard_and_opens_nothing(env):
    env.bind()
    send(env, "peer-a", f"check {PR1}")
    send(env, "peer-b", f"check {PR1}")
    age_routed(env, 40)
    tr = env.transcripts / "sess-hub.jsonl"
    tr.write_text(peer_line("peer-a", iso(time.time() - 60)) + "\n")
    r = env.hook("prompt", {"session_id": "sess-hub", "prompt": "carry on please", "transcript_path": str(tr)})
    heard = {i["owner"]: i.get("last_heard") for i in routed_items(env)}
    assert heard["peer-a"] and not heard["peer-b"]
    assert "STALLED" in r.stdout and "peer-b" in r.stdout and "peer-a" not in r.stdout
    # the tick finds the same message through the bound hub's transcript, and a notification by tool use id
    age_routed(env, 40)
    tr.write_text(tr.read_text() + json.dumps({"type": "user", "origin": {"kind": "task-notification"}, "timestamp": iso(time.time()),
                                               "message": {"content": "<task-notification><task-id>peer-b</task-id></task-notification>"}}) + "\n")
    env.run("tick", "--no-notify")
    assert all(i["last_heard"] for i in routed_items(env))
    assert order_ids(env) == []


def test_prompt_hook_peer_text_marks_heard_but_opens_no_order(env):
    env.bind()
    send(env, "peer-a", f"check {PR1}")
    age_routed(env, 40)
    env.prompt(f'<cross-session-message from="uds:x" from-name="peer-a">{HOTFIX}</cross-session-message>')
    assert routed_items(env)[0]["last_heard"] and order_ids(env) == []


def test_silent_owner_is_stalled_at_the_prompt_in_tick_and_at_turn_end(env):
    env.bind()
    env.prompt(HOTFIX)  # a live order, so the prompt output has other lines to sit below
    send(env, "peer-a", f"check {PR1}", "check 101")
    assert "STALLED" not in env.prompt("carry on").stdout  # fresh
    age_routed(env, 20)
    out = env.prompt("carry on").stdout
    assert out.splitlines()[0].startswith("hubkit STALLED:") and "peer-a" in out.splitlines()[0]
    assert "SendMessage peer-a" in out and "#101" in out and "20 min" in out
    assert "hubkit STALLED: peer-a" in json.loads(env.hook("stop", {"session_id": "sess-hub", "last_assistant_message": "done"}).stdout)["reason"]
    first = env.run("tick", "--no-notify").stdout
    assert "ALERT" in first and "peer-a" in first and "one-line state" in first
    assert env.run("tick", "--no-notify").stdout == ""  # back-off, not at once
    assert "NEXT: SendMessage peer-a for a one-line state" in env.run("status").stdout


def test_stall_limit_comes_from_config(env):
    env.cfg.write_text(env.cfg.read_text() + "[routed]\nstall_min = 60\n")
    env.bind()
    send(env, "peer-a", f"check {PR1}")
    age_routed(env, 20)
    assert "STALLED" not in env.prompt("carry on").stdout and env.run("tick", "--no-notify").stdout == ""
    age_routed(env, 50)
    assert "STALLED" in env.prompt("carry on").stdout


def test_stalled_item_reaches_notify_cmd(env, tmp_path):
    note = tmp_path / "note.txt"
    sh = tmp_path / "notify.sh"
    sh.write_text(f'#!/bin/sh\necho "$@" >> {note}\n')
    sh.chmod(0o755)
    env.cfg.write_text(env.cfg.read_text() + f'[tools]\nnotify_cmd="{sh}"\n')
    env.bind()
    send(env, "peer-a", f"check {PR1}")
    age_routed(env, 30)
    env.run("tick")
    assert "peer-a" in note.read_text() and "nothing for 30 min" in note.read_text()


def test_moved_head_alerts_and_the_pr_is_read_once_per_tick(env):
    fake_gh(env, head="aaa1111")
    env.bind()
    send(env, "peer-a", f"check {PR1}")
    send(env, "peer-b", f"also {PR1}")
    assert routed_items(env)[0]["heads"][PR1] == "aaa1111"  # baseline taken when the work was routed
    assert env.run("tick", "--no-notify").stdout == ""
    set_gh(env, "bbb2222")
    before = gh_calls(env)
    out = env.run("tick", "--no-notify").stdout
    assert out.count("ALERT") == 2  # one alert per item
    assert gh_calls(env) - before == 1  # one gh read for the PR, two items
    p = env.prompt("carry on").stdout
    assert p.splitlines()[0].startswith("hubkit STALLED:") and "head moved" in p and "SendMessage" in p
    send(env, "peer-a", f"check {PR1} at bbb2222")  # the hub acted: baseline reset, flag cleared
    assert [i["head_moved"] for i in routed_items(env) if i["owner"] == "peer-a"] == [None]


def test_gh_failure_or_timeout_is_unknown_and_never_crashes_the_tick(env):
    env.cfg.write_text(env.cfg.read_text() + "[routed]\ngh_timeout_s = 1\n")
    fake_gh(env)
    env.bind()
    send(env, "peer-a", f"check {PR1}")
    (env.root / "gh.json").unlink()  # gh now fails
    r = env.run("tick", "--no-notify")
    assert r.returncode == 0 and "head moved" not in r.stdout
    assert "PR unknown" in env.run("status").stdout
    set_gh(env, "aaa1111")
    env.run("tick", "--no-notify")
    assert "PR unknown" not in env.run("status").stdout  # gh answers again: known
    gh = env.root / "gh"
    gh.write_text(gh.read_text().replace("sleep 0", "sleep 5"))  # gh hangs
    t0 = time.time()
    r = env.run("tick", "--no-notify")
    assert r.returncode == 0 and time.time() - t0 < 4 and "PR unknown" in env.run("status").stdout


def test_review_after_routing_counts_as_movement(env):
    fake_gh(env)
    env.bind()
    send(env, "peer-a", f"check {PR1}")
    age_routed(env, 30)
    assert "nothing for" in env.run("tick", "--no-notify").stdout
    set_gh(env, "aaa1111", review_at=iso(time.time() - 60))
    env.run("tick", "--no-notify")
    assert routed_items(env)[0]["last_heard"] and "STALLED" not in env.prompt("carry on").stdout


def test_watch_wakes_the_hub_on_a_stall_without_any_prompt(env):
    env.bind()
    send(env, "peer-a", f"check {PR1}")
    age_routed(env, 30)
    r = env.run("watch", "--interval", "1", "--max-cycles", "3", timeout=30)
    assert r.returncode == 0 and "ALERT" in r.stdout and "peer-a" in r.stdout
    env.run("work", "done", "W1", "--note", "x")
    r = env.run("watch", "--interval", "1", "--max-cycles", "2", timeout=30)
    assert r.returncode == 3 and r.stdout == ""


def test_work_done_closes_a_routed_item_and_status_lists_it(env):
    env.bind()
    send(env, "peer-a", f"check {PR1}", "check 101")
    out = env.run("status").stdout
    assert "routed work" in out and "W1 -> peer-a" in out and "age 0m" in out and "last heard never" in out
    assert env.run("work", "done", "W1", "--note", "delivered").returncode == 0
    assert routed_items(env) == [] and "W1 -> peer-a" not in env.run("status").stdout
    row = json.loads(next((env.state / "archive").glob("archive_*.jsonl")).read_text().splitlines()[0])
    assert row["kind"] == "routed" and row["done_note"] == "delivered"


def test_work_add_routed_by_hand_dedupes_with_the_hook(env):
    r = env.run("work", "add", "--kind", "routed", "--owner", "peer-a", "--title", "check 101", "--pr", PR1, "--head", "abc1234")
    assert r.returncode == 0 and routed_items(env)[0]["heads"] == {PR1: "abc1234"}
    env.bind()
    send(env, "peer-a", f"again {PR1}")
    assert len(routed_items(env)) == 1


def test_routed_hook_is_exit_zero_with_garbage(env):
    env.bind()
    for bad in ("not json", "[]", json.dumps({**SEND, "tool_input": "x"})):
        r = env.run("hook", "routed", stdin=bad)
        assert r.returncode == 0
    assert routed_items(env) == []


def test_review_items_chase_at_5_and_are_overdue_at_10_minutes_from_routing(env):
    env.bind()
    send(env, "peer-a", f"Full gated review of {PR1}", "review 101")
    send(env, "peer-b", f"check {PR1}", "check 101")
    age_routed(env, 6)
    out = env.prompt("carry on").stdout
    assert "peer-a" in out and "limit 5" in out and "peer-b" not in out and "OVERDUE" not in out
    age_routed(env, 5)  # 11 min since routing; the owner spoke at minute 3 but that does not stop the hard limit
    p = env.state / "work.json"
    items = json.loads(p.read_text())
    items[0]["last_heard"] = time.time() - 60
    p.write_text(json.dumps(items))
    out = env.prompt("carry on").stdout
    assert out.splitlines()[0].startswith("hubkit OVERDUE:") and "peer-a" in out.splitlines()[0] and "hard limit of 10" in out
    assert "hard limit of 10" in env.run("tick", "--no-notify").stdout


def test_review_clock_values_come_from_config(env):
    env.cfg.write_text(env.cfg.read_text() + "[routed]\nreview_stall_min = 30\nreview_overdue_min = 40\n")
    env.bind()
    send(env, "peer-a", f"Full gated review of {PR1}", "review 101")
    age_routed(env, 12)
    assert "STALLED" not in env.prompt("carry on").stdout and "OVERDUE" not in env.prompt("carry on").stdout


# ---- 14. the outcome ledger, wake-up, NEXT lines, ladder, Stop gate ----

def set_gh_raw(env, head, reviews=None, author="pr-author"):
    (env.root / "gh.json").write_text(json.dumps({"headRefOid": head, "author": {"login": author}, "reviews": reviews or []}))


def review(commit, state="APPROVED", login="rev-er", at=None):
    return {"submittedAt": iso(at or time.time() + 5), "state": state, "commit": {"oid": commit}, "author": {"login": login}}


def queue_line(content, ts):
    return json.dumps({"type": "queue-operation", "operation": "enqueue", "timestamp": ts, "content": content})


def test_peer_message_still_queued_marks_heard(env):
    """A message sent while the hub is busy has only a queue-operation entry until the hub takes it up."""
    env.bind()
    send(env, "peer-a", f"check {PR1}")
    age_routed(env, 40)
    tr = env.transcripts / "sess-hub.jsonl"
    tr.write_text(queue_line('<cross-session-message from="uds:x" from-name="peer-a" from-mode="bypass">\nstep 4 done', iso(time.time() - 30)) + "\n")
    out = env.hook("prompt", {"session_id": "sess-hub", "prompt": "carry on", "transcript_path": str(tr)}).stdout
    assert routed_items(env)[0]["last_heard"] and "STALLED" not in out


def test_agent_completion_still_queued_marks_heard(env):
    env.bind()
    base = {"hook_event_name": "PostToolUse", "session_id": "sess-hub", "tool_name": "Agent", "tool_use_id": "toolu_X1"}
    env.hook("routed", {**base, "tool_input": {"description": "fix it", "prompt": f"fix {PR1}. Output: a PR", "run_in_background": True},
                        "tool_response": {"agentId": "agent123", "status": "async_launched"}})
    age_routed(env, 40)
    tr = env.transcripts / "sess-hub.jsonl"
    tr.write_text(queue_line("<task-notification>\n<task-id>agent123</task-id>\n<status>completed</status>\n</task-notification>", iso(time.time() - 30)) + "\n")
    env.hook("prompt", {"session_id": "sess-hub", "prompt": "carry on", "transcript_path": str(tr)})
    assert routed_items(env)[0]["last_heard"]


def test_watcher_and_deliverable_free_agents_are_not_tracked(env):
    env.bind()
    base = {"hook_event_name": "PostToolUse", "session_id": "sess-hub", "tool_name": "Agent", "tool_use_id": "t", "tool_response": {"agentId": "agent9"}}
    env.hook("routed", {**base, "tool_input": {"description": "Run the inbox watcher", "prompt": "Run mep watch --cursor hub forever. Output: new asks", "run_in_background": True}})
    env.hook("routed", {**base, "tool_input": {"description": "Keep an eye on the box", "prompt": "Watch the disk and tell me if it fills", "run_in_background": True}})
    assert routed_items(env) == []
    env.hook("routed", {**base, "tool_input": {"description": "Fix it", "prompt": "Fix the bug. Done-when: tests pass", "run_in_background": True}})
    assert len(routed_items(env)) == 1


def test_bare_ref_and_full_url_to_the_same_owner_are_one_outcome(env):
    env.bind()
    send(env, "peer-a", "Please check #101 at its head")
    send(env, "peer-a", f"Again, {PR1}")
    send(env, "peer-a", "Do PR 101 now")
    (item,) = routed_items(env)
    assert item["prs"] == [PR1]  # the full URL replaced the bare ref


def test_bare_ref_resolves_against_the_configured_repo_so_gh_runs(env):
    env.cfg.write_text(env.cfg.read_text() + '[routed]\nrepo = "acme/repo"\n')
    fake_gh(env, head="aaa1111")
    env.bind()
    send(env, "peer-a", "Please check #101")
    (item,) = routed_items(env)
    assert item["prs"] == [PR1] and item["heads"] == {PR1: "aaa1111"}
    assert gh_calls(env) == 1


def test_one_owner_gets_one_chase_line_whatever_the_item_count(env):
    env.bind()
    send(env, "peer-a", f"check {PR1}")
    send(env, "peer-a", "check https://github.com/acme/repo/pull/102")
    send(env, "peer-b", "check https://github.com/acme/repo/pull/103")
    age_routed(env, 20)
    lines = [l for l in env.prompt("carry on").stdout.splitlines() if l.startswith("hubkit STALLED")]
    assert len(lines) == 2 and sum("peer-a" in l for l in lines) == 1  # one for peer-a (two items), one for peer-b
    assert "#101" in lines[0] and "#102" in lines[0]


def test_a_chase_is_an_event_not_a_new_clock(env):
    env.bind()
    send(env, "peer-a", f"check {PR1}", "check 101")
    age_routed(env, 20)
    asked = routed_items(env)[0]["routed_at"]
    send(env, "peer-a", "Status on #101 now?", "status")
    (item,) = routed_items(env)
    assert item["routed_at"] == asked and item["chases"] == 1 and item["title"] == "check 101"
    assert "STALLED" not in env.prompt("carry on").stdout  # chased at this step: waiting
    assert "chased 1x, waiting" in env.run("status").stdout
    age_routed(env, 20)  # 40 min of silence: the next ladder step
    out = env.prompt("carry on").stdout
    assert "STALLED" in out and "re-brief" in out


def test_ladder_climbs_and_each_step_alerts_once(env):
    env.bind()
    send(env, "peer-a", f"check {PR1}")
    age_routed(env, 20)
    assert "one-line state" in env.prompt("x").stdout
    assert "ALERT" in env.run("tick", "--no-notify").stdout
    age_routed(env, 15)  # 35 min: past 2x
    assert "re-brief" in env.prompt("x").stdout
    assert "ALERT" in env.run("tick", "--no-notify").stdout  # a new step alerts without waiting out the back-off
    age_routed(env, 15)  # 50 min: past 3x
    assert "tell the user" in env.prompt("x").stdout
    keys = [r["key"] for r in env.rows("alerts.jsonl") if "routed_stalled" in r["key"]]
    assert sorted(keys) == ["W1:routed_stalled:L0", "W1:routed_stalled:L1"]


def test_every_item_shows_one_next_action(env):
    fake_gh(env, head="aaa1111")
    env.bind()
    send(env, "peer-a", f"check {PR1}")
    out = env.run("status").stdout
    assert "NEXT: wait for peer-a; next check" in out
    set_gh(env, "bbb2222")
    env.run("tick", "--no-notify")
    assert "NEXT: SendMessage peer-a: #101 head moved aaa1111 -> bbb2222; ask for a re-check at the new head" in env.run("status").stdout


def test_review_at_the_current_head_closes_the_item_by_itself(env):
    fake_gh(env, head="aaa1111")
    env.bind()
    send(env, "peer-a", f"Full gated review of {PR1}", "review 101")
    assert routed_items(env)[0]["review"] is True
    set_gh_raw(env, "aaa1111", [review("aaa1111", "CHANGES_REQUESTED")])
    out = env.run("tick", "--no-notify").stdout
    assert routed_items(env) == [] and "CHANGES_REQUESTED" in out and "(the head)" in out and "NEXT" in out
    row = json.loads(next((env.state / "archive").glob("archive_*.jsonl")).read_text().splitlines()[0])
    assert "#101 CHANGES_REQUESTED by rev-er at aaa1111" in row["done_note"]


def test_a_review_on_an_older_head_never_closes_the_item(env):
    fake_gh(env, head="bbb2222")
    env.bind()
    send(env, "peer-a", f"Full gated review of {PR1}", "review 101")
    set_gh_raw(env, "bbb2222", [review("aaa1111")])  # reviewed aaa1111, the PR has moved to bbb2222
    env.run("tick", "--no-notify")
    (item,) = routed_items(env)
    assert item["verdict_old_head"]["commit"] == "aaa1111"
    out = env.prompt("carry on").stdout
    assert "review landed on an old head" in out and "re-review at the head" in out


def test_the_authors_own_review_does_not_close_a_review_item(env):
    fake_gh(env, head="aaa1111")
    env.bind()
    send(env, "peer-a", f"Full gated review of {PR1}", "review 101")
    set_gh_raw(env, "aaa1111", [review("aaa1111", login="pr-author")])
    env.run("tick", "--no-notify")
    assert len(routed_items(env)) == 1


def start_watch(env, interval="30"):
    p = subprocess.Popen([sys.executable, str(SCRIPTS / "hubkit"), "watch", "--interval", interval, "--max-cycles", "3"],
                         env=env.env(), stdout=subprocess.PIPE, text=True)
    env.procs.append(p)
    for _ in range(100):
        if (env.state / "watch.pid").exists():
            return p
        time.sleep(0.1)
    raise AssertionError("watch never wrote its pidfile")


STOP = {"hook_event_name": "Stop", "session_id": "sess-hub", "last_assistant_message": "Routed it."}


def stop(env, **extra):
    r = env.hook("stop", {**STOP, **extra})
    assert r.returncode == 0
    return json.loads(r.stdout) if r.stdout.strip() else None


def test_stop_blocks_until_a_watch_is_running_then_lets_the_turn_end(env):
    env.bind()
    assert stop(env) is None  # nothing routed, nothing to watch
    send(env, "peer-a", f"check {PR1}")
    blocked = stop(env)
    assert blocked["decision"] == "block" and "hubkit watch --interval 120" in blocked["reason"]
    assert stop(env, stop_hook_active=True) is None  # the harness already sent the hub back once: let it through
    start_watch(env)
    assert stop(env) is None
    assert stop(env, stop_hook_active=True) is None
    second = env.run("watch", "--interval", "1", "--max-cycles", "1")
    assert second.returncode == 4 and "already running" in second.stdout  # a second watch never wakes anyone


def test_a_stalled_item_blocks_the_stop_until_the_owner_is_chased(env):
    env.bind()
    start_watch(env)  # first, so its first tick sees nothing stalled and it keeps running
    send(env, "peer-a", f"check {PR1}")
    age_routed(env, 20)
    reason = stop(env)["reason"]
    assert "STALLED: peer-a" in reason and "NEXT: SendMessage peer-a for a one-line state" in reason
    send(env, "peer-a", "Status on #101 please?")
    assert stop(env) is None  # chased at this step


def test_a_question_to_the_user_needs_something_blocked_on_him(env):
    env.bind()
    q = {"last_assistant_message": "I merged nothing.\nShall I merge it?"}
    assert "nothing is blocked on him" in stop(env, **q)["reason"]
    env.run("work", "add", "--kind", "ask", "--title", "Merge #101 now?", "--note", "blocks the release")
    assert stop(env, **q) is None
    env.hook("answer", {"hook_event_name": "PostToolUse", "session_id": "sess-hub", "tool_name": "AskUserQuestion",
                        "tool_response": {"answers": {"Merge?": "yes"}}})
    assert "nothing is blocked on him" in stop(env, **q)["reason"]  # his answer closed the ask


def test_a_verdict_is_checked_against_github_per_pr_and_per_sentence(env):
    fake_gh(env, head="aaa1111")
    env.bind()
    send(env, "peer-a", f"Full gated review of {PR1}", "review 101")
    start_watch(env)
    r = stop(env, last_assistant_message="#101 is approved, merge it.")
    assert r and "verdict on #101" in r["reason"]
    assert stop(env, last_assistant_message="#101 is with the reviewer.") is None
    # a PR number in one sentence and a verdict word in another is not a verdict on that PR
    assert stop(env, last_assistant_message="The review of #101 is under way. Approved plan for the rest.") is None
    # GitHub, not the item, decides: a non-author review at the head lets the verdict through
    set_gh_raw(env, "aaa1111", [review("aaa1111", "CHANGES_REQUESTED")])
    assert stop(env, last_assistant_message="#101 got changes requested.") is None
    set_gh_raw(env, "bbb2222", [review("aaa1111")])  # the head moved past that review
    assert stop(env, last_assistant_message="#101 is approved.")["decision"] == "block"


# ---- 15. live-status bugs: review tag, follow-ups, watch spin, hook-error count, queues, alive owners ----

LONG = "Please work through the build and report back when each stage is complete, with the evidence for it. " * 3


def test_review_clock_applies_only_to_a_pr_review_not_any_text_with_the_word(env):
    env.bind()
    send(env, "peer-a", "Build the research design and review the old notes for the hubkit work. " + LONG, "Build research design")
    assert routed_items(env)[0]["review"] is False
    send(env, "peer-b", f"Full gated review of {PR1}", "review 101")
    assert [i["review"] for i in routed_items(env) if i["owner"] == "peer-b"] == [True]


def test_follow_ups_to_the_same_owner_refresh_one_item_and_are_counted(env):
    env.bind()
    send(env, "peer-a", "Build the thing. Done-when: tests pass. " + LONG, "build")
    asked = routed_items(env)[0]["routed_at"]
    send(env, "peer-a", "One-line state, please. " + LONG, "state")
    send(env, "peer-a", "Pyright flags to fix too. " + LONG, "pyright")
    (item,) = routed_items(env)
    assert item["chases"] == 2 and item["routed_at"] == asked
    assert "chased 2x" in env.run("status").stdout


def test_a_chased_item_neither_alerts_nor_wakes_the_watch_again(env):
    env.bind()
    send(env, "peer-a", f"check {PR1}")
    age_routed(env, 20)
    assert "ALERT" in env.run("tick", "--no-notify").stdout
    send(env, "peer-a", "Status on #101?")  # chased at this step
    age_routed(env, 9)  # 29 min of silence: still the chased step
    p = env.state / "alerts.jsonl"
    rows = [json.loads(l) for l in p.read_text().splitlines()]
    p.write_text("".join(json.dumps({**r, "ts": r["ts"] - 700}) + "\n" for r in rows))  # the 10-minute back-off has passed
    assert env.run("tick", "--no-notify").stdout == ""
    r = env.run("watch", "--interval", "1", "--max-cycles", "2", timeout=30)
    assert r.returncode == 3 and "ALERT" not in r.stdout


def test_our_own_stop_gate_block_is_not_counted_as_a_hook_failure(env):
    def att(msg):
        return json.dumps({"timestamp": "2026-11-05T10:33:13.781Z", "attachment": {
            "type": "hook_blocking_error", "hookEvent": "Stop", "command": "hubkit hook stop",
            "blockingError": {"blockingError": msg, "command": "hubkit"}}})
    (env.transcripts / "s.jsonl").write_text(att("hubkit STALLED: peer-a: W1 nothing for 6 min") + "\n")
    assert "hook errors in scan window: 0" in env.run("status").stdout or "hook errors" not in env.run("status").stdout
    (env.transcripts / "s.jsonl").write_text(att("Traceback (most recent call last): hubkit boom") + "\n")
    env.run("tick", "--no-notify")
    assert "hook errors in scan window: 1" in env.run("status").stdout


def test_a_repeating_hook_error_count_is_one_alert_key(env):
    def att(ts):
        return json.dumps({"timestamp": ts, "attachment": {"type": "hook_non_blocking_error", "command": "hubkit hook x"}})
    f = env.transcripts / "s.jsonl"
    f.write_text(att("2026-11-05T10:00:00.000Z") + "\n")
    env.run("tick", "--no-notify")
    f.write_text(att("2026-11-05T10:00:00.000Z") + "\n" + att("2026-11-05T10:00:00.000Z") + "\n")
    env.run("tick", "--no-notify")
    assert len([r for r in env.rows("alerts.jsonl") if r["key"].startswith("hook_errors")]) == 1


def test_a_running_agent_with_a_growing_transcript_counts_as_heard(env):
    env.bind()
    agent = "a1b2c3d4e5f6a7b8c"
    base = {"hook_event_name": "PostToolUse", "session_id": "sess-hub", "tool_name": "Agent", "tool_use_id": "t",
            "tool_response": {"agentId": agent},
            "tool_input": {"description": "Fix it", "prompt": "Fix the bug. Done-when: tests pass", "run_in_background": True}}
    env.hook("routed", base)
    age_routed(env, 40)
    sub = env.root / "proj" / "sess" / "subagents"
    sub.mkdir(parents=True)
    (sub / f"agent-{agent}.jsonl").write_text("{}\n")
    env.cfg.write_text(env.cfg.read_text() + f'[routed]\nactivity_globs=["{env.root}/proj/*/subagents/agent-{{id}}.jsonl"]\n')
    env.run("tick", "--no-notify")
    assert routed_items(env)[0]["last_heard"] and "STALLED" not in env.prompt("carry on").stdout


def test_a_queue_is_clocked_one_pr_at_a_time(env):
    fake_gh(env, head="aaa1111")
    env.bind()
    urls = [f"https://github.com/acme/repo/pull/{n}" for n in (201, 202, 203, 204, 205)]
    send(env, "peer-a", "Gated review of the queue: " + " ".join(urls), "review the queue")
    (item,) = routed_items(env)
    assert item["prs"] == urls[:1] and item["queue"] == urls
    age_routed(env, 4)
    set_gh_raw(env, "aaa1111", [review("aaa1111")])
    out = env.run("tick", "--no-notify").stdout
    (item,) = routed_items(env)
    assert item["prs"] == urls[1:2] and item["queue"] == urls[1:] and "restarts on the next PR" in out
    assert time.time() - item["routed_at"] < 30  # the clock of the new PR starts now
    assert "+3 queued" in env.run("status").stdout


def test_next_never_says_re_brief_while_the_owner_is_alive_and_reporting(env):
    env.bind()
    send(env, "peer-a", f"Full gated review of {PR1}", "review 101")
    age_routed(env, 11)  # past the hard review limit
    p = env.state / "work.json"
    items = json.loads(p.read_text())
    items[0]["last_heard"] = time.time() - 60
    p.write_text(json.dumps(items))
    out = env.prompt("carry on").stdout
    assert "OVERDUE" in out and "do not re-brief" in out and "re-brief this work" not in out


# ---- 16. independent-check fixes: real 40-hex heads, follow-ups, one PR per clock, no Stop-block traps ----
# (the gh binary is a local stand-in only because the tests cannot reach GitHub; heads are real 40-hex shas)

SHA_A = "58810605d48e989e5c1d6d18287e08f8daace2f0"
SHA_B = "8481ff775e950f9c8b29b38ba19c47f555568623"
SHA_C = "1c0ffee0d48e989e5c1d6d18287e08f8daace2f0"


def test_a_40_hex_head_survives_storage_and_unchanged_head_is_not_a_move(env):
    fake_gh(env, head=SHA_A)
    env.bind()
    send(env, "peer-a", f"check {PR1}")
    assert routed_items(env)[0]["heads"] == {PR1: SHA_A}  # not [REDACTED 1]
    out = env.run("tick", "--no-notify").stdout + env.run("tick", "--no-notify").stdout
    assert "head moved" not in out and routed_items(env)[0]["head_moved"] is None
    set_gh(env, SHA_B)
    assert "head moved 5881060 -> 8481ff7" in env.run("tick", "--no-notify").stdout


def test_sha_is_kept_only_under_head_fields_and_secrets_are_still_redacted():
    from hubcore.redact import redact_obj
    assert redact_obj({"heads": {"u": SHA_A}, "head_moved": {"old": SHA_A, "new": SHA_B}}) == {"heads": {"u": SHA_A}, "head_moved": {"old": SHA_A, "new": SHA_B}}
    assert "REDACTED" in redact_obj({"note": SHA_A})["note"]
    assert "REDACTED" in redact_obj({"heads": {"u": "ghp_" + "a1" * 15}})["heads"]["u"]


def test_a_mangled_stored_baseline_is_rebaselined_not_called_a_move(env):
    fake_gh(env, head=SHA_A)
    env.bind()
    send(env, "peer-a", f"check {PR1}")
    p = env.state / "work.json"
    d = json.loads(p.read_text())
    d[0]["heads"] = {PR1: "[REDACTED 1]"}
    p.write_text(json.dumps(d))
    assert "head moved" not in env.run("tick", "--no-notify").stdout
    assert routed_items(env)[0]["heads"] == {PR1: SHA_A}


def test_follow_up_naming_a_pr_on_the_item_is_a_chase_that_keeps_the_clock_and_answers_a_moved_head(env):
    fake_gh(env, head=SHA_A)
    env.bind()
    send(env, "peer-a", f"Gated review of {PR1}", "review 101")
    asked = routed_items(env)[0]["routed_at"]
    set_gh(env, SHA_B)
    env.run("tick", "--no-notify")
    assert routed_items(env)[0]["head_moved"]
    send(env, "peer-a", f"{PR1} moved to {SHA_B[:7]}, please re-check")
    (item,) = routed_items(env)
    assert item["routed_at"] == asked and item["chases"] == 1
    assert item["head_moved"] is None  # the hub acted: flag cleared
    set_gh(env, SHA_C)  # pushed again between the last tick and the next message: the message reads the new head
    send(env, "peer-a", f"{PR1} moved again to {SHA_C[:7]}, please re-check")
    assert routed_items(env)[0]["heads"] == {PR1: SHA_C}  # that head is the new baseline
    assert "head moved" not in env.run("tick", "--no-notify").stdout


def test_a_new_brief_is_a_new_ask_and_clears_flags_and_restarts_the_clock(env):
    fake_gh(env, head=SHA_A)
    env.bind()
    send(env, "peer-a", f"check {PR1}")
    first = routed_items(env)[0]["routed_at"]
    set_gh(env, SHA_B)
    env.run("tick", "--no-notify")
    assert routed_items(env)[0]["head_moved"]
    time.sleep(0.05)
    send(env, "peer-a", f"Re-do {PR1}. Done-when: reviewed at the new head")
    (item,) = routed_items(env)
    assert item["head_moved"] is None and item["routed_at"] > first and item["chases"] == 0


def test_a_new_brief_to_the_same_owner_without_a_pr_refreshes_the_one_item(env):
    env.bind()
    send(env, "peer-a", "Build the thing. Done-when: tests pass. " + LONG, "build")
    send(env, "peer-a", "Now build the other part. Done-when: it works. " + LONG, "build part two")
    (item,) = routed_items(env)
    assert item["title"] == "build part two" and item["chases"] == 0


def test_a_short_follow_up_without_a_pr_counts_as_a_chase_on_every_item_of_the_owner(env):
    env.bind()
    send(env, "peer-a", f"check {PR1}")
    send(env, "peer-a", "check https://github.com/acme/repo/pull/102")
    send(env, "peer-a", "One-line state, please.")
    send(env, "peer-z", "One-line state, please.")  # no item for peer-z: nothing is tracked
    assert [(i["owner"], i["chases"]) for i in routed_items(env)] == [("peer-a", 1), ("peer-a", 1)]
    send(env, "peer-a", "thanks, got it")
    assert [i["chases"] for i in routed_items(env)] == [1, 1]  # an acknowledgement is not a chase


def test_two_prs_in_one_message_keep_one_pr_per_clock(env):
    env.bind()
    nxt = "https://github.com/acme/repo/pull/102"
    send(env, "peer-a", f"Review {PR1}; the next in the queue is {nxt}", "review 101")
    (item,) = routed_items(env)
    assert item["prs"] == [PR1] and item["queue"] == [PR1, nxt]
    send(env, "peer-a", f"Status on {nxt}?")  # the queued PR is on the item: a chase, not a second item
    (item,) = routed_items(env)
    assert item["chases"] == 1


def test_landed_reviews_drop_their_pr_and_the_gaps_are_reported(env):
    fake_gh(env, head="aaa1111")
    env.bind()
    two = "https://github.com/acme/repo/pull/102"
    env.run("work", "add", "--kind", "routed", "--owner", "peer-a", "--title", "review both", "--pr", PR1, "--pr", two)
    (env.root / "gh").write_text(
        f'#!/bin/sh\ncase "$3" in *101) cat {env.root}/g101.json;; *) cat {env.root}/g102.json;; esac\n')
    (env.root / "g101.json").write_text(json.dumps({"headRefOid": "aaa1111", "author": {"login": "x"}, "reviews": [review("aaa1111")]}))
    (env.root / "g102.json").write_text(json.dumps({"headRefOid": "bbb2222", "author": {"login": "x"}, "reviews": []}))
    out = env.run("tick", "--no-notify").stdout
    (item,) = routed_items(env)
    assert item["prs"] == [two] and "Still open: #102" in out
    assert "SendMessage peer-a" not in env.run("status").stdout or "#102" in env.run("status").stdout


def test_bare_refs_recorded_before_the_repo_was_set_are_resolved_so_gh_runs(env):
    fake_gh(env, head=SHA_A)
    env.bind()
    env.run("work", "add", "--kind", "routed", "--owner", "peer-a", "--title", "check 7432", "--pr", "#7432")
    assert routed_items(env)[0]["prs"] == ["#7432"]
    assert "routed.repo is not set" in env.run("tick", "--no-notify").stdout + env.run("status").stdout
    env.cfg.write_text(env.cfg.read_text() + '[routed]\nrepo = "acme/repo"\n')
    env.run("tick", "--no-notify")
    (item,) = routed_items(env)
    assert item["prs"] == ["https://github.com/acme/repo/pull/7432"] and item["pr_unknown"] == []


def test_every_stop_block_names_a_concrete_action(env):
    fake_gh(env, head=SHA_A)
    env.bind()
    send(env, "peer-a", f"Gated review of {PR1}", "review 101")
    age_routed(env, 20)
    r = stop(env, last_assistant_message="#101 is approved. Shall I merge it?")
    lines = r["reason"].splitlines()
    assert len(lines) >= 3 and all("NEXT:" in l for l in lines), lines


def test_an_old_head_verdict_blocks_once_and_a_chase_clears_it_for_good(env):
    fake_gh(env, head=SHA_B)
    env.bind()
    start_watch(env)
    send(env, "peer-a", f"Gated review of {PR1}", "review 101")
    set_gh_raw(env, SHA_B, [review(SHA_A)])
    env.run("tick", "--no-notify")
    assert "old head" in stop(env)["reason"]
    send(env, "peer-a", f"{PR1}: review again at {SHA_B[:7]}")  # asks for the re-review
    env.run("tick", "--no-notify")
    assert routed_items(env)[0]["verdict_old_head"] is None and stop(env) is None


# ---- 17. what counts as an ask: replies and PRs named in passing ----

def peer_wrote(env, name, minutes_ago=2):
    tr = env.transcripts / "sess-hub.jsonl"
    tr.write_text(queue_line(f'<cross-session-message from="uds:x" from-name="{name}" from-mode="bypass">\nplease send the numbers', iso(time.time() - minutes_ago * 60)) + "\n")
    env.hook("routed", {**SEND, "transcript_path": str(tr), "tool_input": {"to": "nobody", "message": "ok"}})


def test_a_reply_to_a_peer_who_wrote_to_the_hub_is_not_tracked(env):
    env.bind()
    peer_wrote(env, "peer-sdk")
    send(env, "peer-sdk", "Here are the numbers you asked for, the table is in the thread above and the totals match. " * 3)
    assert routed_items(env) == []
    send(env, "peer-sdk", "Now please build the export. Done-when: the tests pass. " + LONG)  # a new brief is an ask
    assert len(routed_items(env)) == 1


def test_the_same_long_message_to_a_peer_who_did_not_write_is_an_ask(env):
    env.bind()
    send(env, "peer-sdk", "Here are the numbers you asked for, the table is in the thread above and the totals match. " * 3)
    assert len(routed_items(env)) == 1


def test_a_pr_named_in_passing_in_a_long_message_is_not_the_owners_pr(env):
    fake_gh(env, head="aaa1111")
    env.bind()
    msg = ("Four live bugs from status, add them to this build: W20 was a message about #101 that mentioned the next "
           "queue PR and was merged into one item. Keep one PR per clock and report the counts when you are done. " * 2)
    send(env, "peer-a", msg, "live bugs")
    (item,) = routed_items(env)
    assert item["prs"] == [] and item["review"] is False
    send(env, "peer-b", msg + " Review " + PR1 + " when you are done.", "bugs and a review")
    assert [i["prs"] for i in routed_items(env) if i["owner"] == "peer-b"] == [[PR1]]  # a directive names it


def test_doctor_does_not_call_a_stored_head_sha_a_secret_but_still_finds_secrets(env):
    env.run("status")
    (env.state / "archive").mkdir(exist_ok=True)
    row = {"type": "work", "heads": {"u": SHA_A}, "head_moved": {"old": SHA_A, "new": SHA_B}}
    (env.state / "archive" / "a.jsonl").write_text(json.dumps(row) + "\n")
    assert "secret-shaped strings in state (count only): 0" in env.run("doctor").stdout
    (env.state / "archive" / "a.jsonl").write_text(json.dumps({"note": SHA_A}) + "\n")
    assert "secret-shaped strings in state (count only): 1" in env.run("doctor").stdout


# ---- 18. who counts as the author; handovers are not reviews; ETA names the open PR (real 40-hex heads) ----

def test_a_handover_that_mentions_review_elsewhere_is_not_a_review(env):
    fake_gh(env, head=SHA_A)
    env.bind()
    send(env, "peer-sdk", f"The box is yours for {PR1} deploy. The earlier review thread is closed, nothing more to review there.", "box handover")
    assert routed_items(env)[0]["review"] is False
    send(env, "peer-b", "Please look at the box. The review of the notes is done.", "review notes")  # a review word, no PR
    assert [i["review"] for i in routed_items(env) if i["owner"] == "peer-b"] in ([], [False])
    send(env, "peer-c", f"Review {PR1} at its head.", "review 101")
    assert [i["review"] for i in routed_items(env) if i["owner"] == "peer-c"] == [True]


def test_the_person_asked_being_the_prs_author_is_not_a_review_and_not_a_head_alert(env):
    fake_gh(env, head=SHA_A)
    set_gh_raw(env, SHA_A, author="dev-author")
    env.bind()
    send(env, "dev-author", f"Please review {PR1}", "review 101")  # asked of the author by name
    send(env, "peer-x", f"Please review {PR1}", "review 101 again")
    mine = {i["owner"]: i for i in routed_items(env)}
    assert mine["dev-author"]["review"] is False and mine["peer-x"]["review"] is True
    set_gh_raw(env, SHA_B, author="dev-author")
    out = env.run("tick", "--no-notify").stdout
    mine = {i["owner"]: i for i in routed_items(env)}
    assert mine["dev-author"]["head_moved"] is None and mine["dev-author"]["heads"] == {PR1: SHA_B}
    assert mine["peer-x"]["head_moved"] and "peer-x" in out and "dev-author" not in out


def test_an_owner_found_to_be_the_author_later_loses_the_review_tag(env):
    fake_gh(env, head=SHA_A)
    env.bind()
    (env.root / "gh.json").unlink()  # GitHub unreachable at routing: the author is unknown then
    send(env, "peer-a", f"Please review {PR1}", "review 101")
    assert routed_items(env)[0]["review"] is True
    env.cfg.write_text(env.cfg.read_text() + '[routed]\nowner_logins = { "peer-a" = "Gh-User" }\n')  # explicit map: peer-a is gh-user
    set_gh_raw(env, SHA_A, author="gh-user")
    env.run("tick", "--no-notify")
    set_gh_raw(env, SHA_B, author="gh-user")
    env.run("tick", "--no-notify")
    (item,) = routed_items(env)
    assert item["review"] is False and item["head_moved"] is None


def test_agents_have_no_login_and_are_never_the_author(env, monkeypatch):
    from hubcore.routed import owner_is_author
    from hubcore import config
    for k, v in env.env().items():
        monkeypatch.setenv(k, v)
    cfg = config.load()
    assert owner_is_author(cfg, "dev-author", "dev-author") and owner_is_author(cfg, "peer-dev-author", "dev-author")
    assert not owner_is_author(cfg, "a1b2c3d4e5f6a7b8c", "dev-author") and not owner_is_author(cfg, "a1b2c3d4e5f6a7b8c", "a1b") and not owner_is_author(cfg, "peer-a", "")


def test_eta_next_names_the_open_prs_not_the_first_one(env):
    fake_gh(env, head=SHA_A)
    env.bind()
    two = "https://github.com/acme/repo/pull/102"
    env.run("work", "add", "--kind", "routed", "--owner", "peer-a", "--title", "fix both", "--pr", PR1, "--pr", two)
    d = json.loads((env.state / "work.json").read_text())
    d[0]["review"] = True
    (env.state / "work.json").write_text(json.dumps(d))
    age_routed(env, 11)  # past the hard review limit
    d = json.loads((env.state / "work.json").read_text())
    d[0]["last_heard"] = time.time() - 60
    (env.state / "work.json").write_text(json.dumps(d))
    out = env.prompt("carry on").stdout
    assert f"ETA on #101 #102" in out
    (env.root / "gh").write_text(f'#!/bin/sh\ncase "$3" in *101) cat {env.root}/g101.json;; *) cat {env.root}/g102.json;; esac\n')
    (env.root / "g101.json").write_text(json.dumps({"headRefOid": SHA_A, "author": {"login": "x"}, "reviews": [review(SHA_A)]}))
    (env.root / "g102.json").write_text(json.dumps({"headRefOid": SHA_B, "author": {"login": "x"}, "reviews": []}))
    env.run("tick", "--no-notify")
    d = json.loads((env.state / "work.json").read_text())
    d[0]["last_heard"] = time.time() - 60
    d[0]["routed_at"] -= 900
    (env.state / "work.json").write_text(json.dumps(d))
    out = env.prompt("carry on").stdout
    assert "ETA on #102;" in out and "#101" not in out.split("NEXT:")[1]


# ---- 19. work queued behind other work has no review clock until the owner starts it ----

def test_a_review_queued_behind_other_work_is_not_overdue_while_the_owner_is_busy(env):
    fake_gh(env, head=SHA_A)
    env.bind()
    send(env, "peer-a", f"Review {PR1} after the other team's queue", "queued review")
    send(env, "peer-b", f"Review {PR1} now", "review now")
    assert {i["owner"]: i["waiting"] for i in routed_items(env)} == {"peer-a": True, "peer-b": False}
    age_routed(env, 12)
    out = env.prompt("carry on").stdout
    assert "peer-b" in out and "OVERDUE: peer-a" not in out and "STALLED: peer-a" not in out
    assert "queued behind other work" in env.run("status").stdout
    age_routed(env, 60)  # the wait is over: the review clock runs from here
    assert "OVERDUE: peer-a" in env.prompt("carry on").stdout


def test_a_review_showing_on_the_pr_means_the_owner_started_the_queued_work(env):
    fake_gh(env, head=SHA_A)
    env.bind()
    send(env, "peer-a", f"Review {PR1} once the other batch is done", "queued review")
    assert routed_items(env)[0]["waiting"] is True
    set_gh_raw(env, SHA_A, [review(SHA_B, login="rev-er")])  # a review on an older commit: started, not at the head
    env.run("tick", "--no-notify")
    assert routed_items(env)[0]["waiting"] is False


def test_queued_phrases_are_detected_narrowly():
    from hubcore.routed import queued_behind
    for yes in ("Queue #7443 and sdk #399 after the other team's", "after the other team's queue", "once X finishes", "behind the batch"):
        assert queued_behind(yes), yes
    for no in ("Review #101 now", "Review it after the fix lands in main", "Do the review"):
        assert not queued_behind(no), no


def reply_from(env, name, minutes_ago=0):
    tr = env.transcripts / "sess-hub.jsonl"
    tr.write_text(queue_line(f'<cross-session-message from="uds:x" from-name="{name}" from-mode="bypass">\nstate: on #101, review half done', iso(time.time() - minutes_ago * 60 + 5)) + "\n")
    env.hook("prompt", {"session_id": "sess-hub", "prompt": "carry on", "transcript_path": str(tr)})


def test_a_state_ask_is_not_a_review_and_the_owners_reply_closes_it(env):
    fake_gh(env, head=SHA_A)
    env.bind()
    send(env, "peer-sdk", f"One-line state of the review on {PR1}, then carry on.", "Ask for a one-line state")
    (item,) = routed_items(env)
    assert item["review"] is False and item["state_ask"] is True
    reply_from(env, "peer-sdk")
    assert routed_items(env) == []
    assert "STALLED" not in env.prompt("carry on").stdout


def test_an_action_on_a_pr_or_a_brief_marker_is_never_a_chase(env):
    fake_gh(env, head=SHA_A)
    env.bind()
    send(env, "peer-a", f"Review these PRs: {PR1} and report back with verdicts, done-when each is reviewed", "review ask")
    (item,) = routed_items(env)
    assert item["review"] is True and item["chases"] == 0 and not item.get("state_ask")
    from hubcore import config
    from hubcore.routed import is_chase
    cfg = config.load()
    assert is_chase(cfg, f"status of {PR1}?")
    assert not is_chase(cfg, "Report back with the numbers. Output: a table")
    assert not is_chase(cfg, f"Review {PR1} and report back")
