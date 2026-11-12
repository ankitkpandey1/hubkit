"""Standing rules: saved with the user's quote, fired once per PR head or message, never run by hubkit.

The one stand-in is the `gh` binary (tests/ghkit.py); heads are real 40-hex shas and everything else is the real CLI and files."""

import json
import time

import ghkit
from ghkit import HEAD, OLD, URL, review
from test_hubkit import routed_items

QUOTE = "when peer-b approves, merge it"


def add(env, *extra, on="approved", quote=QUOTE, action="merge it"):
    return env.run("standing", "add", "--on", on, "--quote", quote, "--action", action, *extra)


def route(env, n=101):
    r = env.run("work", "add", "--kind", "routed", "--owner", "peer-a", "--title", "rebase onto main", "--pr", URL.format(n=n), "--head", OLD)
    assert r.returncode == 0, r.stderr


def setup(env):
    ghkit.install(env)
    route(env)


def tick(env):
    r = env.run("tick", "--no-notify")
    assert r.returncode == 0, r.stderr
    return r.stdout


def orders(env):
    return [r for r in env.rows("orders.jsonl") if r["op"] == "open"]


def test_add_needs_the_users_quote_and_an_action_and_ls_and_drop_work(env):
    assert add(env, quote="", action="merge it").returncode == 2
    assert env.run("standing", "add", "--on", "approved", "--quote", QUOTE).returncode == 2
    assert add(env, on="sometimes").returncode == 2
    assert add(env, "--word", "x").returncode == 2  # a word scopes an inbound rule
    r = add(env, "--repo", "acme/repo")
    assert r.returncode == 0 and "S1 saved" in r.stdout and "NEXT: hubkit standing ls" in r.stdout
    ls = env.run("standing", "ls").stdout
    assert "S1 on approved (repo acme/repo)" in ls and f'"{QUOTE}"' in ls
    assert env.run("standing", "drop", "S1").returncode == 2  # only the user's words drop a rule
    assert env.run("standing", "drop", "S1", "--quote", "stop merging for me").returncode == 0
    assert "no standing rules" in env.run("standing", "ls").stdout and env.run("standing", "drop", "S1", "--quote", "again please").returncode == 4


def test_an_approval_at_the_head_opens_an_order_with_the_quote_and_a_merge_command_with_the_sha(env):
    setup(env)
    add(env)
    ghkit.pr(env, reviews=[review(HEAD, "peer-b")])
    out = tick(env)
    assert "STANDING S1 fired: peer-b approved " + URL.format(n=101) + " at aaaaaaa" in out
    assert f"NEXT: gh pr merge {URL.format(n=101)} --squash --match-head-commit {HEAD}" in out
    assert f'The user said: "{QUOTE}"' in out
    (o,) = orders(env)
    assert o["kind"] == "merge" and QUOTE in o["quote"] and URL.format(n=101) in o["refs"]
    assert f"hubkit order close {o['id']}" in out  # how to close what was opened
    assert tick(env).count("STANDING") == 0 and len(orders(env)) == 1  # once per head
    assert len(json.loads((env.state / "standing.json").read_text())["fired"]) == 1  # (the tick back-off would hide a second alert, and an identical live order is not opened twice)


def test_a_moved_head_fires_again_only_when_the_approval_is_at_the_new_head(env):
    setup(env)
    add(env)
    ghkit.pr(env, reviews=[review(HEAD, "peer-b")])
    tick(env)
    new = "c" * 40
    ghkit.pr(env, head=new, reviews=[review(HEAD, "peer-b")])  # approval is on the old head: nothing to merge
    assert "STANDING" not in tick(env)
    ghkit.pr(env, head=new, reviews=[review(HEAD, "peer-b"), review(new, "peer-b")])
    out = tick(env)
    assert f"--match-head-commit {new}" in out and len([o for o in orders(env) if o["kind"] == "merge"]) == 2


def test_only_a_counting_approval_fires(env):
    setup(env)
    add(env)
    for reviews, author in (
        ([review(HEAD, "dev")], "dev"),  # the author's own review does not count
        ([review(HEAD, "peer-b", ago=120), review(HEAD, "peer-b", state="CHANGES_REQUESTED", ago=60)], "dev"),  # withdrawn
        ([review(HEAD, "peer-b", state="COMMENTED")], "dev"),
    ):
        ghkit.pr(env, reviews=reviews, author=author)
        assert "STANDING" not in tick(env)
    ghkit.pr(env, state="MERGED", reviews=[review(HEAD, "peer-b")])  # already merged
    assert "STANDING" not in tick(env) and orders(env) == []


def test_red_ci_turns_the_next_into_a_hold_not_a_merge(env):
    setup(env)
    add(env)
    ghkit.pr(env, reviews=[review(HEAD, "peer-b")], ci=["FAILURE"])
    out = tick(env)
    assert "CI is failing at aaaaaaa: do not act on red" in out and "then gh pr merge" in out


def test_scope_repo_and_pr_and_a_named_pr_is_read_even_when_untracked(env):
    ghkit.install(env)
    route(env, 101)
    add(env, "--repo", "other/repo")
    ghkit.pr(env, 101, reviews=[review(HEAD, "peer-b")])
    assert "STANDING" not in tick(env)  # another repo
    add(env, "--pr", URL.format(n=202), quote="when 202 is approved merge it too")
    ghkit.pr(env, 202, reviews=[review(HEAD, "peer-b")])
    out = tick(env)
    assert "S2 fired" in out and "/pull/202" in out and "/pull/101" not in out.split("S2 fired")[1].split("NEXT")[0]


