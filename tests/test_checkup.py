"""Worker check-ups, ETA honoured, stored review asks, noise and notifications (the 25 Nov 12:13 asks)."""

import json
import os
import stat
import time

import pytest
from test_priority import age_routed, stop, work


def route(env, owner="peer-a", *extra):
    out = env.run("work", "add", "--kind", "routed", "--owner", owner, "--title", "check the billing fix and report back", *extra)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def set_session(env, p, **fields):
    f = env.sessions / f"{p.pid}.json"
    d = json.loads(f.read_text())
    d.update(fields)
    f.write_text(json.dumps(d))


def checkup(env):
    return env.run("checkup").stdout


def test_checkup_matches_the_session_when_its_name_has_a_ref_suffix(env):
    env.bind()
    peer = env.session(sid="s-peer", name="peer-a [93f867]")
    route(env, "peer-a")
    set_session(env, peer, status="busy")
    out = checkup(env)
    assert "peer-a" in out and "session: live, busy" in out and "no live session" not in out


def test_checkup_says_so_when_there_is_no_such_session(env):
    env.bind()
    route(env, "peer-z")
    assert "session: no live session" in checkup(env)


def test_checkup_sorts_items_into_the_five_states(env):
    env.bind()
    w = env.session(sid="s-w", name="waiter")
    set_session(env, w, status="waiting", waitingFor="permission prompt")
    route(env, "waiter")
    route(env, "fresh")
    route(env, "quiet")
    route(env, "late", "--estimate", "20")
    env.session(sid="s-free", name="free-peer")
    (env.state / "peers.json").write_text(json.dumps({"free-peer": time.time() - 60}))
    rows = work(env)
    for r in rows:
        if r["owner"] in ("quiet", "late"):
            for k in ("added", "routed_at", "last_heard"):
                if r.get(k):
                    r[k] -= (60 if r["owner"] == "quiet" else 40) * 60
    (env.state / "work.json").write_text(json.dumps(rows))
    lines = {x.split()[1]: x for x in checkup(env).splitlines() if x.startswith("  ")}
    assert "waiting-on-permission-prompt" in lines["waiter"]
    assert "reported-within-ETA" in lines["fresh"]
    assert " idle " in lines["quiet"] or " overdue " in lines["quiet"]
    assert " overdue " in lines["late"]
    assert " free " in lines["free-peer"]


def test_checkup_due_line_in_the_prompt_and_stop_hooks_until_done(env):
    env.bind()
    route(env, "quiet")
    age_routed(env, 60)
    assert "hubkit CHECKUP DUE: 1 item(s) not reported within ETA: W1" in env.prompt("how is the weather").stdout
    assert "CHECKUP DUE" in json.loads(stop(env))["reason"]
    assert env.run("checkup", "done").returncode == 0
    assert "CHECKUP DUE" not in env.prompt("how is the weather").stdout
    p = env.state / "checkups.jsonl"
    rows = [json.loads(x) for x in p.read_text().splitlines()]
    rows[-1]["ts"] -= 31 * 60
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert "CHECKUP DUE" in env.prompt("how is the weather").stdout


def test_no_checkup_line_while_every_item_is_within_its_eta(env):
    env.bind()
    route(env, "fresh")
    assert "CHECKUP DUE" not in env.prompt("how is the weather").stdout


def test_checkup_sends_nothing(env):
    env.bind()
    route(env, "quiet")
    age_routed(env, 60)
    before = {p.name: p.stat().st_mtime for p in env.sessions.iterdir()}
    checkup(env)
    assert before == {p.name: p.stat().st_mtime for p in env.sessions.iterdir()}


# ---- ETA honoured ----

def test_a_stated_eta_stops_the_stalled_line_until_it_passes(env):
    env.bind()
    route(env, "peer-a", "--estimate", "90")
    age_routed(env, 40)
    out = env.prompt("how is the weather").stdout
    assert "hubkit STALLED" not in out and "re-brief" not in out
    age_routed(env, 60)  # now 100 minutes: past the 90 minute ETA
    assert "hubkit STALLED" in env.prompt("how is the weather").stdout


