"""A PR called ready for the user's merge must be mergeable on GitHub, and `routed.court` names whose court it is in.

The only stand-in is `gh` (tests/test_hubkit.py `fake_gh`): the tests cannot reach GitHub."""

import json
import sys

from conftest import SCRIPTS
from test_hubkit import fake_gh, review, stop

sys.path.insert(0, str(SCRIPTS))
from hubcore import routed  # noqa: E402

H = "c" * 40
CLAIM = "https://github.com/acme/repo/pull/101 is ready to merge."


def gh(env, **over):
    fake_gh(env, head=H)
    env.cfg.write_text(env.cfg.read_text() + '[routed]\nrepo="acme/repo"\n')
    env.bind()
    data = {"headRefOid": H, "author": {"login": "pr-author"}, "reviews": [review(H)], "state": "OPEN",
            "reviewDecision": "APPROVED", "mergeStateStatus": "CLEAN", "reviewRequests": [], "statusCheckRollup": [{"conclusion": "SUCCESS"}], **over}
    (env.root / "gh.json").write_text(json.dumps(data))


def info(**over):
    base = {"head": H, "state": "OPEN", "ci": "green", "decision": "APPROVED", "merge_state": "CLEAN", "requested": [], "reviews": [], "author": "a"}
    return {**base, **over}


def test_pr_info_reads_the_review_decision_merge_state_and_requested_reviewers(env):
    gh(env, reviewDecision="REVIEW_REQUIRED", mergeStateStatus="BLOCKED", reviewRequests=[{"login": "rev-er"}, {"slug": "core-team"}])
    got = routed.pr_info({"tools": {"gh": str(env.root / "gh")}}, "https://github.com/acme/repo/pull/101", 5)
    assert got and got["decision"] == "REVIEW_REQUIRED" and got["merge_state"] == "BLOCKED" and got["requested"] == ["rev-er", "core-team"]
    assert got["head"] == H and got["ci"] == "green"  # the earlier keys are unchanged


def test_court_names_whose_turn_it_is():
    assert routed.court(info()) == "user"
    assert routed.court(None) == "unknown" and routed.court({}) == "unknown"
    assert routed.court(info(state="MERGED")) == "done"
    assert routed.court(info(ci="failing")) == "ci"
    assert routed.court(info(decision="CHANGES_REQUESTED")) == "author"
    assert routed.court(info(merge_state="BEHIND")) == "author"
    assert routed.court(info(decision="REVIEW_REQUIRED", requested=["rev-er"])) == "reviewer:rev-er"
    assert routed.court(info(decision="REVIEW_REQUIRED")) == "reviewer:any"
    assert routed.court(info(merge_state="BLOCKED", requested=["rev-er"])) == "reviewer:rev-er"
    assert routed.court(info(merge_state="BLOCKED")) == "unknown"
    assert routed.court(info(ci="pending")) == "ci"


def test_ready_to_merge_is_held_while_changes_are_requested_and_names_who_asked(env):
    gh(env, reviewDecision="CHANGES_REQUESTED", reviews=[review(H, state="CHANGES_REQUESTED", login="other-rev")])
    held = stop(env, last_assistant_message=CLAIM)
    assert held and "review decision CHANGES_REQUESTED" in held["reason"] and "other-rev requested changes" in held["reason"]
    assert "say who it waits on, or leave it off the user's list" in held["reason"]


def test_ready_to_merge_is_held_when_a_different_approver_is_required(env):
    gh(env, reviewDecision="REVIEW_REQUIRED", mergeStateStatus="BLOCKED", reviewRequests=[{"login": "approver-x"}])
    held = stop(env, last_assistant_message="PR https://github.com/acme/repo/pull/101 needs your word to merge.")
    assert held and "merge state BLOCKED" in held["reason"] and "approver-x" in held["reason"] and "reviewers' court" in held["reason"]


def test_ready_to_merge_is_held_when_ci_fails_or_the_branch_is_behind(env):
    gh(env, statusCheckRollup=[{"conclusion": "FAILURE"}])
    held = stop(env, last_assistant_message=CLAIM)
    assert held and "CI failing" in held["reason"] and "CI's court" in held["reason"]
    gh_json = env.root / "gh.json"
    data = json.loads(gh_json.read_text())
    gh_json.write_text(json.dumps({**data, "statusCheckRollup": [{"conclusion": "SUCCESS"}], "mergeStateStatus": "BEHIND"}))
    held = stop(env, last_assistant_message=CLAIM)
    assert held and "merge state BEHIND" in held["reason"] and "author's court" in held["reason"]


def test_ready_to_merge_passes_when_github_agrees_and_negated_or_other_sentences_pass(env):
    gh(env)
    assert stop(env, last_assistant_message=CLAIM) is None
    gh_json = env.root / "gh.json"
    gh_json.write_text(json.dumps({**json.loads(gh_json.read_text()), "reviewDecision": "CHANGES_REQUESTED"}))
    assert stop(env, last_assistant_message="https://github.com/acme/repo/pull/101 is not ready to merge.") is None
    assert stop(env, last_assistant_message="The change is ready to merge.") is None  # no PR named
    assert stop(env, last_assistant_message="https://github.com/acme/repo/pull/101 has changes requested.") is None


def test_unreadable_github_proves_nothing(env):
    gh(env)
    (env.root / "gh.json").unlink()  # the stand-in gh now exits 1
    assert stop(env, last_assistant_message=CLAIM) is None
