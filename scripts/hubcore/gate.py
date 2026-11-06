"""Stop-hook gate: reasons the hub may not end its turn yet (arm the watch, stalled item, unfounded question, unverified verdict)."""

from __future__ import annotations

import re
import time

from . import lessons, routed, slow, store
from .orders import PR_REF, Orders
from .status import work_load

VERDICT = re.compile(r"(?i)\b(approved?|changes requested|request_changes|verdict|review(\s+(of|on|for)\s+\S+)?\s+(is\s+)?(done|complete|finished|landed))\b")
_V = r"(?:verdict|review|approval)"
AWAITING = re.compile(
    rf"(?i)\b(?:wait(?:s|ing)?\s+(?:on|for)|awaiting|expect(?:s|ing|ed)?)\s+(?:\S+\s+){{0,3}}?{_V}\b"
    rf"|\bpending\s+(?:an?\s+|the\s+)?{_V}\b|\bno\s+{_V}\b|\b(?:until|still\s+need\w*|yet\s+to\s+(?:get|have|receive|see))\s+(?:\S+\s+){{0,2}}?{_V}\b"
)
REPO_PR = re.compile(r"[\w.-]+(?:/[\w.-]+)?#\d{2,6}|\b[A-Za-z][\w.-]*\s+\d{2,6}\b")
WATCH_CMD = "python3 ~/.claude/skills/hub/scripts/hubkit watch --interval 120 --max-cycles 30"


def watch_live(cfg: dict) -> bool:
    """A `hubkit watch` is running: its pidfile names a process that is still the same process."""
    from . import hooks

    p = store.state(cfg) / "watch.pid"
    try:
        pid, start = p.read_text(encoding="utf-8").split()
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
    named: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n", last):
        if VERDICT.search(sentence) and (PR_REF.search(sentence) or REPO_PR.search(sentence)) and not AWAITING.search(sentence):
            named += routed.refs(cfg, sentence)
    urls = {
        routed.pr_key(p): p
        for i in routed.items(cfg)
        if i.get("review")
        for p in [*i["prs"], *i.get("queue", [])]
        if any(routed.same_pr(p, r) for r in named)
    }
    timeout = float(routed.opts(cfg)["gh_timeout_s"])
    return sorted(routed.pr_num(u) for u in urls.values() if not _reviewed_at_head(cfg, u, timeout))


def ci_not_green(cfg: dict, last: str) -> list[str]:
    """PRs the message gives an approving verdict on while CI at the head is failing or pending. A PR is resolved from
    its URL, a repo-qualified ref or `routed.repo`; a bare number is matched to a tracked item's PR."""
    out: list[str] = []
    timeout = float(routed.opts(cfg)["gh_timeout_s"])
    tracked = [u for i in routed.items(cfg) for u in [*i["prs"], *i.get("queue", [])]]
    for sentence in re.split(r"(?<=[.!?])\s+|\n", last):
        if not (re.search(r"(?i)\bapprov", sentence) and (PR_REF.search(sentence) or REPO_PR.search(sentence)) and not AWAITING.search(sentence)):
            continue
        for ref in routed.refs(cfg, sentence):
            for u in [ref] if ref.startswith("http") else [t for t in tracked if routed.same_pr(t, ref)]:
                info = routed.pr_info(cfg, u, timeout)
                if info and info.get("ci") in ("failing", "pending"):
                    out.append(f"#{routed.pr_num(u)} ({info['ci']})")
    return sorted(set(out))


def _reviewed_at_head(cfg: dict, url: str, timeout: float) -> bool:
    """True when GitHub shows a non-author review at the head, or cannot be read (an unreadable PR proves nothing)."""
    info = routed.pr_info(cfg, url, timeout)
    if info is None:
        return True
    return any(routed.counts_as_review(cfg, r, info) and routed.same_head(r["commit"], info["head"]) for r in info["reviews"])


BLOCKED_WORDS = ["blocked", "is down", "are down", "failed", "failing", "no host", "cannot reach", "can't reach", "unreachable",
                 "not available", "waiting on", "waiting for", "stuck", "needs a person", "needs his call", "needs her call"]


NEGATION = re.compile(r"(?i)\b(no|not|nothing|nobody|none|never|nor|without|isn't|aren't|wasn't|weren't|hasn't|haven't|doesn't|don't)\s+(?:\w+\s+){0,3}$")


# A wait that is the user's own stop or hold is not a blocker to solve: waiting on his word, or work he put on hold.
USER_WAIT = re.compile(
    r"(?i)\b(?:wait(?:s|ing)?|awaiting)\s+(?:on|for)\s+(?:your|you|his|her|the user'?s?)\b"
    r"|\b(?:on\s+hold|held|(?:you|he|she)\s+(?:asked|told|said|ordered)\s+(?:me\s+)?to\s+(?:hold|stop|wait|pause))\b"
)


