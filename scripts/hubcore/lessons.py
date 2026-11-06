"""Checks that come from repeated corrections: re-reading the user's asks, unchecked claims, briefs that forbid the
fix, and an unasked merge. Every check is a warning or a Stop reason; none denies a tool call."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import store
from .orders import Orders

# ---- asks: the user's recent words, one line each ----

DONE_WORDS = re.compile(r"(?i)\b(done|finished|complete[d]?|all set|shipped)\b")
SPAN = re.compile(r"^(\d+)\s*([mhd])$")


def parse_span(text: str) -> float:
    """`30m`, `12h` or `2d` as seconds; raises ValueError otherwise."""
    m = SPAN.match(text.strip().lower())
    if not m:
        raise ValueError(f"--since takes a number and m, h or d (for example 12h), not {text!r}")
    return int(m[1]) * {"m": 60, "h": 3600, "d": 86400}[m[2]]


def recent_prompts(cfg: dict, since_s: float, now: float | None = None) -> list[dict]:
    """The human prompts held in recent-prompts.jsonl within the span, oldest first."""
    now = now or time.time()
    rows = store.read_rows(store.state(cfg) / "recent-prompts.jsonl")[0]
    return [r for r in rows if now - r["ts"] <= since_s]


def asks_lines(cfg: dict, span: str) -> list[str]:
    """`HH:MM  words` per prompt, newest last. Reading them marks the asks as swept for the Stop gate."""
    rows = recent_prompts(cfg, parse_span(span))
    store.write_atomic(store.state(cfg) / "asks-swept", str(time.time()))
    return [f"{store.clock(cfg, r['ts'])}  {r['text']}" for r in rows] or ["(no prompts in that span)"]


def _last_work_done(cfg: dict, now: float) -> float:
    last = 0.0
    for back in (0, 1):
        day = store.day_of(cfg, now - back * 86400)
        for r in store.read_rows(store.state(cfg) / "archive" / f"archive_{day}.jsonl")[0]:
            if r.get("type") == "work" and r.get("outcome") == "done":
                last = max(last, float(r["ts"]))
    return last


def unswept_asks(cfg: dict, last: str, now: float | None = None) -> str | None:
    """A reason when the message says done while the user's newest prompt is newer than the last `work done` and
    the last `hubkit asks ls`: the requirements were not re-read."""
    now = now or time.time()
    if not DONE_WORDS.search(last):
        return None
    prompts = recent_prompts(cfg, 86400, now)
    if not prompts:
        return None
    try:
        swept = float((store.state(cfg) / "asks-swept").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        swept = 0.0
    if prompts[-1]["ts"] <= max(swept, _last_work_done(cfg, now)):
        return None
    return ("hubkit: you say it is done but have not re-read his words since his last prompt. NEXT: hubkit asks ls --since 12h; "
            "list every requirement in them and tick each with evidence before saying done")


# ---- claims that need a lookup in the same turn ----

UNCHECKED_WORDS = ["gone", "lost", "absent", "vanished", "disappeared", "does not exist", "doesn't exist", "never written", "was never there",
                   "a person's call", "needs your call", "needs his call", "needs her call", "needs a human decision"]
LOOKUP_TOOLS = ("Read", "Grep", "Glob", "Bash")


def lookups_this_turn(transcript: str | Path | None) -> int | None:
    """Count of Read/Grep/Glob/Bash calls after the last typed prompt in the transcript, or None when unreadable."""
    if not transcript:
        return None
    try:
        lines = Path(transcript).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    n = 0
    for line in reversed(lines):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        c = (o.get("message") or {}).get("content")
        blocks = [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []
        if o.get("type") == "user" and (isinstance(c, str) or any(b.get("type") == "text" for b in blocks)) \
                and (o.get("origin") or {}).get("kind", "human") == "human":
            break
        if o.get("type") == "assistant":
            n += sum(1 for b in blocks if b.get("type") == "tool_use" and b.get("name") in LOOKUP_TOOLS)
    return n


def unchecked_claim(cfg: dict, last: str, transcript: str | Path | None) -> str | None:
    """A reason when the message calls something gone/lost/absent, or a person's call, and this turn made no lookup."""
    from .gate import _asserted

    words = Orders(cfg).intents.get("stop_gate", {}).get("unchecked_words", UNCHECKED_WORDS)
    hit = next((s.strip()[:100] for s in re.split(r"(?<=[.!?])\s+|\n", last) if any(_asserted(s.lower(), w.lower()) for w in words)), None)
    if not hit or lookups_this_turn(transcript) != 0:
        return None
    return (f"hubkit: your message claims {hit!r} but this turn read nothing. A report that something is gone, or is a person's call, "
            "is checked, not relayed. NEXT: read the code or record that writes it (or what he already decided), then say it")


# ---- a brief must not forbid the step that clears a blocker ----

FORBID = re.compile(r"(?i)\b(do not|don't|dont|never|must not|mustn't|not allowed|forbidden|banned)\b")
STOP_WORDS = {"with", "that", "this", "from", "have", "been", "when", "then", "than", "into", "does", "host", "need", "needs", "start",
              "running", "after", "before", "about", "their", "there", "which", "would", "could", "should"}


def _keys(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9_-]{3,}", text.lower()) if w not in STOP_WORDS}


def brief_forbids_fix(cfg: dict, brief: str) -> str | None:
    """The line of a brief that forbids a step an open order is blocked on (shares a key word with its blocker or fix)."""
    blockers = [(o["id"], b) for o in Orders(cfg).live() if o["state"] == "open" and (b := o.get("blocker"))]
    for line in brief.splitlines():
        if not FORBID.search(line):
            continue
        for oid, b in blockers:
            shared = _keys(line) & _keys(b["on"] + " " + b["fix"])
            if shared:
                return (f"hubkit WARNING: this brief says {line.strip()[:100]!r}, which touches what {oid} is blocked on ({b['on'][:60]}; "
                        f"shared: {', '.join(sorted(shared)[:3])}). A brief must not forbid the one action that clears the blocker. "
                        "NEXT: reword it, or allow that step explicitly")
    return None


# ---- merge guard (warn only) ----

MERGE_CMD = re.compile(r"\bgh\s+pr\s+merge\b([^\n;&|]*)")


def merge_target(command: str) -> str | None:
    """The PR number a `gh pr merge` command names ("" when it names none), or None when it is not a merge."""
    m = MERGE_CMD.search(command)
    if not m:
        return None
    n = re.search(r"(?:/pull/|\s)(\d{1,7})\b", " " + m[1])
    return n[1] if n else ""


def merge_warning(cfg: dict, command: str, now: float | None = None) -> str | None:
    """A warning when a merge has neither a live order nor a typed word of his naming that PR. Never denies."""
    num = merge_target(command)
    if num is None:
        return None
    now = now or time.time()
    for o in Orders(cfg).live():
        if o["state"] == "open" and (re.search(rf"(?<!\d){num}(?!\d)", " ".join([o["quote"], *o["refs"]])) if num else o["kind"] == "merge"):
            return None
    for r in recent_prompts(cfg, 7200, now):
        if re.search(r"(?i)\bmerge", r["text"]) and (not num or re.search(rf"(?<!\d){num}(?!\d)", r["text"])):
            return None
    what = f"#{num}" if num else "a PR"
    return (f"hubkit WARNING: merging {what} with no live order and none of his typed words naming it. A merge is done on his order only. "
            "NEXT: stop unless he ordered it; if he did, open the order: hubkit order open --kind merge --quote \"<his words>\"")
