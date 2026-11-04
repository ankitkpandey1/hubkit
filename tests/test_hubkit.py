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


def test_stop_guard_logs_only(env):
    env.bind()
    env.prompt(HOTFIX)
    p = {"hook_event_name": "Stop", "session_id": "sess-hub", "last_assistant_message": "Done thinking.\nShall I merge it?"}
    r = env.hook("stop", p)
    assert r.returncode == 0 and r.stdout == ""
    assert env.rows("stop-guard.jsonl")[0]["would_block"] is True
    n = len(env.rows("stop-guard.jsonl"))
    env.hook("stop", {**p, "stop_hook_active": True})
    assert len(env.rows("stop-guard.jsonl")) == n


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
