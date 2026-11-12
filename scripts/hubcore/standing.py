"""A standing rule the user states in a typed prompt ("from now on ...", "never ...") is captured as a learning, not left in the chat."""

from __future__ import annotations

import re

from . import store
from .redact import redact

RULE_PATTERNS = [
    r"(?i)\bfrom now on\b",
    r"(?i)\b(?:always|never)\s+(?:use|do|run|ask|send|check|merge|push|post|reply|give|say|put|add|start|make|write|skip|open|close|show|leave|call|wait|assume|touch|edit|commit|include|link|tell|report|let|stop|take|route|hold|use)\b.{0,60}",
    r"(?i)\bremember (?:this|that|to)\b",
    r"(?i)\bgoing forward\b",
    r"(?i)\bsave (?:this|that|it) to memory\b",
]
LEARN = "python3 ~/.claude/skills/hub/scripts/learn.py"


def rule_line(intents: dict, sd, text: str, now: float) -> str:
    """One `hubkit RULE` line when a short typed statement (not a question) holds a standing-rule phrase, else empty."""
    cfg = intents.get("corrections", {})
    flat = " ".join(text.split())
    if not flat or flat.endswith("?") or len(flat) > int(cfg.get("rule_max_chars", 300)) or text.count("\n") > 4:
        return ""
    if not any(re.search(p, flat) for p in cfg.get("rule_patterns", RULE_PATTERNS)):
        return ""
    store.append_row(sd / "rule-prompts.jsonl", {"ts": now})
    quote = redact(flat)[0][:200].replace('"', "'")
    return f'hubkit RULE: the user stated a standing rule. NEXT: {LEARN} add --class standing-rule --quote "{quote}" --situation "<when>" --wrong "<what was done instead>" --right "<the rule>" --evidence "<the quote>"'
