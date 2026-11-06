"""Briefs (required lines, agent cap), repeated commands, whole-suite test runs and cards without a recommendation."""

from test_hubkit import LONG, send
from test_tool_guards import pre


def test_a_brief_missing_its_required_lines_is_warned(env):
    env.bind()
    r = send(env, "peer-a", "Please run the migration checks on the staging data and tell me what you find. " + LONG, "checks")
    assert "no Done-when, Output, Estimate line" in r.stdout
    ok = send(env, "peer-b", "Please run the migration checks. Done-when: all pass. Output: a table. Estimate: 10m\n" + LONG, "checks2")
    assert "has no" not in ok.stdout


def test_a_fifth_agent_is_warned(env):
    env.bind()
    for n in range(4):
        r = send(env, f"peer-{n}", f"Done-when: reviewed. Output: list. Estimate: 5m. check https://github.com/acme/repo/pull/{200 + n}\n{LONG}", f"job {n}")
        assert "agents hold live work" not in r.stdout
    r = send(env, "peer-9", f"Done-when: reviewed. Output: list. Estimate: 5m. check https://github.com/acme/repo/pull/299\n{LONG}", "job 9")
    assert "5 agents hold live work (cap 4)" in r.stdout


def test_the_same_command_a_third_time_is_warned(env):
    env.bind()
    assert pre(env, "Bash", command="make smoke") == ""
    assert pre(env, "Bash", command="make smoke") == ""
    assert "this exact command has now run 3 times" in pre(env, "Bash", command="make smoke")
    assert pre(env, "Bash", command="make other") == ""


def test_a_whole_suite_run_is_warned_and_a_selected_one_is_not(env):
    env.bind()
    assert "runs the whole suite" in pre(env, "Bash", command="pytest -q")
    assert pre(env, "Bash", command="pytest tests/test_one.py -q") == ""
    assert pre(env, "Bash", command="pytest -k smoke -q") == ""
    assert pre(env, "Bash", command="pytest -m i__backend") == ""


def test_a_card_without_a_recommended_first_option_is_warned(env):
    env.bind()
    bad = {"questions": [{"question": "Merge?", "options": [{"label": "No"}, {"label": "Yes (Recommended)"}]}]}
    good = {"questions": [{"question": "Merge?", "options": [{"label": "Yes (Recommended)"}, {"label": "No"}]}]}
    assert "not marked (Recommended)" in pre(env, "AskUserQuestion", **bad)
    assert pre(env, "AskUserQuestion", **good) == ""
