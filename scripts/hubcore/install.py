"""`hubkit skills --install DIR` and `hubkit hooks`: put the skills where the agent tool reads them, and print the hooks block."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import config

HUB_FILES = ("SKILL.md", "LICENSE", "config.example.toml", "intents.example.toml", "references", "docs", "scripts")
EVENTS = (
    ("SessionStart", "", "session", 5),
    ("UserPromptSubmit", "", "prompt", 5),
    ("PostToolUse", "AskUserQuestion", "answer", 5),
    ("PostToolUse", "SendMessage|Agent", "routed", 6),
    ("Stop", "", "stop", 5),
)
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".ruff_cache", ".mypy_cache", "data")


def sources() -> dict[str, list[Path]]:
    """Skill name -> the paths copied into `<dir>/<name>/`, from a clone or from the wheel's package data."""
    root = config.SKILL_DIR
    extra = root if (root / "skills").is_dir() else root.parent
    return {
        "hub": [root / n for n in HUB_FILES if (root / n).exists()],
        "hub-mep": [extra / "skills" / "hub-mep"] if (extra / "skills" / "hub-mep").is_dir() else [extra / "hub-mep"],
        "hubkit-setup": [extra / "skills" / "hubkit-setup"] if (extra / "skills" / "hubkit-setup").is_dir() else [extra / "hubkit-setup"],
    }


def install_skills(target: Path) -> list[str]:
    """Copy the skills into `target`, replacing earlier copies of the same files. Returns one line per skill."""
    out = []
    for name, paths in sources().items():
        dest = target / name
        if dest.resolve() == config.SKILL_DIR.resolve():
            out.append(f"{name}: already the running copy, left alone")
            continue
        for p in paths:
            if not p.exists():
                raise FileNotFoundError(f"packaged skill file missing: {p}")
            if p.is_dir() and p.name == name:  # a whole skill directory
                shutil.copytree(p, dest, dirs_exist_ok=True, ignore=IGNORE)
            elif p.is_dir():
                shutil.copytree(p, dest / p.name, dirs_exist_ok=True, ignore=IGNORE)
            else:
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest / p.name)
        out.append(f"{name}: installed in {dest}")
    return out


def hooks_block(skills_dir: str, merge_guard: bool = False) -> str:
    """The `hooks` JSON for settings.json: each hook runs the hub skill's script, and says so if the script is missing."""
    script = f"{skills_dir.rstrip('/')}/hub/scripts/hubkit"
    hooks: dict[str, list[dict]] = {}
    events = [*EVENTS, *((("PreToolUse", "Bash|TaskStop|AskUserQuestion", "pretool", 5),) if merge_guard else ())]
    for event, matcher, name, timeout in events:
        cmd = f'[ -f "{script}" ] || echo "hubkit: script missing at {script}"; python3 "{script}" hook {name}'
        group: dict = {"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]}
        if matcher:
            group = {"matcher": matcher, **group}
        hooks.setdefault(event, []).append(group)
    return json.dumps({"hooks": hooks}, indent=2)
