"""Review tracking: merged PRs close, a re-brief moves the PRs, the next step after a review names an action, CI at the head."""

import json

from test_hubkit import PR1, fake_gh, review, routed_items, send


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