def test_a_defaulted_estimate_is_not_an_eta(env):
    env.bind()
    route(env, "peer-a")
    age_routed(env, 40)
    assert "hubkit STALLED" in env.prompt("how is the weather").stdout


# ---- re-review asks from stored state ----

REVIEW_LINE = "1781000 · alice · DM: alice · Can you re-review https://github.com/acme/repo/pull/77 when you get a minute · reply\n"
OTHER_LINE = "1781001 · bob · DM: bob · lunch tomorrow? · reply\n"


def test_a_review_ask_in_stored_watcher_output_opens_one_item_that_shows_in_status(env):
    env.bind()
    (env.state / "mep_watch.out").write_text(OTHER_LINE + REVIEW_LINE)
    env.run("tick", "--no-notify")
    env.run("tick", "--no-notify")
    items = [i for i in work(env) if i["kind"] == "reply"]
    assert len(items) == 1 and items[0]["title"].startswith("REVIEW ASK") and items[0]["review_ask"]
    assert "REVIEW ASK" in env.run("status").stdout
    assert env.run("work", "done", items[0]["id"], "--note", "carded").returncode == 0
    env.run("tick", "--no-notify")
    assert not [i for i in work(env) if i["kind"] == "reply"]  # dropped means dropped


def test_an_ordinary_inbox_line_is_not_ingested_by_the_scan(env):
    env.bind()
    (env.state / "mep_watch.out").write_text(OTHER_LINE)
    env.run("tick", "--no-notify")
    assert [i for i in work(env) if i["kind"] == "reply"] == []


# ---- noise and notifications ----

@pytest.mark.parametrize("text", [
    "<cross-session-message from-name=\"peer\">this is urgent, code red</cross-session-message>",
    "[system] urgent: code red",
    "<task-notification>urgent asap</task-notification>",
])
def test_peer_and_harness_text_opens_no_priority_item(env, text):
    env.bind()
    env.prompt(text)
    assert [i for i in work(env) if i.get("lead")] == []


def test_a_non_human_origin_opens_no_priority_item(env):
    env.bind()
    env.hook("prompt", {"hook_event_name": "UserPromptSubmit", "session_id": "sess-hub", "prompt": "urgent code red", "origin": {"kind": "peer"}})
    assert [i for i in work(env) if i.get("lead")] == []


def fake_notify(env, tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    log = tmp_path / "notified.log"
    f = d / "notify-send"
    f.write_text(f"#!/bin/sh\necho fired >> {log}\n")
    f.chmod(f.stat().st_mode | stat.S_IXUSR)
    return str(d) + os.pathsep + os.environ["PATH"], log


def raise_hub_dead_alert(env, path):
    env.run("order", "open", "--kind", "merge", "--quote", "merge it now")  # a live job and no bound hub: hub:dead
    return env.run("tick", PATH=path)


def test_no_desktop_notification_fires_with_notify_user_false_and_no_notify_cmd(env, tmp_path):
    path, log = fake_notify(env, tmp_path)
    with env.cfg.open("a") as f:
        f.write('[watcher]\nnotify_user=false\n[tools]\nnotify_cmd=""\n')
    r = raise_hub_dead_alert(env, path)
    assert "hub:dead" in r.stdout or "hub session is not running" in r.stdout
    assert not log.exists()


def test_the_same_alert_does_notify_when_notify_user_is_true(env, tmp_path):
    path, log = fake_notify(env, tmp_path)
    with env.cfg.open("a") as f:
        f.write("[watcher]\nnotify_user=true\n")
    raise_hub_dead_alert(env, path)
    assert log.exists()


def test_a_review_ask_stays_in_the_status_head_under_thirty_routed_lines(env):
    env.bind()
    for n in range(30):
        route(env, f"peer-{n}")
    (env.state / "mep_watch.out").write_text(REVIEW_LINE)
    env.run("tick", "--no-notify")
    head = env.run("status").stdout
    assert "REVIEW ASK" in head and "more lines cut" in head
