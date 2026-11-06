"""Watcher checks outside the hub session: real state, real short-lived processes, real files for the sessions."""

import json
import os
import stat
import sys
import time

import pytest
from test_hubkit import PR1, age_routed, fake_crontab, routed_items, send, start_watch, stop

PR2 = "https://github.com/acme/repo/pull/102"


def tick(env, *args, **extra):
    r = env.run("tick", *args, **extra)
    assert r.returncode == 0, r.stderr
    return r.stdout


def set_session(env, proc, **fields):
    p = env.sessions / f"{proc.pid}.json"
    d = json.loads(p.read_text())
    d.update(fields)
    p.write_text(json.dumps(d))


def ms(minutes_ago):
    return int((time.time() - minutes_ago * 60) * 1000)


def sink(env, name):
    """A real executable that records its arguments: the notification seam."""
    out = env.root / f"{name}.log"
    bin_ = env.root / "bin"
    bin_.mkdir(exist_ok=True)
    exe = bin_ / name
    exe.write_text(f'#!/bin/sh\necho "$@" >> {out}\n')
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe, out


def test_a_dead_hub_with_live_jobs_alerts_and_a_live_one_does_not(env):
    hub = env.bind()
    send(env, "peer-a", f"check {PR1}")
    assert "hub session is not running" not in tick(env, "--no-notify")
    hub.kill()
    hub.wait()
    out = tick(env, "--no-notify")
    assert "ALERT hub session is not running and 1 job are live" in out


def test_a_dead_hub_with_nothing_live_is_quiet(env):
    hub = env.bind()
    hub.kill()
    hub.wait()
    assert "hub session" not in tick(env, "--no-notify")


def test_the_user_is_notified_through_the_configured_command(env):
    exe, out = sink(env, "notifier")
    env.cfg.write_text(env.cfg.read_text() + f'[tools]\nnotify_cmd="{exe}"\n')
    hub = env.bind()
    send(env, "peer-a", f"check {PR1}")
    hub.kill()
    hub.wait()
    tick(env)
    assert "hub session is not running" in out.read_text()


@pytest.mark.skipif(sys.platform != "linux", reason="the default channel here is notify-send")
def test_the_default_desktop_channel_carries_hub_alerts_and_nothing_else(env):
    exe, out = sink(env, "notify-send")
    path = {"PATH": f"{exe.parent}{os.pathsep}{os.environ['PATH']}"}
    hub = env.bind()
    start_watch(env)
    send(env, "peer-a", f"check {PR1}")
    age_routed(env, 20)
    tick(env, **path)  # a stalled item: for the hub, not for the user's desktop
    assert not out.exists()
    hub.kill()
    hub.wait()
    tick(env, **path)
    text = out.read_text()
    assert "hub session is not running" in text and "STALLED" not in text and "watch" not in text.lower()


def test_a_hub_busy_far_too_long_is_stuck_and_a_recent_busy_is_not(env):
    hub = env.bind()
    send(env, "peer-a", f"check {PR1}")
    set_session(env, hub, status="busy", statusUpdatedAt=ms(5))
    assert "hub session has been busy" not in tick(env, "--no-notify")
    set_session(env, hub, status="busy", statusUpdatedAt=ms(40))
    assert "hub session has been busy for 40 min with 1 live jobs" in tick(env, "--no-notify")


def stale_alert(env):
    env.bind()
    send(env, "peer-a", f"check {PR1}")
    for f in (env.state / "seen").glob("*"):
        os.utime(f, (time.time() - 3600,) * 2)
    with open(env.state / "alerts.jsonl", "a") as fh:
        fh.write(json.dumps({"key": "W1:routed_stalled:L0", "msg": "W1 stalled", "ts": time.time() - 900}) + "\n")


def test_an_alert_the_hub_has_not_acted_on_is_flagged(env):
    stale_alert(env)
    assert "alert unhandled for 15 min" in tick(env, "--no-notify")


