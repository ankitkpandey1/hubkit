"""`hubkit skills --install DIR` and `hubkit hooks`, from a clone and from a real built wheel."""

import json
import shutil
import subprocess
import sys

import pytest
from conftest import SKILL


def test_skills_install_lays_down_three_working_skills(env, tmp_path):
    target = tmp_path / "skills"
    r = env.run("skills", "--install", str(target))
    assert r.returncode == 0, r.stderr
    for name in ("hub", "hub-mep", "hubkit-setup"):
        assert (target / name / "SKILL.md").read_text(encoding="utf-8").startswith("---")
    assert (target / "hub" / "scripts" / "hubcore" / "cli.py").exists() and (target / "hub" / "intents.example.toml").exists()
    assert not list(target.rglob("__pycache__"))
    copy = subprocess.run([sys.executable, str(target / "hub" / "scripts" / "hubkit"), "doctor"], capture_output=True, text=True, env=env.env(), timeout=30)
    assert copy.returncode == 0 and "bind: none or dead" in copy.stdout  # the copy runs on its own


def test_skills_install_twice_refreshes_the_copies(env, tmp_path):
    target = tmp_path / "skills"
    env.run("skills", "--install", str(target))
    (target / "hub" / "SKILL.md").write_text("stale", encoding="utf-8")
    assert env.run("skills", "--install", str(target)).returncode == 0
    assert (target / "hub" / "SKILL.md").read_text(encoding="utf-8").startswith("---")


def test_skills_install_into_the_running_copy_leaves_it_alone(env):
    r = env.run("skills", "--install", str(SKILL.parent))
    assert r.returncode == 0 and "already the running copy" in r.stdout.splitlines()[0] or SKILL.name != "hub"


def test_hooks_block_is_valid_settings_json_with_every_event(env):
    block = json.loads(env.run("hooks", "--skills-dir", "$HOME/.claude/skills").stdout)["hooks"]
    assert set(block) == {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"}
    cmds = [h["command"] for g in block["PostToolUse"] for h in g["hooks"]]
    assert any("hook answer" in c for c in cmds) and any("hook routed" in c for c in cmds) and any("hook skill" in c for c in cmds)
    assert all("$HOME/.claude/skills/hub/scripts/hubkit" in h["command"] for gs in block.values() for g in gs for h in g["hooks"])
    guarded = json.loads(env.run("hooks", "--merge-guard").stdout)["hooks"]
    assert guarded["PreToolUse"][0]["matcher"] == "Bash|TaskStop|AskUserQuestion" and "hook pretool" in guarded["PreToolUse"][0]["hooks"][0]["command"]


@pytest.mark.skipif(not shutil.which("uv"), reason="needs uv to build and install a wheel")
def test_a_built_wheel_installs_and_lays_down_the_skills(tmp_path):
    wheels = tmp_path / "wheels"
    subprocess.run(["uv", "build", "--wheel", "-o", str(wheels), str(SKILL)], check=True, capture_output=True, timeout=300)
    venv = tmp_path / "venv"
    subprocess.run(["uv", "venv", "-q", "--python", sys.executable, str(venv)], check=True, capture_output=True, timeout=120)
    exe = venv / ("Scripts" if sys.platform == "win32" else "bin")
    subprocess.run(["uv", "pip", "install", "-q", "--python", str(exe / "python"), str(next(wheels.glob("*.whl")))], check=True, capture_output=True, timeout=300)
    target = tmp_path / "skills"
    r = subprocess.run([str(exe / "hubkit"), "skills", "--install", str(target)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert (target / "hub" / "scripts" / "hubkit").exists() and (target / "hubkit-setup" / "SKILL.md").exists()


def test_doctor_warns_when_the_users_intents_lack_a_section_the_example_has(env):
    from conftest import INTENTS

    text = INTENTS.read_text(encoding="utf-8")
    assert "[stop_gate]" in text
    head, _, rest = text.partition("[stop_gate]")
    old = head + "[" + rest.partition("\n[")[2]
    mine = env.root / "intents.toml"
    mine.write_text(old, encoding="utf-8")
    env.cfg.write_text(env.cfg.read_text().replace(f'intents="{INTENTS}"', f'intents="{mine}"'))
    r = env.run("doctor")
    assert "WARNING intents.toml lacks" in r.stdout and "[stop_gate]" in r.stdout and r.returncode == 0
    mine.write_text(text, encoding="utf-8")
    assert "WARNING intents.toml" not in env.run("doctor").stdout


def test_doctor_warns_about_a_hook_registered_twice_and_an_eod_that_is_never_scheduled(env):
    from test_hubkit import fake_crontab

    cmd = "python3 ~/.claude/skills/hub/scripts/hubkit hook prompt"
    twice = {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": cmd}]}, {"hooks": [{"type": "command", "command": cmd}]}]}}
    once = {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": cmd}]}]}}
    settings = env.root / "settings.json"
    scheduled = fake_crontab(env, "0 21 * * * python3 ~/.claude/skills/hub/scripts/hubkit eod\n")
    settings.write_text(json.dumps(twice))
    out = env.run("doctor", HUBKIT_SETTINGS=str(settings), **scheduled).stdout
    assert "more than once for: prompt" in out and "runs `hubkit eod`" not in out
    settings.write_text(json.dumps(once))
    unscheduled = fake_crontab(env, "*/5 * * * * python3 ~/.claude/skills/hub/scripts/hubkit tick\n")
    out = env.run("doctor", HUBKIT_SETTINGS=str(settings), **unscheduled).stdout
    assert "more than once" not in out and "runs `hubkit eod`" in out
