"""Stop-hook gate: reasons the hub may not end its turn yet (arm the watch, stalled item, unfounded question, unverified verdict)."""

from __future__ import annotations

import re
import time

from . import routed, store
from .orders import PR_REF, Orders
from .status import work_load

VERDICT = re.compile(r"(?i)\b(approved?|changes requested|request_changes|verdict|review(\s+(of|on|for)\s+\S+)?\s+(is\s+)?(done|complete|finished|landed))\b")
_V = r"(?:verdict|review|approval)"
AWAITING = re.compile(
    rf"(?i)\b(?:wait(?:s|ing)?\s+(?:on|for)|awaiting|expect(?:s|ing|ed)?)\s+(?:\S+\s+){{0,3}}?{_V}\b"
    rf"|\bpending\s+(?:an?\s+|the\s+)?{_V}\b|\bno\s+{_V}\b|\b(?:until|still\s+need\w*|yet\s+to\s+(?:get|have|receive|see))\s+(?:\S+\s+){{0,2}}?{_V}\b"
)
WATCH_CMD = "python3 ~/.claude/skills/hub/scripts/hubkit watch --interval 120 --max-cycles 30"


def watch_live(cfg: dict) -> bool:
    """A `hubkit watch` is running: its pidfile names a process that is still the same process."""
    from . import hooks

    p = store.state(cfg) / "watch.pid"
    try:
        pid, start = p.read_text().split()
    except ValueError, OSError:
        return False
    return hooks.proc_start(int(pid)) == start


def open_work(cfg: dict) -> bool:
    """True while routed work or a live open order exists."""
    return bool(routed.items(cfg)) or any(o["state"] == "open" for o in Orders(cfg).live())


def blocked_on_user(cfg: dict) -> bool:
    """True when an `ask` work item says something really waits on the user."""
    return any(i.get("kind") == "ask" for i in work_load(store.state(cfg)))


def is_question(cfg: dict, last: str) -> bool:
    """True when the final message ends in a question or holds a configured stop-guard phrase."""
    tail = last.splitlines()[-1] if last else ""
    return tail.endswith("?") or any(p in last.lower() for p in cfg["stop_guard"]["phrases"])


def unverified_verdict(cfg: dict, last: str) -> list[str]:
    """PRs of a tracked review item that a sentence of the message gives a verdict on, while GitHub shows no
    review at the PR's current head that counts (routed.counts_as_review). A verdict word and a PR number count only in the same sentence."""
    named: set[str] = set()
    for sentence in re.split(r"(?<=[.!?])\s+|\n", last):
        if VERDICT.search(sentence) and PR_REF.search(sentence) and not AWAITING.search(sentence):
            named |= {routed.pr_num(r) for r in routed.refs(cfg, sentence)}
    urls = {
        routed.pr_num(p): p
        for i in routed.items(cfg)
        if i.get("review")
        for p in [*i["prs"], *i.get("queue", [])]
    }
    timeout = float(routed.opts(cfg)["gh_timeout_s"])
    return sorted(n for n in named & urls.keys() if not _reviewed_at_head(cfg, urls[n], timeout))


def _reviewed_at_head(cfg: dict, url: str, timeout: float) -> bool:
    """True when GitHub shows a non-author review at the head, or cannot be read (an unreadable PR proves nothing)."""
    info = routed.pr_info(cfg, url, timeout)
    if info is None:
        return True
    return any(routed.counts_as_review(cfg, r, info) and routed.same_head(r["commit"], info["head"]) for r in info["reviews"])


def stop_reasons(cfg: dict, last: str, now: float | None = None) -> list[str]:
    """Every reason the turn cannot end yet, each with its exact action. Empty = the turn may end."""
    now = now or time.time()
    reasons = routed.prompt_lines(cfg, now)
    if open_work(cfg) and not watch_live(cfg):
        reasons.append(f"hubkit: nothing will wake you when routed work stalls. NEXT: start in the background: {WATCH_CMD}")
    for n in unverified_verdict(cfg, last):
        reasons.append(f"hubkit: your message gives a verdict on #{n}, but GitHub shows no review at its current head "
                       "(agent words are not evidence). NEXT: correct the message; relay a verdict only after the item closes itself")
    if is_question(cfg, last) and not blocked_on_user(cfg):
        reasons.append("hubkit: you ended on a question to the user but nothing is blocked on him. NEXT: settle it from the record "
                       "or route it; only if it truly needs him run: hubkit work add --kind ask --title \"<the one question>\" --note \"<ordered work it blocks>\"")
    return reasons