def test_an_alert_the_hub_acted_on_is_not_flagged(env):
    stale_alert(env)
    (env.state / "seen" / "prompt").touch()  # the hub took a prompt after the alert
    assert "alert unhandled" not in tick(env, "--no-notify")


def test_a_peer_waiting_on_a_prompt_is_reported_with_how_long(env):
    env.bind()
    peer = env.session(sid="sess-peer", name="peer-a")
    send(env, "peer-a", f"check {PR1}")
    set_session(env, peer, status="busy", waitingFor="permission to run Bash", statusUpdatedAt=ms(1))
    assert "peer-a has waited" not in tick(env, "--no-notify")
    set_session(env, peer, waitingFor="permission to run Bash", statusUpdatedAt=ms(5))
    assert "ALERT peer-a has waited 5 min on permission to run Bash (holds W1)" in tick(env, "--no-notify")
    set_session(env, peer, waitingFor="", statusUpdatedAt=ms(5))
    assert "peer-a has waited" not in tick(env, "--no-notify")


def test_a_stall_alert_names_what_the_holder_is_doing(env):
    env.bind()
    start_watch(env)
    env.session(sid="sess-peer", name="peer-a")
    send(env, "peer-a", f"check {PR1}")
    age_routed(env, 20)
    out = tick(env, "--no-notify")
    assert "STALLED" in out or "nothing for" in out
    assert "holder: unknown (session " in out


@pytest.mark.parametrize("text,tracked", [
    ("Can you look at why the export test fails and tell me?", True),
    ("Please rebase the branch on release", True),
    ("did the migration run?", True),
    ("ok thanks, will do", False),
    ("Done, thanks", False),
    ("Status?", False),
    ("Pushed the fix.", False),
    ("please", False),
])
def test_short_messages_are_tracked_only_when_they_ask_for_something(env, text, tracked):
    env.bind()
    send(env, "peer-a", text)
    assert bool(routed_items(env)) is tracked


def test_a_holder_that_is_visibly_working_does_not_stall_and_one_gone_quiet_does(env):
    env.bind()
    start_watch(env)
    peer = env.session(sid="sess-peer", name="peer-a")
    set_session(env, peer, status="busy")
    tr = env.transcripts / "sess-peer.jsonl"
    tr.write_text("{}\n")
    send(env, "peer-a", f"check {PR1}\nEstimate: 60m")
    age_routed(env, 20)
    assert stop(env) is None and "STALLED" not in tick(env, "--no-notify")
    os.utime(tr, (time.time() - 600,) * 2)  # its transcript stopped growing
    assert "STALLED: peer-a" in stop(env)["reason"]
    os.utime(tr, (time.time(),) * 2)
    set_session(env, peer, status="idle")  # fresh transcript but the session is idle: not working
    assert "STALLED: peer-a" in stop(env)["reason"]


def test_a_job_blocked_on_another_does_not_alarm_until_the_link_is_cleared(env):
    env.bind()
    start_watch(env)
    send(env, "peer-a", f"fix {PR1}")
    send(env, "peer-b", f"review {PR2}")
    age_routed(env, 20)
    assert env.run("work", "block", "W2", "--on", "W9").returncode == 2  # no such job
    assert env.run("work", "block", "W2", "--on", "W1").returncode == 0
    reason = stop(env)["reason"]
    assert "peer-a" in reason and "peer-b" not in reason
    env.run("work", "block", "W2")  # cleared
    assert "peer-b" in stop(env)["reason"]


def test_a_dead_watch_with_live_work_alerts_and_a_running_one_does_not(env):
    env.bind()
    send(env, "peer-a", f"check {PR1}")
    assert "hubkit watch is not running" not in tick(env, "--no-notify")  # inside the grace period
    age_routed(env, 10)
    assert "ALERT hubkit watch is not running" in tick(env, "--no-notify")
    start_watch(env)
    assert "hubkit watch is not running" not in tick(env, "--no-notify")
    assert "hubkit watch is NOT running" not in env.run("status").stdout


