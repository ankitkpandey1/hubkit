"""Playbooks: how a recurring command is done, replayed on a matching prompt, owed when missing, corrected on a correction."""

from test_hubkit import HOTFIX, stop

STEPS = "1. open the release PRs\n2. have the reviewer review them\n3. merge with --match-head-commit\n"


def save(env, name="release", *extra, steps=STEPS):
    f = env.root / f"{name}.steps"
    f.write_text(steps)
    return env.run("how", "save", name, "--trigger", "release pr", "--trigger", "re:cut a rel\\w+", "--steps-file", str(f), "--quote", "release pr aint it created by bot?", *extra)


def test_save_ls_show_match_round_trip_in_the_config_dir(env):
    assert save(env).returncode == 0
    assert (env.root / "playbooks" / "release.md").exists()
    assert "release: release pr, re:cut a rel" in env.run("how", "ls").stdout
    shown = env.run("how", "show", "release").stdout
    assert "3. merge with --match-head-commit" in shown and "quote: release pr aint it created by bot?" in shown
    assert "PLAYBOOK release: follow these steps" in env.run("how", "match", "please Cut a release now").stdout
    assert env.run("how", "match", "unrelated words").stdout.strip() == "NO PLAYBOOK"


def test_save_overwrites_only_with_replace_and_keeps_a_backup(env):
    save(env)
    r = save(env, steps="1. new\n")
    assert r.returncode == 2 and "--replace" in r.stderr
    assert "3. merge" in env.run("how", "show", "release").stdout
    assert save(env, "release", "--replace", steps="1. new\n").returncode == 0
    assert "1. new" in env.run("how", "show", "release").stdout
    (bak,) = (env.root / "playbooks").glob("release.md.bak-*")
    assert "3. merge" in bak.read_text()


def test_a_matching_prompt_injects_every_matching_playbook_verbatim(env):
    env.bind()
    save(env)
    save(env, "second", steps="1. review blockers only\n")  # same trigger: both match
    out = env.prompt("release pr time").stdout
    assert "PLAYBOOK release: follow these steps\n1. open the release PRs" in out and "PLAYBOOK second: follow these steps\n1. review blockers only" in out
    assert "NO PLAYBOOK" not in out


def test_an_order_with_no_playbook_is_told_to_save_one_and_the_stop_waits_until_it_is_saved(env):
    env.bind()
    out = env.prompt(HOTFIX).stdout
    assert "NO PLAYBOOK for 'we need a hotfix" in out and "hubkit how save" in out
    assert "how save" not in stop(env, last_assistant_message="Working on it.")["reason"]  # nothing was closed yet
    env.run("work", "add", "--kind", "active", "--title", "the hotfix")
    env.run("work", "done", "W1", "--note", "shipped")
    held = stop(env, last_assistant_message="Hotfix shipped.")
    assert "hubkit how save" in held["reason"] and "no playbook" in held["reason"]
    save(env, "hotfix")
    assert "how save" not in stop(env, last_assistant_message="Hotfix shipped.")["reason"]


def test_a_correction_in_a_matched_turn_adds_the_quote_and_says_to_update_it(env):
    env.bind()
    save(env)
    out = env.prompt("release pr again, I told you the bot opens it").stdout
    assert "update playbook release if a step was wrong" in out
    assert "quote: release pr again, I told you the bot opens it" in env.run("how", "show", "release").stdout