def _asserted(low: str, word: str) -> bool:
    """The word is in the sentence and not negated ("nothing is stuck", "not blocked", "no failed runs" assert nothing)."""
    return any(not NEGATION.search(low[: m.start()]) for m in re.finditer(rf"(?<!\w){re.escape(word)}(?!\w)", low))


def blocked_claim(cfg: dict, last: str) -> str | None:
    """The sentence of the final message that says live work is blocked, down, failed or waiting on a host, tool or
    person, or None. Review-verdict waits are the verdict check's business. Words come from intents.toml [stop_gate]."""
    words = [w.lower() for w in Orders(cfg).intents.get("stop_gate", {}).get("blocked_words", BLOCKED_WORDS)]
    for sentence in re.split(r"(?<=[.!?])\s+|\n", last):
        low = sentence.lower()
        if any(_asserted(low, w) for w in words) and not AWAITING.search(sentence) and not USER_WAIT.search(sentence):
            return sentence.strip()[:100]
    return None


def blocker_reasons(cfg: dict, last: str, now: float) -> list[str]:
    """Blocked work must be solved, not reported: an open blocker without a running fix, a fix past its limit, a
    stop-loss with no result since, or a message that calls an open order blocked when no fix is on record."""
    orders = Orders(cfg)
    out = []
    live = [o for o in orders.live() if o["state"] == "open"]
    for o in live:
        why = orders.blocker_problem(o, now)
        if why:
            out.append(f"hubkit BLOCKER: {why}. NEXT: start the fix now on a free owner, record it: hubkit order block {o['id']} --on \"..\" "
                       f"--fix \"<action running>\" --holder <who>; when it ran: hubkit order result {o['id']} --note \"<what came of it>\"")
        if orders.stoploss_pending(o):
            out.append(f"hubkit STOP-LOSS: {o['id']} was chased past the limit and no result is recorded since. A reply is not a result. "
                       f"NEXT: do the blocking step, then: hubkit order result {o['id']} --note \"<what you did, what came of it>\"")
    claim = blocked_claim(cfg, last) if live else None
    if claim and not any((o.get("blocker") or {}).get("fix") for o in live):
        out.append(f"hubkit: your message says work is blocked ({claim!r}) but no order has a fix on record. Solve it, do not report it. "
                   f"NEXT: start the fix on a free owner, then: hubkit order block <id> --on \"..\" --fix \"<action running>\" --holder <who>")
    return out


def stop_reasons(cfg: dict, last: str, now: float | None = None, transcript: str | None = None, cwd: str | None = None) -> list[str]:
    """Every reason the turn cannot end yet, each with its exact action. Empty = the turn may end."""
    now = now or time.time()
    ci_bad = ci_not_green(cfg, last)  # before prompt_lines: a reviewed item closes there
    reasons = routed.prompt_lines(cfg, now)
    reasons += blocker_reasons(cfg, last, now)
    if r := lessons.unpushed_work(last, cwd):
        reasons.append(r)
    reasons += [r for r in (lessons.unswept_asks(cfg, last, now), lessons.unchecked_claim(cfg, last, transcript), lessons.unrun_claim(cfg, last, transcript)) if r]
    if refs := lessons.bare_refs(cfg, last):
        reasons.append(f"hubkit: your message names {', '.join(refs[:4])} with no link. NEXT: give the full URL of each PR or issue so he can click it")
    try:
        reasons += slow.gate_lines(cfg, now)
    except Exception as e:  # a failing estimate check must not wedge the hub's turn end
        store.append_row(store.state(cfg) / "hook-errors.jsonl", {"where": "slow.gate", "error": repr(e)[:200]})
    if open_work(cfg) and not watch_live(cfg):
        reasons.append(f"hubkit: nothing will wake you when routed work stalls. NEXT: start in the background: {WATCH_CMD}")
    reasons += [f"hubkit: the ask {i['id']} ({i['title'][:50]}) was asked {i['asked']} times with no answer. Stop asking. NEXT: take the safest default, record it with the reason, "
                f"and go on: hubkit work done {i['id']} --note \"defaulted to ...\"" for i in work_load(store.state(cfg)) if i.get("kind") == "ask" and int(i.get("asked", 1)) >= 3]
    for n in unverified_verdict(cfg, last):
        reasons.append(f"hubkit: your message gives a verdict on #{n}, but GitHub shows no review at its current head "
                       "(agent words are not evidence). NEXT: correct the message; relay a verdict only after the item closes itself")
    if ci_bad:
        reasons.append(f"hubkit: your message approves {', '.join(ci_bad)} but CI at the head is not green. NEXT: say CI is not green, or wait for it, before relaying an approval")
    if (r := lessons.prose_ask(last, transcript, is_question(cfg, last) and blocked_on_user(cfg))):
        reasons.append(r)
    if is_question(cfg, last) and not blocked_on_user(cfg):
        reasons.append("hubkit: you ended on a question to the user but nothing is blocked on him. NEXT: settle it from the record "
                       "or route it; only if it truly needs him run: hubkit work add --kind ask --title \"<the one question>\" --note \"<ordered work it blocks>\"")
    return reasons