def test_the_status_block_tells_the_hub_to_re_arm_a_dead_watch(env):
    env.bind()
    send(env, "peer-a", f"check {PR1}")
    assert "hubkit watch is NOT running with live jobs. NEXT: start in the background" in env.run("status").stdout


def test_a_watch_notices_a_cron_that_stopped_running_hubkit(env):
    cron = fake_crontab(env, "*/5 * * * * python3 ~/.claude/skills/hub/scripts/hubkit tick --if-bound\n")
    (env.state / "seen").mkdir(parents=True)
    seen = env.state / "seen" / "cron"
    seen.write_text("")
    os.utime(seen, (time.time() - 3600,) * 2)
    r = env.run("watch", "--interval", "1", "--max-cycles", "1", **cron)
    assert r.returncode == 0 and "ALERT cron has not run hubkit" in r.stdout
    seen.touch()
    assert env.run("watch", "--interval", "1", "--max-cycles", "1", **cron).returncode == 3


def write_gh(env, web_reviewed, repo_head="a" * 40, web_head="b" * 40):
    """A local stand-in for gh (tests cannot reach GitHub) that answers per repo in the PR URL."""
    gh = env.root / "gh"
    approved = {"submittedAt": "2026-11-06T10:00:00Z", "state": "APPROVED", "commit": {"oid": web_head},
                "author": {"login": "rev"}, "body": ""}
    for name, head, revs in (("repo", repo_head, []), ("web", web_head, [approved] if web_reviewed else [])):
        (env.root / f"{name}.json").write_text(json.dumps({"headRefOid": head, "reviews": revs, "author": {"login": "auth"}}))
    gh.write_text(f'#!/bin/sh\ncase "$3" in *acme/web*) cat {env.root}/web.json;; *) cat {env.root}/repo.json;; esac\n')
    gh.chmod(0o755)
    env.cfg.write_text(env.cfg.read_text() + f'[tools]\ngh="{gh}"\n[routed]\nrepo="acme/repo"\nrepo_aliases={{fe="acme/web"}}\n')


def test_a_pr_number_is_looked_up_in_its_own_repo_not_the_default_one(env):
    write_gh(env, web_reviewed=True)
    env.bind()
    start_watch(env)
    send(env, "peer-a", f"Please review {PR1} and FE 3468 for me")
    (item,) = routed_items(env)
    assert item["prs"] == [PR1] and item["queue"] == [PR1, "https://github.com/acme/web/pull/3468"]
    # the web PR has an approval at its head; the default repo's PR 3468 would show none
    assert stop(env, last_assistant_message="FE 3468 is approved.") is None
    assert "no review at its current head" in stop(env, last_assistant_message="#101 is approved.")["reason"]


def test_the_same_number_in_two_repos_is_two_prs(env):
    env.bind()
    send(env, "peer-a", "Please check acme/web#101 for me")
    send(env, "peer-a", f"Brief: also check {PR1}. Done-when: checked")
    assert [i["prs"] for i in routed_items(env)] == [["https://github.com/acme/web/pull/101"], [PR1]]  # two jobs
    send(env, "peer-a", "Status on acme/web#101?")
    assert len(routed_items(env)) == 2 and routed_items(env)[0]["chases"] == 1  # a follow-up finds its own PR


def test_a_head_moved_by_the_owners_own_commit_is_progress_not_a_stall(env):
    write_gh(env, web_reviewed=False)
    env.bind()
    send(env, "peer-a", f"Please check {PR1}")
    (env.root / "repo.json").write_text(json.dumps({"headRefOid": "c" * 40, "reviews": [], "author": {"login": "auth"},
                                                    "commits": [{"authors": [{"login": "peer-a"}]}]}))
    assert "head moved" not in tick(env, "--no-notify")
    (env.root / "repo.json").write_text(json.dumps({"headRefOid": "d" * 40, "reviews": [], "author": {"login": "auth"},
                                                    "commits": [{"authors": [{"login": "someone-else"}]}]}))
    assert "head moved" in tick(env, "--no-notify")


