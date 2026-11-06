"""Review tracking: merged PRs close, a re-brief moves the PRs, the next step after a review names an action, CI at the head."""

import json

from test_hubkit import PR1, PR2, fake_gh, review, routed_items, send


def gh_json(env, head, reviews=None, **extra):
    (env.root / "gh.json").write_text(json.dumps({"headRefOid": head, "author": {"login": "pr-author"}, "reviews": reviews or [], **extra}))


def test_a_merged_pr_closes_its_review_item_instead_of_staying_overdue(env):
    fake_gh(env, head="aaa1111")
    env.bind()
    send(env, "peer-a", f"Full gated review of {PR1}", "review 101")
    gh_json(env, "aaa1111", state="MERGED")
    out = env.run("tick", "--no-notify").stdout
    assert routed_items(env) == [] and "MERGED" in out


def test_a_rebrief_moves_the_prs_off_the_old_owner(env):
    fake_gh(env, head="aaa1111")
    env.bind()
    send(env, "peer-a", f"Full gated review of {PR1}", "review 101")
    send(env, "peer-b", f"Take over the review of {PR1}. Done-when: reviewed", "take over 101")
    assert [i["owner"] for i in routed_items(env)] == ["peer-b"]


def test_an_approval_names_the_concrete_next_step_and_ci_at_the_head(env):
    fake_gh(env, head="aaa1111")
    env.bind()
    send(env, "peer-a", f"Full gated review of {PR1}", "review 101")
    gh_json(env, "aaa1111", [review("aaa1111")], statusCheckRollup=[{"conclusion": "SUCCESS"}])
    out = env.run("tick", "--no-notify").stdout
    assert "CI green" in out and "--match-head-commit aaa1111" in out


def test_an_approval_on_red_ci_says_not_to_merge(env):
    fake_gh(env, head="aaa1111")
    env.bind()
    send(env, "peer-a", f"Full gated review of {PR1}", "review 101")
    gh_json(env, "aaa1111", [review("aaa1111")], statusCheckRollup=[{"conclusion": "FAILURE"}])
    out = env.run("tick", "--no-notify").stdout
    assert "CI is failing" in out and "do not merge on red" in out


def test_an_owners_pushes_to_a_fix_job_are_not_a_stalled_head_but_a_reviews_moved_head_still_is(env):
    fake_gh(env, head="aaa1111")
    env.bind()
    send(env, "peer-fix", f"Fix the gaps from the audit and update the tests. Done-when: tests pass. Output: the PR {PR1}. Estimate: 30m", "audit work")
    send(env, "peer-rev", f"Full gated review of {PR2}", "review 102")
    gh_json(env, "bbb2222")
    out = env.run("tick", "--no-notify").stdout
    items = {i["owner"]: i for i in routed_items(env)}
    assert items["peer-fix"]["head_moved"] is None and items["peer-fix"]["heads"][PR1] == "bbb2222"
    assert items["peer-rev"]["head_moved"] and "ALERT" in out


def test_a_fix_job_titled_with_review_findings_has_no_review_clock_but_a_real_review_does(env):
    from test_hubkit import age_routed

    fake_gh(env, head="aaa1111")
    env.bind()
    fix = f"Fix the findings from the review of {PR1}. Done-when: tests pass. Output: the PR updated, ready for re-review of {PR1}.\nEstimate: 45m"
    send(env, "peer-fix", fix, "Fix #101 review findings")
    assert routed_items(env)[0]["review"] is False
    age_routed(env, 29)
    assert "past the hard limit" not in env.prompt("carry on").stdout
    send(env, "peer-rev", f"Full gated review of {PR2}. Done-when: a verdict. Output: the verdict", "review 102")
    age_routed(env, 29)
    out = env.prompt("carry on").stdout
    assert "peer-rev" in out and "past the hard limit" in out and out.count("past the hard limit") == 1


def test_a_reviews_own_stated_estimate_sets_its_hard_limit(env):
    from test_hubkit import age_routed

    fake_gh(env, head="aaa1111")
    env.bind()
    send(env, "peer-rev", f"Full gated review of {PR1}. Done-when: a verdict. Output: the verdict.\nEstimate: 60m", "review 101")
    age_routed(env, 29)
    assert "past the hard limit" not in env.prompt("carry on").stdout
    age_routed(env, 40)
    assert "past the hard limit" in env.prompt("carry on").stdout
