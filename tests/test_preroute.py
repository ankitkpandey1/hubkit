"""Before more work goes out: a PR already merged or closed, or already reviewed at its head, is warned about.

The one fake is the `gh` binary (a local stand-in; the tests cannot reach GitHub). Heads are real 40-hex shas.
"""

import json
import time

from test_hubkit import PR1, fake_gh, iso, send

HEAD = "a" * 40
OLD = "b" * 40
WORK = f"Rebase {PR1} on main and push. Done-when: pushed. Output: the new head sha."
ASK = f"Please review {PR1}. Done-when: a verdict is posted. Output: the verdict. Estimate: 10m"


def gh_says(env, state="OPEN", reviews=None, merged_at=None, closed_at=None, head=HEAD, sleep=0):
    if not (env.root / "gh").exists():
        fake_gh(env, head, sleep=sleep)
    (env.root / "gh.json").write_text(json.dumps({
        "headRefOid": head, "state": state, "mergedAt": merged_at, "closedAt": closed_at,
        "author": {"login": "dev"}, "reviews": reviews or [], "commits": [], "statusCheckRollup": []}))


def review(commit, login="rev", state="APPROVED", ago=60):
    return {"submittedAt": iso(time.time() - ago), "state": state, "commit": {"oid": commit}, "author": {"login": login}, "body": ""}


def test_work_for_a_merged_pr_warns_with_the_time_and_a_next(env):
    env.bind()
    gh_says(env, "MERGED", merged_at=iso(time.time() - 600))
    r = send(env, "peer-a", WORK)
    assert r.returncode == 0
    assert "hubkit: tracking" in r.stdout  # still tracked
    assert "PR 101 is already merged at " in r.stdout and "NEXT: read it" in r.stdout


def test_work_for_a_closed_pr_warns(env):
    env.bind()
    gh_says(env, "CLOSED", closed_at=iso(time.time() - 600))
    assert "PR 101 is already closed at " in send(env, "peer-a", WORK).stdout


def test_an_open_unreviewed_pr_gets_no_warning(env):
    env.bind()
    gh_says(env, "OPEN")
    assert "already" not in send(env, "peer-a", WORK).stdout
    assert "already" not in send(env, "peer-b", ASK).stdout


def test_a_review_ask_with_a_counting_review_at_the_head_warns_but_an_older_head_does_not(env):
    env.bind()
    gh_says(env, "OPEN", reviews=[review(HEAD)])
    out = send(env, "peer-r", ASK).stdout
    assert "PR 101 already has a review by rev at its current head" in out and "NEXT: read that review" in out
    gh_says(env, "OPEN", reviews=[review(OLD)])  # the review is on an older head: it does not count
    assert "already has a review" not in send(env, "peer-s", ASK).stdout


def test_a_work_brief_is_not_warned_about_an_existing_review(env):
    env.bind()
    gh_says(env, "OPEN", reviews=[review(HEAD)])
    assert "already has a review" not in send(env, "peer-a", WORK).stdout


def test_no_gh_means_no_warning_and_no_crash(env):
    env.bind()
    env.cfg.write_text(env.cfg.read_text() + f'[tools]\ngh="{env.root / "no-such-gh"}"\n')
    r = send(env, "peer-a", WORK)
    assert r.returncode == 0 and "hubkit: tracking" in r.stdout and "already" not in r.stdout


def test_a_slow_gh_is_cut_off_inside_the_hook_budget(env):
    env.bind()
    gh_says(env, "MERGED", merged_at=iso(time.time() - 60), sleep=8)
    env.cfg.write_text(env.cfg.read_text() + "[routed]\ngh_timeout_s=1\n")
    t = time.time()
    r = send(env, "peer-a", WORK)
    assert r.returncode == 0 and "hubkit: tracking" in r.stdout and "already merged" not in r.stdout
    assert time.time() - t < 6.0
