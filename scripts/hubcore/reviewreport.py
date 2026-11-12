"""A reviewer's report is checked for the steps its role requires (`[roles.<role>] report_must_include`).

`routed.heard` calls `check` when the owner of a review item reports a result; the missing steps stay on the item
(`report_missing`) as one prompt line and one tick alert until a later report shows them or the item closes.
Warn-level: the GitHub-evidence close of a review is not touched."""

from __future__ import annotations

import re
import time


def required(cfg: dict, item: dict, now: float) -> list[str]:
    """The phrases the owner's role(s) must show in a review report: the peer's roster role and `reviewer`."""
    from . import roster

    try:
        mine = roster.view(cfg, str(item["owner"]), now, skip=item["id"])
        roles = [r for r in dict.fromkeys([mine["role"], "reviewer"]) if r]
    except OSError, ValueError, KeyError, TypeError:
        roles = ["reviewer"]
    need: list[str] = []
    for r in roles:
        need += [str(p) for p in ((cfg.get("roles") or {}).get(r) or {}).get("report_must_include") or []]
    return list(dict.fromkeys(need))


def missing(cfg: dict, item: dict, text: str, now: float) -> list[str]:
    """The required phrases (regex, else plain text) that the report lacks."""
    out = []
    for phrase in required(cfg, item, now):
        try:
            found = bool(re.search(phrase, text, re.I))
        except re.error:
            found = phrase.lower() in text.lower()
        if not found:
            out.append(phrase)
    return out


def check(cfg: dict, item: dict, text: str, ts: float) -> None:
    """Record on the item what this report lacks (or clear it when the report shows everything)."""
    gaps = missing(cfg, item, text, time.time())
    if gaps:
        item["report_missing"], item["report_at"] = gaps, ts
    else:
        item.pop("report_missing", None)
        item.pop("report_at", None)


def _line(item: dict) -> str:
    from . import routed

    pr = routed._pr_no(item["prs"][0]) if item.get("prs") else item["id"]
    return (f"{item['owner']}'s review report on {pr} does not show: {'; '.join(item['report_missing'])}. "
            f"NEXT: ask {item['owner']} for them before relaying the verdict")


def prompt_lines(items: list[dict]) -> list[str]:
    """One WARNING line per review item whose last report lacked required steps."""
    return [f"hubkit WARNING REPORT-INCOMPLETE: {_line(i)}" for i in items if i.get("report_missing")]


def alerts(items: list[dict]) -> list[dict]:
    """The same as tick alerts, one per report (the key carries the report time)."""
    return [{"key": f"{i['id']}:report_incomplete:{int(i.get('report_at') or 0)}", "msg": f"REPORT-INCOMPLETE: {_line(i)}"} for i in items if i.get("report_missing")]