def test_an_orders_holder_is_its_linked_item_and_unknown_without_a_link(env):
    env.bind()
    send(env, "peer-b", "Please check https://github.com/acme/repo/pull/555")  # the latest routed item: unrelated
    env.run("order", "open", "--kind", "merge", "--quote", "merge the release branch")
    oid = env.rows("orders.jsonl")[0]["id"]
    env.run("order", "move", oid, "--note", "started")
    send(env, "peer-a", "Please prepare the release notes for the merge")
    env.backdate(40)
    age_routed(env, 40)
    out = tick(env, "--no-notify")
    assert f"SLOW {oid}" in out and "holder unknown; why: unknown (no routed item is linked to this job)" in out
    env.run("order", "move", oid, "--note", "release notes routed to peer-a as W2")
    env.backdate(0)
    out = tick(env, "--no-notify")
    assert f"SLOW {oid}" in out and "holder peer-a;" in out


CRON_LINE = "*/5 * * * * python3 ~/.claude/skills/hub/scripts/hubkit tick --if-bound\n"


def test_cron_alone_reports_a_dead_hub_that_holds_only_routed_jobs(env):
    hub = env.bind()
    send(env, "peer-a", f"check {PR1}")  # routed work only: no order is open
    hub.kill()
    hub.wait()
    out = tick(env, "--if-bound", "--no-notify")  # the exact command the crontab line runs
    assert "ALERT hub session is not running and 1 job are live" in out


def test_cron_alone_notifies_the_desktop_when_the_hub_is_dead(env):
    exe, out = sink(env, "notifier")
    env.cfg.write_text(env.cfg.read_text() + f'[tools]\nnotify_cmd="{exe}"\n')
    cron = fake_crontab(env, CRON_LINE)
    hub = env.bind()
    send(env, "peer-a", f"check {PR1}")
    hub.kill()
    hub.wait()
    assert not (env.state / "watch.pid").exists()  # no watch: the hub's stop gate is not in play
    tick(env, "--if-bound", **cron)
    assert "hub session is not running" in out.read_text()


def test_a_cron_that_never_ran_is_flagged_once_work_has_waited_past_the_limit(env):
    cron = fake_crontab(env, CRON_LINE)
    env.bind()
    send(env, "peer-a", f"check {PR1}")
    r = env.run("watch", "--interval", "1", "--max-cycles", "1", **cron)
    assert "cron has not run" not in r.stdout  # inside the grace
    age_routed(env, 20)
    assert not (env.state / "seen" / "cron").exists()
    r = env.run("watch", "--interval", "1", "--max-cycles", "1", **cron)
    assert "ALERT cron has not run hubkit" in r.stdout


def test_the_real_peer_session_shape_waiting_for_input_is_reported(env):
    env.bind()
    peer = env.session(sid="sess-peer", name="peer-a")
    send(env, "peer-a", f"check {PR1}")
    set_session(env, peer, status="waiting", waitingFor="input needed", statusUpdatedAt=ms(5))
    assert "ALERT peer-a has waited 5 min on input needed (holds W1)" in tick(env, "--no-notify")
    (env.state / "alerts.jsonl").unlink()  # past the back-off
    set_session(env, peer, status="waiting", waitingFor="", statusUpdatedAt=ms(9))
    assert "peer-a has waited 9 min on waiting (holds W1)" in tick(env, "--no-notify")  # the status alone is enough


def test_watcher_findings_reach_the_hub_status_block(env):
    env.bind()
    peer = env.session(sid="sess-peer", name="peer-a")
    send(env, "peer-a", f"check {PR1}")
    set_session(env, peer, status="waiting", waitingFor="input needed", statusUpdatedAt=ms(5))
    tick(env, "--no-notify")
    block = env.run("status").stdout
    assert "watcher: peer-a has waited 5 min on input needed" in block
    set_session(env, peer, status="busy", waitingFor="", statusUpdatedAt=ms(1))
    tick(env, "--no-notify")
    assert "watcher: peer-a" not in env.run("status").stdout
