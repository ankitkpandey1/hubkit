"""Stop-gate check: a PR called "ready to merge" or "waiting on the user's merge" must be mergeable on GitHub.

A sentence of the final message that says a PR waits on the user's merge, while GitHub shows changes requested, a review
still required, a blocked, behind or conflicting branch, or failing CI, holds the turn. The reason says whose court the PR
is in. A PR gh cannot read proves nothing (fail open)."""

from __future__ import annotations

import re

from . import routed
from .orders import PR_REF, Orders

READY_WORDS = [
    "ready to merge", "ready for you to merge", "ready for your merge", "ready for merge", "waiting on your merge", "waiting for your merge",
    "waits for your merge", "awaiting your merge", "needs your merge", "needs your word", "awaiting your word", "waiting on your word",
    "waits for your word", "for you to merge", "your word to merge", "your call to merge",
]
BAD_DECISION = ("CHANGES_REQUESTED", "REVIEW_REQUIRED")
BAD_MERGE = ("BLOCKED", "DIRTY", "BEHIND")


def problems(info: dict) -> list[str]:
    """What GitHub shows against a merge now, from a routed.pr_info dict; empty = nothing blocks it."""
    if info.get("state") in ("MERGED", "CLOSED"):
        return []
    out = []
    if info.get("decision") in BAD_DECISION:
        out.append("review decision " + info["decision"])
    if info.get("merge_state") in BAD_MERGE:
        out.append("merge state " + info["merge_state"])
    if info.get("ci") == "failing":
        out.append("CI failing")
    return out


def whose(info: dict) -> str:
    """The court in words: the requested reviewers, who requested changes, or CI."""
    latest: dict[str, str] = {}
    for r in sorted(info.get("reviews", []), key=lambda x: x["at"]):
        latest[r["login"]] = r["state"].upper()
    changes = sorted(k for k, v in latest.items() if v == "CHANGES_REQUESTED" and k)
    c = routed.court(info)
    if c == "ci":
        return "CI's court (checks are " + str(info.get("ci")) + ")"
    if c == "author":
        by = f"; {', '.join(changes)} requested changes" if changes else ""
        return f"the author's court (changes to make or the branch to update{by})"
    if c.startswith("reviewer:"):
        names = ", ".join(info.get("requested") or []) or "a reviewer not yet named"
        return f"the reviewers' court (review still required from {names})"
    return "a blocker GitHub does not name"


def ready_claims(cfg: dict, last: str) -> list[str]:
    """PR refs named in a sentence that says the PR waits on the user's merge (negated sentences are skipped)."""
    from . import gate

    words = [w.lower() for w in Orders(cfg).intents.get("stop_gate", {}).get("ready_words", READY_WORDS)]
    named: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n", last):
        low = sentence.lower()
        if (PR_REF.search(sentence) or gate.REPO_PR.search(sentence)) and any(gate._asserted(low, w) for w in words):
            named += routed.refs(cfg, sentence)
    return named


def not_mergeable(cfg: dict, last: str) -> list[str]:
    """One reason per PR called ready for the user's merge while GitHub shows otherwise."""
    named = ready_claims(cfg, last)
    if not named:
        return []
    timeout = float(routed.opts(cfg)["gh_timeout_s"])
    tracked = [u for i in routed.items(cfg) for u in [*i["prs"], *i.get("queue", [])]]
    out: dict[str, str] = {}
    for ref in named:
        for u in [ref] if ref.startswith("http") else [t for t in tracked if routed.same_pr(t, ref)]:
            info = routed.pr_info(cfg, u, timeout)
            if info and (why := problems(info)):
                out[routed.pr_key(u)] = (
                    f"hubkit: your message says #{routed.pr_num(u)} waits on the user's merge, but GitHub shows {', '.join(why)}: it is in "
                    f"{whose(info)}. NEXT: say who it waits on, or leave it off the user's list")
    return sorted(out.values())
