"""Warn-only checks before a tool call (posts, banned commands, stopping an agent) and the card-tool rule for asks."""

import json

from test_hubkit import stop
from test_lessons import prompts, transcript

BANNED = (
    "rm " + "-rf build/",
    "git reset " + "--hard HEAD~1",
    "git push " + "--force origin x",
    "p" + "kill -f worker",
    "git " + "stash",
    "git " + "clean -fd",
)


def pre(env, tool, **tool_input):
    r = env.hook(
        "pretool",
        {
            "hook_event_name": "PreToolUse",
            "session_id": "sess-hub",
            "tool_name": tool,
            "tool_input": tool_input,
        },
    )
    assert (
        r.returncode == 0 and "permissionDecision" not in r.stdout
    )  # warns, never denies
    return (
        json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        if r.stdout.strip()
        else ""
    )


def test_a_post_without_a_typed_yes_is_warned_and_with_one_is_not(env):
    env.bind()
    assert "no typed yes" in pre(
        env, "Bash", command='mep send --to dana "the review is done"'
    )
    assert "no typed yes" in pre(env, "Bash", command="gh pr comment 101 --body 'lgtm'")
    prompts(env, "yes send it")
    assert pre(env, "Bash", command='mep send --to dana "the review is done"') == ""
    assert pre(env, "Bash", command="gh pr view 101") == ""


def test_banned_commands_are_warned(env):
    env.bind()
    for cmd in BANNED:
        assert "banned command" in pre(env, "Bash", command=cmd), cmd
    for cmd in ("rm build/one.txt", "git push origin x", "git status"):
        assert "banned" not in pre(env, "Bash", command=cmd), cmd


def test_stopping_an_agent_is_warned_unless_he_said_to_stop_it(env):
    env.bind()
    assert "stopping a running agent" in pre(env, "TaskStop", task_id="abc")
    prompts(env, "kill that agent, it is stuck")
    assert pre(env, "TaskStop", task_id="abc") == ""


def test_a_decision_asked_in_prose_must_go_through_the_card_tool(env):
    env.bind()
    env.run(
        "work",
        "add",
        "--kind",
        "ask",
        "--title",
        "Merge #101 now?",
        "--note",
        "blocks the release",
    )
    q = {"last_assistant_message": "Which option?\nShall I merge it?"}
    held = stop(env, transcript_path=transcript(env, ["Read"]), **q)
    assert held and "asked the user in prose" in held["reason"]
    assert stop(env, transcript_path=transcript(env, ["AskUserQuestion"]), **q) is None


def test_a_report_on_built_work_is_held_while_it_is_only_on_this_machine(env, tmp_path):
    import subprocess

    def git(*a, cwd):
        subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin"})

    remote, work = tmp_path / "remote.git", tmp_path / "work"
    git("init", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    git("clone", str(remote), str(work), cwd=tmp_path)
    (work / "a.txt").write_text("one\n")
    git("add", "a.txt", cwd=work)
    git("commit", "-m", "one", cwd=work)
    git("push", "-u", "origin", "HEAD:main", cwd=work)
    env.bind()
    msg = {"last_assistant_message": "The fix is built and ready.", "cwd": str(work)}
    assert stop(env, **msg) is None  # clean and pushed
    (work / "a.txt").write_text("two\n")
    assert "not committed" in stop(env, **msg)["reason"]
    git("commit", "-am", "two", cwd=work)
    assert "1 commit(s) not pushed" in stop(env, **msg)["reason"]
    assert stop(env, last_assistant_message="The fix is built but not pushed yet.", cwd=str(work)) is None
    git("push", "origin", "HEAD:main", cwd=work)
    assert stop(env, **msg) is None


def test_a_claim_that_tests_pass_needs_a_command_run_this_turn(env):
    env.bind()
    msg = {"last_assistant_message": "The tests pass and the fix works."}
    held = stop(env, transcript_path=transcript(env, ["Read"]), **msg)
    assert held and "ran no command" in held["reason"]
    assert stop(env, transcript_path=transcript(env, ["Bash"]), **msg) is None


def test_a_report_naming_a_pr_number_with_no_link_is_held(env):
    env.bind()
    held = stop(env, last_assistant_message="PR #7457 is merged and #7430 waits.", transcript_path=transcript(env, ["Bash"]))
    assert held and "with no link" in held["reason"]
    linked = "PR #7457 is merged: https://github.com/acme/repo/pull/7457"
    assert stop(env, last_assistant_message=linked, transcript_path=transcript(env, ["Bash"])) is None


def test_an_approval_relayed_while_ci_is_red_at_the_head_is_held(env):
    from test_hubkit import fake_gh, review

    fake_gh(env, head="aaa1111")
    env.cfg.write_text(env.cfg.read_text() + '[routed]\nrepo="acme/repo"\n')
    env.bind()
    data = {"headRefOid": "aaa1111", "author": {"login": "pr-author"}, "reviews": [review("aaa1111")], "statusCheckRollup": [{"conclusion": "FAILURE"}]}
    (env.root / "gh.json").write_text(json.dumps(data))
    held = stop(env, last_assistant_message="#101 is approved.")
    assert held and "CI at the head is not green" in held["reason"]
    data["statusCheckRollup"] = [{"conclusion": "SUCCESS"}]
    (env.root / "gh.json").write_text(json.dumps(data))
    assert stop(env, last_assistant_message="#101 is approved.") is None
