"""The peer roster: learned from the peers' own words, shown stale when old, used at routing time, with a record per peer.

The only fake is `gh` where a test needs a PR state (a local stand-in; the tests cannot reach GitHub).
"""

import json
import time

from test_hubkit import PR1, age_routed, send
from test_peer_reports import deliver, peer_entry

REVIEWER = "Reviewer (code-review only): I review PRs and report verdicts. I'm idle now."
WORKER = "No role assigned yet. Treat me as a free worker."
BUSY = "Role: research worker. I'm busy running a benchmark, not free for other work."
WORK = f"Rebase {PR1} on main and push. Done-when: pushed. Output: the new head sha. Estimate: 10m"
ASK = f"Please review {PR1}. Done-when: a verdict is posted. Output: the verdict. Estimate: 10m"
OTHER = "Start the smoke on https://github.com/acme/repo/pull/102. Done-when: smoke passes. Output: the log tail. Estimate: 10m"


def tell(env, name, body):
    return deliver(env, peer_entry(name, body))


def ls(env):
    return env.run("peers", "ls").stdout


def roster_file(env):
    return json.loads((env.state / "roster.json").read_text())


def test_peers_announce_role_and_state_and_ls_shows_them(env):
    env.bind()
    tell(env, "peer-r", REVIEWER)
    tell(env, "peer-w", WORKER)
    tell(env, "peer-x", BUSY)
    out = ls(env)
    assert "peer-r: reviewer, free" in out and "peer-w: worker, free" in out and "peer-x: worker, busy" in out


def test_a_message_that_is_no_announcement_changes_nothing(env):
    env.bind()
    tell(env, "peer-w", WORKER)
    tell(env, "peer-w", "Pushed the fix, tests pass, see the PR for the reviewer notes.")
    assert "peer-w: worker, free" in ls(env)


def test_not_busy_means_free_and_a_later_message_replaces_an_earlier_state(env):
    env.bind()
    tell(env, "peer-w", BUSY)
    tell(env, "peer-w", "Finished. I'm idle now, not busy.")
    assert "peer-w: worker, free" in ls(env)


def test_a_state_older_than_the_stale_age_is_shown_stale_and_not_trusted(env):
    env.bind()
    tell(env, "peer-w", WORKER)
    data = roster_file(env)
    data["peers"]["peer-w"]["learned_at"] -= 3 * 3600
    (env.state / "roster.json").write_text(json.dumps(data))
    assert "peer-w: worker, STALE (said free 18" in ls(env)
    send(env, "peer-b", WORK)  # holds a live job
    out = send(env, "peer-b", OTHER, "second").stdout
    assert "a fitting peer is free" not in out  # the stale 'free' is not offered


def test_by_hand_set_ls_and_forget(env):
    env.bind()
    assert env.run("peers", "set", "peer-h", "--role", "Reviewer", "--note", "mornings only", "--free").returncode == 0
    out = ls(env)
    assert "peer-h: reviewer, free" in out and "mornings only" in out
    deliver(env, peer_entry("peer-h", WORKER, ago=-5))  # newer than the hand edit; a role set by hand is kept
    assert "peer-h: reviewer" in ls(env)
    env.run("peers", "set", "peer-h", "--busy")
    assert "peer-h: reviewer, busy" in ls(env)
    assert "forgotten" in env.run("peers", "forget", "peer-h").stdout
    assert "peer-h" not in ls(env)
    assert env.run("peers", "set", "peer-h", "--busy", "--free").returncode != 0


def test_a_peer_holding_a_live_job_is_busy_whatever_it_said(env):
    env.bind()
    tell(env, "peer-w", WORKER)
    send(env, "peer-w", WORK)
    assert "peer-w: worker, busy, holds W1" in ls(env)


def test_a_review_ask_to_a_non_reviewer_warns_while_a_reviewer_is_free(env):
    env.bind()
    tell(env, "peer-r", REVIEWER)
    tell(env, "peer-w", WORKER)
    out = send(env, "peer-w", ASK).stdout
    assert "a review ask went to peer-w (worker) while a reviewer is free" in out and "NEXT: send it to peer-r" in out
    assert "while a reviewer is free" not in send(env, "peer-r", ASK.replace("101", "102")).stdout


def test_non_review_work_to_a_reviewer_only_peer_warns(env):
    env.bind()
    tell(env, "peer-r", REVIEWER)
    out = send(env, "peer-r", WORK).stdout
    assert "peer-r is review-only and this is not a review ask" in out


