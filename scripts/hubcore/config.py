"""Config loading. All user-specific values come from one TOML file.

Search order: $HUBKIT_CONFIG, $XDG_CONFIG_HOME/hubkit/config.toml, ~/.config/hubkit/config.toml.
Missing file means built-in defaults (generic, nothing personal).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# A clone or an installed skill directory sits two levels above hubcore; the wheel carries the same tree as package data.
SKILL_DIR = _HERE / "data" / "hub" if (_HERE / "data" / "hub").is_dir() else _HERE.parent.parent
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
    "tools": {"mep": "mep", "notify_cmd": "", "gh": "gh", "send_regex": "", "draft_regex": "", "discard_regex": ""},
    "routed": {"stall_min": 15, "min_chars": 200, "max_agents": 4, "skip_owners": ["inbox"], "gh_timeout_s": 10,
               "repo": "", "reply_window_min": 60, "queued_wait_min": 60, "owner_logins": {}, "skip_briefs": ["mep watch", "inbox watch"],
        "activity_globs": [
            "~/.claude/projects/*/*/subagents/agent-{id}.jsonl",
            "/tmp/claude-*/*/*/tasks/{id}.output",
        ], "reviewer_logins": [], "review_words": ["review"], "review_stall_min": 5, "review_overdue_min": 10,
               "done_when_markers": ["Done-when:", "Done when:"], "require_done_when": "warn"},
    "requests": {"stall_min": 60, "overdue_min": 120, "floor_min": 10, "overdue_factor": 2.0, "similarity": 0.6, "max_open": 20, "title_chars": 120, "block_lines": 5,
                 "text_chars": 600, "max_parts": 8, "false_min": 3, "false_ratio": 0.5, "window_days": 14},
    "slow": {"min_samples": 3, "order_default_min": 30, "floor_min": 2, "tool_min": 2, "scan_kb": 2048},
    "watcher": {"stale_agent_min": 30, "reply_owed_min": 60, "reply_floor_min": 10, "reply_min_samples": 3, "verify_stale_min": 180, "stuck_min": 30, "unhandled_min": 10, "waiting_min": 3, "active_s": 120, "watch_grace_min": 5, "notify_user": True},
    "mep": {"enabled": True, "timeout_s": 30},
    "watchdog": {"transcript_scan_hours": 24, "cron_stale_min": 15},
    "think": {"decisions": ""},  # path of the active slug's decisions.md; its last lines go in the status block
    "stop_guard": {"phrases": ["waits for your go", "say go", "want me to", "shall i"]},
    "private": {"terms": []},
    "report": {"zone_check": True, "utc_markers": ["UTC", "GMT"], "max_lines": 0, "max_code_lines": 0, "eod_time": "", "eod_check_min": 30},  # a final message with a UTC time while the user has another zone is held
    "learn": {"overlay_max_lines": 200},
    "gate": {"false_alarm_min": 3, "false_alarm_ratio": 0.3, "window_days": 14, "restore_window_min": 30, "never_downgrade": ["verdict", "ci", "merge-state"]},
    "guards": {"needs_order": [], "needs_order_words": ["ticket", "issue", "jira", "file a", "open a", "create a"],
               "schedule_tools": ["CronCreate", "ScheduleWakeup"], "schedule_regexes": [r"(?i)\bcrontab\b", r"(?i)\bwhile\s+(?:true|:)\b[^\n]*\bsleep\b", r"(?i)\bwatch\s+-n\b"],
               "schedule_words": ["schedule", "loop", "watch", "every", "remind", "cron", "poll", "recurring", "keep an eye", "check again"], "window_min": 60,
               "inline_edits": "warn", "inline_ok_paths": ["/tmp/*", "*.md"], "hosts": [], "review_ask_window_min": 240},
    "accounts": {"rules": []},  # [[accounts.rules]] match = "<regex on the command>", require = "<regex that must also be present>", hint = "<text>"
    "roster": {"stale_min": 120, "correction_window_min": 10,
               "reviewer_words": ["reviewer", "code-review only", "code review only", "review only"],
               "worker_words": ["worker", "no role assigned"],
               "busy_words": ["busy", "not free", "not available", "occupied"],
               "free_words": ["idle", "free worker", "i'm free", "i am free", "free for", "ready for work", "ready for the next"]},
    "standing": {"approved_cmd": "gh pr merge {pr} --squash --match-head-commit {sha}", "estimate_min": 10, "gh_timeout_s": 10, "max_reads": 20},
    "review_debt": {"enabled": "auto", "every_min": 30, "gh_timeout_s": 15, "max_prs": 30, "max_reads": 10},  # "auto": on when user.github_login is set
    "stale": {"enabled": True, "max_reads": 8, "gh_timeout_s": 5},
    "threads": {"min_shared": 2, "root_share": 0.5},  # automatic linking of work to the ask it serves
    "roles": {},  # [roles.<role>] brief_must_include / report_must_include = ["phrase or regex", ...]
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
