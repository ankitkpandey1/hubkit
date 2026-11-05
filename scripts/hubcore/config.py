"""Config loading. All user-specific values come from one TOML file.

Search order: $HUBKIT_CONFIG, $XDG_CONFIG_HOME/hubkit/config.toml, ~/.config/hubkit/config.toml.
Missing file means built-in defaults (generic, nothing personal).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent.parent
ENV_PREFIX = "HUBKIT"

DEFAULTS: dict = {
    "user": {"name": "the user", "timezone": "", "github_login": ""},
    "hub": {"name": "hub"},
    "paths": {
        "state_dir": "",  # default: $XDG_STATE_HOME/hubkit
        "intents": "",  # default: <config dir>/intents.toml, else skill example
        "overlay": "",  # default: <config dir>/overlay.md
        "learnings": "",  # default: <state dir>/learnings.md
        "sessions_dir": "~/.claude/sessions",
        "transcripts_glob": "~/.claude/projects/*/*.jsonl",
        "extensions_dir": "",  # default: <config dir>/ext
    },
    "tools": {"mep": "mep", "notify_cmd": "", "gh": "gh"},
    "routed": {"stall_min": 15, "min_chars": 200, "skip_owners": ["inbox"], "gh_timeout_s": 10,
               "repo": "", "reply_window_min": 60, "queued_wait_min": 60, "owner_logins": {}, "skip_briefs": ["mep watch", "inbox watch"],
        "activity_globs": [
            "~/.claude/projects/*/*/subagents/agent-{id}.jsonl",
            "/tmp/claude-*/*/*/tasks/{id}.output",
        ], "reviewer_logins": [], "review_words": ["review"], "review_stall_min": 5, "review_overdue_min": 10},
    "mep": {"enabled": True, "timeout_s": 30},
    "watchdog": {"transcript_scan_hours": 24, "cron_stale_min": 15},
    "stop_guard": {"phrases": ["waits for your go", "say go", "want me to", "shall i"]},
    "private": {"terms": []},
}


def config_dir() -> Path:
    """Directory holding the user's config files ($XDG_CONFIG_HOME/hubkit)."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "hubkit"


def config_path() -> Path:
    """Path of config.toml: $HUBKIT_CONFIG, else <config dir>/config.toml."""
    env = os.environ.get(f"{ENV_PREFIX}_CONFIG")
    return Path(env).expanduser() if env else config_dir() / "config.toml"


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, val in over.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], val)
        else:
            out[key] = val
    return out


def load() -> dict:
    """Defaults merged with the user's config.toml (a missing file means defaults)."""
    path = config_path()
    user: dict = {}
    if path.exists():
        user = tomllib.loads(path.read_text(encoding="utf-8"))
    cfg = _merge(DEFAULTS, user)
    cfg["_config_path"] = str(path)
    cfg["_config_dir"] = str(path.parent)
    return cfg


def state_dir(cfg: dict) -> Path:
    """State directory: $HUBKIT_STATE, else paths.state_dir, else $XDG_STATE_HOME/hubkit."""
    env = os.environ.get(f"{ENV_PREFIX}_STATE")
    if env:
        return Path(env).expanduser()
    if cfg["paths"]["state_dir"]:
        return Path(cfg["paths"]["state_dir"]).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "hubkit"


def path_of(cfg: dict, key: str, default: Path) -> Path:
    """A path from [paths] with ~ expanded, or `default` when unset."""
    val = cfg["paths"].get(key, "")
    return Path(val).expanduser() if val else default


def sessions_dir(cfg: dict) -> Path:
    """Where Claude Code writes one JSON file per running session."""
    env = os.environ.get(f"{ENV_PREFIX}_SESSIONS")
    return Path(env).expanduser() if env else Path(cfg["paths"]["sessions_dir"]).expanduser()


def intents_path(cfg: dict) -> Path:
    """The user's intents.toml, else the intents.example.toml shipped with the skill."""
    mine = path_of(cfg, "intents", Path(cfg["_config_dir"]) / "intents.toml")
    return mine if mine.exists() else SKILL_DIR / "intents.example.toml"