def test_a_busy_owner_with_a_free_worker_warns_without_any_open_order(env):
    env.bind()
    tell(env, "peer-w", WORKER)
    send(env, "peer-a", WORK)
    out = send(env, "peer-a", OTHER, "second").stdout
    assert "peer-a is busy (holds W1) and a fitting peer is free" in out and "NEXT: send it to peer-w" in out
    assert "a fitting peer is free" not in send(env, "peer-w", OTHER.replace("102", "103")).stdout


def test_a_busy_owner_with_nobody_free_is_not_warned(env):
    env.bind()
    send(env, "peer-a", WORK)
    assert "a fitting peer is free" not in send(env, "peer-a", OTHER, "second").stdout


def finish(env, owner, minutes_ago, brief=WORK):
    send(env, owner, brief)
    age_routed(env, minutes_ago)
    (wid,) = [i["id"] for i in json.loads((env.state / "work.json").read_text()) if i["owner"] == owner]
    assert env.run("work", "done", wid, "--note", "done").returncode == 0


def test_the_record_counts_jobs_median_and_late_and_prefers_the_better_peer(env):
    env.bind()
    tell(env, "peer-slow", WORKER)  # announced first: only the record can put peer-good ahead
    tell(env, "peer-good", WORKER)
    finish(env, "peer-good", 4)  # inside its 10 m estimate
    finish(env, "peer-slow", 30)  # over it
    tell(env, "peer-good", "Idle now.")
    tell(env, "peer-slow", "Idle now.")
    out = ls(env)
    assert "peer-good: worker, free" in out and "jobs done 1, median 4m, late 0" in out
    assert "peer-slow: worker, free" in out and "late 1" in out
    send(env, "peer-a", WORK.replace("101", "201"))
    warn = send(env, "peer-a", OTHER, "second").stdout
    assert "NEXT: send it to peer-good (worker, free, 1 done, median 4m, 0 late)" in warn


def test_the_status_block_has_a_one_line_roster(env):
    env.bind()
    tell(env, "peer-r", REVIEWER)
    tell(env, "peer-w", WORKER)
    env.run("tick", "--no-notify")
    out = env.hook("session", {"session_id": "sess-hub", "hook_event_name": "SessionStart"}).stdout
    (line,) = [x for x in out.splitlines() if x.startswith("roster:")]
    assert "peer-r: reviewer, free, " in line and "peer-w: worker, free, " in line


def role_cfg(env):
    env.cfg.write_text(env.cfg.read_text() + '[roles.reviewer]\nbrief_must_include = ["code-review skill", "(?i)verdict.*head"]\n')


def test_a_review_brief_missing_a_required_role_line_names_it(env):
    env.bind()
    role_cfg(env)
    out = send(env, "peer-r", ASK).stdout
    assert 'lacks "code-review skill"' in out and 'lacks "(?i)verdict.*head"' in out
    full = ASK + " Follow the code-review skill. Post the verdict for the current head."
    assert "brief_must_include" not in send(env, "peer-r", full.replace("101", "102")).stdout


def test_a_brief_to_a_peer_known_as_reviewer_is_checked_even_when_it_is_no_review_ask(env):
    env.bind()
    role_cfg(env)
    tell(env, "peer-r", REVIEWER)
    assert 'lacks "code-review skill"' in send(env, "peer-r", WORK).stdout
    assert "brief_must_include" not in send(env, "peer-w", WORK.replace("101", "103")).stdout


def test_no_role_config_means_no_role_warning(env):
    env.bind()
    assert "brief_must_include" not in send(env, "peer-r", ASK).stdout


def test_a_correction_soon_after_a_brief_to_a_role_is_counted_against_it(env):
    env.bind()
    role_cfg(env)
    send(env, "peer-r", ASK)
    assert "CORRECTION" in env.prompt("I told you to make the reviewer follow the code-review skill").stdout
    assert "role reviewer: 1 corrections after briefs to this role" in ls(env)
    env.prompt("I told you already, again")  # no brief in the window any more
    assert "role reviewer: 1 corrections" in ls(env)


def test_a_correction_outside_the_window_is_not_counted(env):
    env.bind()
    send(env, "peer-r", ASK)
    data = roster_file(env)
    for b in data["briefs"]:
        b["ts"] -= 3600
    (env.state / "roster.json").write_text(json.dumps(data))
    assert "CORRECTION" in env.prompt("I told you to make the reviewer follow the skill").stdout
    assert "corrections after briefs" not in ls(env)


def test_the_hooks_never_fail_on_a_broken_roster_file(env):
    env.bind()
    (env.state / "roster.json").parent.mkdir(parents=True, exist_ok=True)
    (env.state / "roster.json").write_text("{not json")
    r = send(env, "peer-a", WORK)
    assert r.returncode == 0 and "hubkit: tracking" in r.stdout
    assert time.time() > 0
