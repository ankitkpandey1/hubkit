"""The user's own review debt: re-review owed where the head moved after their last review. Read only, rate limited, quiet without gh.

The one stand-in is the `gh` binary (tests/ghkit.py); heads are real 40-hex shas and everything else is the real CLI and files."""

import json

import ghkit
from ghkit import HEAD, OLD, URL, review


def tick(env):
    r = env.run("tick", "--no-notify")
    assert r.returncode == 0, r.stderr
    return r.stdout


def owed_setup(env, login="me"):
    ghkit.install(env)
    ghkit.conf(env, "", login=login)
    ghkit.search(env, (301, "Fix the login"))
    ghkit.pr(env, 301, reviews=[review(OLD, "me", state="APPROVED", ago=3 * 86400)], commit_ago=2.5 * 86400)


def backdate_sweep(env):
    f = env.state / "review_debt.json"
    data = json.loads(f.read_text())
    data["tried"] -= 3600
    f.write_text(json.dumps(data))


def test_a_pr_the_user_reviewed_whose_head_moved_is_debt_in_mine_digest_and_one_alert(env):
    owed_setup(env)
    out = tick(env)
    assert "the user owes a re-review: " + URL.format(n=301) in out and "moved to aaaaaaa after their approved review at bbbbbbb" in out
    assert "NEXT: tell the user in the next report" in out
    on_user = env.run("mine").stdout.split("ON THE USER")[1].split("ON THE HUB")[0]
    assert URL.format(n=301) in on_user and "(re-review) Fix the login" in on_user and "waiting 2d" in on_user
    assert f"waits on you: re-review {URL.format(n=301)} (2d)" in env.run("digest").stdout
    backdate_sweep(env)
    assert "re-review" not in tick(env)  # alerted once for this head
    assert len(json.loads((env.state / "review_debt.json").read_text())["alerted"]) == 1  # (the tick back-off alone would hide a second alert)
    ghkit.pr(env, 301, head="c" * 40, reviews=[review(OLD, "me", ago=3 * 86400)])
    backdate_sweep(env)
    assert "moved to ccccccc" in tick(env)  # a new head is a new debt
    assert env.rows("orders.jsonl") == [] and not (env.state / "work.json").exists()  # read only: no order, no work item


def test_a_review_at_the_current_head_or_no_review_by_the_user_is_no_debt(env):
    owed_setup(env)
    ghkit.pr(env, 301, reviews=[review(HEAD, "me")])
    assert "re-review" not in tick(env) and "re-review" not in env.run("mine").stdout
    ghkit.pr(env, 301, reviews=[review(OLD, "someone-else")])  # never reviewed by the user
    backdate_sweep(env)
    assert "re-review" not in tick(env)
    ghkit.pr(env, 301, reviews=[review(OLD, "me"), review(HEAD, "me", state="COMMENTED")])  # the latest review is at the head
    backdate_sweep(env)
    assert "re-review" not in tick(env)


def test_the_sweep_is_rate_limited_and_off_without_a_login_or_when_switched_off(env):
    owed_setup(env)
    tick(env)
    n = ghkit.calls(env, "search")
    assert n == 1
    tick(env)
    assert ghkit.calls(env, "search") == 1  # inside every_min
    backdate_sweep(env)
    tick(env)
    assert ghkit.calls(env, "search") == 2


def test_no_login_means_no_sweep_and_enabled_false_stops_it(env):
    ghkit.install(env)
    ghkit.search(env, (301, "x"))
    tick(env)
    assert ghkit.calls(env, "search") == 0 and not (env.state / "review_debt.json").exists()
    ghkit.conf(env, "[review_debt]\nenabled=false\n", login="me")
    tick(env)
    assert ghkit.calls(env, "search") == 0


def test_a_failing_or_missing_gh_is_silent_and_keeps_the_last_known_debt(env):
    owed_setup(env)
    tick(env)
    (env.root / "search.json").unlink()  # gh now exits 1
    before = json.loads((env.state / "review_debt.json").read_text())["tried"]
    backdate_sweep(env)
    assert "re-review" not in tick(env)
    after = json.loads((env.state / "review_debt.json").read_text())
    assert after["tried"] > before - 3600 + 1 and after["items"]  # the try is recorded (no hammering) and the last known debt stays
    assert "(re-review)" in env.run("mine").stdout
    env.cfg.write_text(env.cfg.read_text().replace(f'gh="{env.root / "gh"}"', f'gh="{env.root / "no-such-gh"}"'))
    backdate_sweep(env)
    r = env.run("tick", "--no-notify")
    assert r.returncode == 0 and "Traceback" not in r.stderr and "re-review" not in r.stdout
