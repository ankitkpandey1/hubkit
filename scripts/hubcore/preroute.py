"""Check, at the moment of routing, that the work is not already done.

A brief that names a PR is checked against GitHub once: a PR that is MERGED or CLOSED, or a review ask when a
counting review already stands at the PR's current head, gets a warning with the state, the time and a NEXT.
One `gh pr view` per PR (shared with the item's own baseline read), at most two PRs, inside a small total budget (the hook has a 6 s limit). No `gh`, a
failure or a timeout means no warning, never an error.
"""

from __future__ import annotations

import time
from datetime import datetime

from . import routed, store

MAX_PRS = 2
BUDGET_S = 2.0


def _epoch(iso: object) -> float | None:
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except ValueError:
        return None


def _read(cfg: dict, exe: str, url: str, timeout: float) -> dict | None:
    """The PR as GitHub shows it now; shares `routed.gh_view`, so the routed hook reads each PR once."""
    return routed.gh_view(cfg, url, timeout)


def _landed(cfg: dict, data: dict) -> tuple[str, str] | None:
    """(login, clock) of a counting review at the PR's current head, else None."""
    head = str(data.get("headRefOid") or "")
    info = {"author": str((data.get("author") or {}).get("login", "")), "head": head}
    for x in data.get("reviews") or []:
        at = _epoch(x.get("submittedAt"))
        rev = {"login": str((x.get("author") or {}).get("login", "")), "state": str(x.get("state", "")),
               "body": str(x.get("body") or ""), "commit": str((x.get("commit") or {}).get("oid", ""))}
        if at and routed.same_head(rev["commit"], head) and routed.counts_as_review(cfg, rev, info):
            return rev["login"], store.clock(cfg, at)
    return None


def check(cfg: dict, text: str, summary: str = "") -> list[str]:
    """Warnings for a brief whose PR is already merged or closed, or whose review already stands at the head."""
    try:
        urls = [u for u in routed.asked_refs(cfg, text, summary) if u.startswith("http")][:MAX_PRS]
        exe = routed._gh(cfg)
        if not urls or exe is None:
            return []
        budget = min(BUDGET_S, float(routed.opts(cfg)["gh_timeout_s"]))
        deadline = time.time() + budget
        review = routed.is_review(cfg, text, summary)
        out = []
        for url in urls:
            left = deadline - time.time()
            if left <= 0:
                break
            data = _read(cfg, exe, url, left)
            if not data:
                continue
            n = routed.pr_num(url)
            state = str(data.get("state") or "").upper()
            if state in ("MERGED", "CLOSED"):
                at = _epoch(data.get("mergedAt" if state == "MERGED" else "closedAt"))
                when = f" at {store.clock(cfg, at)}" if at else ""
                out.append(f"hubkit WARNING: PR {n} is already {state.lower()}{when}. NEXT: read it ({url}) and the last messages about it before sending more work")
            elif review and (hit := _landed(cfg, data)):
                out.append(f"hubkit WARNING: PR {n} already has a review by {hit[0]} at its current head ({hit[1]}). NEXT: read that review before asking for another")
        return out
    except OSError, ValueError, KeyError, TypeError:
        return []
