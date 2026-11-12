"""Two warnings that never close anything: open items whose PR is already merged or closed, and a routed item with no PR whose
owner reported done.

The one stand-in is the `gh` binary (tests/ghkit.py); everything else is the real CLI, hooks and files."""

import json

import ghkit
from ghkit import URL, review  # noqa: F401
from test_hubkit import age_routed, routed_items, send
from test_peer_reports import deliver, peer_entry


def add_item(env, kind, title):
    r = env.run("work", "add", "--kind", kind, "--title", title, "--note", "x")
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def age(env, wid, days):
    f = env.state / "work.json"
    items = json.loads(f.read_text())
    for i in items:
        if i["id"] == wid:
            i["added"] -= days * 86400
    f.write_text(json.dumps(items))


def test_an_item_naming_a_merged_or_closed_pr_is_marked_probably_stale_in_mine_and_digest(env):
    ghkit.install(env)
    ghkit.pr(env, 101, state="MERGED", merged_ago=2.5 * 86400)
    ghkit.pr(env, 102, state="CLOSED", merged_ago=3 * 86400)
    ghkit.pr(env, 103, state="OPEN")
    w1 = add_item(env, "tracked", f"waiting on the review of {URL.format(n=101)}")
    w2 = add_item(env, "tracked", f"waiting on {URL.format(n=102)}")
    w3 = add_item(env, "tracked", f"waiting on {URL.format(n=103)}")
    add_item(env, "tracked", "waiting on the vendor, no PR")
    age(env, w1, 5)
    mine = env.run("mine").stdout
    assert f"{w1} 5d: waiting: waiting on the review of {URL.format(n=101)}" in mine
    row = next(x for x in mine.splitlines() if f" {w1} " in x)
    assert row.endswith("— PR merged 2d ago, probably stale; last activity 5d ago")
    assert "PR closed 3d ago, probably stale" in next(x for x in mine.splitlines() if f" {w2} " in x)
    assert "probably stale" not in next(x for x in mine.splitlines() if f" {w3} " in x)
    assert sum("probably stale" in x for x in mine.splitlines()) == 2
    dg = env.run("digest").stdout
    assert f"probably stale {w1}" in dg and f"probably stale {w3}" not in dg
    assert len(json.loads((env.state / "work.json").read_text())) == 4  # warn only: nothing was closed


def test_an_item_naming_two_prs_is_stale_only_when_both_are_done(env):
    ghkit.install(env)
    ghkit.pr(env, 101, state="MERGED", merged_ago=3600)
    ghkit.pr(env, 102, state="OPEN")
    add_item(env, "tracked", f"waiting on {URL.format(n=101)} and {URL.format(n=102)}")
    assert "probably stale" not in env.run("mine").stdout
    ghkit.pr(env, 102, state="CLOSED", merged_ago=60)
    assert "probably stale" in env.run("mine").stdout


def test_the_gh_reads_per_run_are_capped_and_a_failing_gh_marks_nothing(env):
    ghkit.install(env)
    ghkit.conf(env, "[stale]\nmax_reads=1\n")
    for n in (101, 102):
        ghkit.pr(env, n, state="MERGED", merged_ago=3600)
        add_item(env, "tracked", f"waiting on {URL.format(n=n)}")
    mine = env.run("mine").stdout
    assert mine.count("probably stale") == 1 and ghkit.calls(env, "pr view") == 1
    (env.root / "pr_101.json").unlink()
    (env.root / "pr_102.json").unlink()
    assert "probably stale" not in env.run("mine").stdout  # gh fails: nothing is claimed


def test_stale_can_be_switched_off(env):
    ghkit.install(env)
    ghkit.conf(env, "[stale]\nenabled=false\n")
    ghkit.pr(env, 101, state="MERGED", merged_ago=3600)
    add_item(env, "tracked", f"waiting on {URL.format(n=101)}")
    assert "probably stale" not in env.run("mine").stdout and ghkit.calls(env, "pr view") == 0


# ---- routed item with no PR ----

BRIEF = "Delete the stale branches in the sandbox repo. List them first, keep anything touched in the last month, and remove the rest one by one. " * 2 + "Done-when: they are gone. Output: the list of deleted names."
NUDGE = "peer-a reported done, nothing to check it against. NEXT: check the result yourself, then hubkit work done W1 --note \"<what you checked>\""


def reported_no_pr(env):
    env.bind()
    send(env, "peer-a", BRIEF)
    (item,) = routed_items(env)
    assert item["prs"] == []
    age_routed(env, 40)
    shown = deliver(env, peer_entry("peer-a", "Done, the branches are deleted. Output: old-1, old-2."))
    (item,) = routed_items(env)
    assert item["reported"]
    return shown


def test_a_done_report_on_a_no_pr_item_nudges_in_the_prompt_once_per_stall_interval(env):
    assert NUDGE in reported_no_pr(env)  # the prompt that carried the report already says it
    assert NUDGE not in env.prompt("and now").stdout  # not on every prompt
    f = env.state / "work.json"
    items = json.loads(f.read_text())
    items[0]["nopr_nudged"] -= 16 * 60  # routed.stall_min is 15
    f.write_text(json.dumps(items))
    assert NUDGE in env.prompt("still there?").stdout


def test_the_nudge_is_in_the_status_block_and_the_tick_alert_and_clears_when_the_item_is_done(env):
    reported_no_pr(env)
    assert NUDGE in env.run("status").stdout
    assert "ALERT W1: peer-a reported done, nothing to check it against" in env.run("tick", "--no-notify").stdout
    assert env.run("work", "done", "W1", "--note", "listed the branches: none left").returncode == 0
    assert "nothing to check it against" not in env.run("status").stdout


def test_an_item_with_a_pr_or_a_review_gets_no_such_nudge(env):
    env.bind()
    ghkit.install(env)
    ghkit.pr(env, 101)
    send(env, "peer-a", f"Rebase {URL.format(n=101)} on main and push. Done-when: pushed. Output: the new head sha.")
    age_routed(env, 40)
    deliver(env, peer_entry("peer-a", "Done, pushed. PR 101 is ready."))
    (item,) = routed_items(env)
    assert item["reported"] and item["prs"]
    assert "nothing to check it against" not in env.prompt("what is open").stdout + env.run("status").stdout