def test_an_action_that_is_not_a_merge_names_no_merge_command_and_cmd_overrides(env):
    setup(env)
    add(env, quote="when peer-b approves, tell peer-a to rebase", action="tell peer-a to rebase")
    add(env, "--cmd", "echo ship {pr} {sha}", quote="when peer-b approves, ship it", action="ship it")
    ghkit.pr(env, reviews=[review(HEAD, "peer-b")])
    out = tick(env)
    assert "gh pr merge" not in out
    assert f"NEXT: tell peer-a to rebase (PR {URL.format(n=101)}, head {HEAD})" in out and f"NEXT: echo ship {URL.format(n=101)} {HEAD}" in out
    w = [i for i in json.loads((env.state / "work.json").read_text()) if i["kind"] == "active"]
    assert len(w) == 2 and QUOTE not in w[0]["note"] and "the user said: when peer-b approves" in w[0]["note"]  # a work item carries the quote


def test_an_expired_rule_does_not_fire_and_until_in_the_past_is_refused(env):
    setup(env)
    assert add(env, "--until", "2001-01-01").returncode == 2
    assert add(env, "--until", "not a date").returncode == 2
    assert add(env, "--until", "2999-01-01").returncode == 0
    f = env.state / "standing.json"
    data = json.loads(f.read_text())
    data["rules"][0]["until"] = time.time() - 60  # the real file, aged
    f.write_text(json.dumps(data))
    ghkit.pr(env, reviews=[review(HEAD, "peer-b")])
    assert "STANDING" not in tick(env) and orders(env) == []
    assert "EXPIRED" in env.run("standing", "ls").stdout


def test_an_inbound_rule_fires_once_per_message_on_person_and_word(env):
    add(env, "--from", "peer-b", "--word", "review", on="inbound", quote="when someone asks for a review, route it to the reviewer", action="route it to the reviewer")
    line = "501 · peer-b · chan-1 · can you review my change? · reply\n"
    out = env.run("inbound", "ingest", stdin=line).stdout
    assert "STANDING S1 fired: peer-b asked in chan-1" in out and "NEXT: route it to the reviewer" in out
    assert 'The user said: "when someone asks for a review, route it to the reviewer"' in out and "hubkit work done W" in out
    assert env.run("inbound", "ingest", stdin=line).stdout.strip() == "nothing new"
    other = "502 · peer-c · chan-1 · can you review my change? · reply\n503 · peer-b · chan-1 · lunch? · reply\n"
    assert "STANDING" not in env.run("inbound", "ingest", stdin=other).stdout  # another person, another word
    active = [i for i in json.loads((env.state / "work.json").read_text()) if i["kind"] == "active"]
    assert len(active) == 1 and "route it to the reviewer" in active[0]["title"]


def test_an_inbound_rule_can_open_an_order_kind(env):
    add(env, "--word", "hotfix", "--kind", "hotfix", on="inbound", quote="any hotfix ask is urgent, order it", action="start the hotfix path")
    env.run("inbound", "ingest", stdin="601 · peer-b · chan-1 · need a hotfix for login · reply\n")
    assert [o["kind"] for o in orders(env)] == ["hotfix"]
    assert add(env, "--kind", "nonsense", on="inbound", quote="a quote here ok", action="x").returncode == 2


def test_the_status_block_counts_rules_and_mine_lists_the_fired_unacted_under_the_hub(env):
    setup(env)
    assert "standing rules" not in env.run("status").stdout
    add(env)
    assert "standing rules: 1 active (hubkit standing ls)" in env.run("status").stdout
    ghkit.pr(env, reviews=[review(HEAD, "peer-b")])
    tick(env)
    assert "standing rules: 1 active (hubkit standing ls); 1 fired, not acted on (hubkit mine)" in env.run("status").stdout
    hub = env.run("mine").stdout.split("ON THE HUB")[1].split("ON PEERS")[0]
    assert "S1 fired" in hub and f"--match-head-commit {HEAD}" in hub
    (o,) = orders(env)
    assert env.run("order", "cancel", o["id"], "--quote", "no need to merge it").returncode == 0
    assert "S1 fired" not in env.run("mine").stdout and "fired, not acted" not in env.run("status").stdout


def test_the_prompt_hook_tells_the_hub_once_what_a_tick_fired(env):
    env.bind()
    setup(env)
    add(env)
    ghkit.pr(env, reviews=[review(HEAD, "peer-b")])
    tick(env)
    first = env.prompt("what is going on").stdout
    assert "hubkit STANDING S1 fired" in first and f"--match-head-commit {HEAD}" in first
    assert "STANDING" not in env.prompt("and now").stdout


def test_a_failing_gh_fires_nothing_and_does_not_break_the_tick(env):
    setup(env)
    add(env)
    assert "STANDING" not in tick(env) and routed_items(env)  # pr_101.json is missing: gh exits 1


def test_the_pr_reads_per_tick_are_capped(env):
    ghkit.install(env)
    ghkit.conf(env, "[standing]\nmax_reads=1\n")
    route(env, 101)
    route(env, 102)
    add(env)
    for n in (101, 102):
        ghkit.pr(env, n, reviews=[review(HEAD, "peer-b")])
    out = tick(env)
    assert out.count("STANDING S1 fired") == 1 and "/pull/101" in out
