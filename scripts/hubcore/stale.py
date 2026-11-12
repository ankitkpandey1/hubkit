"""Open items whose PR is already merged or closed are probably stale: say so, never close them.

`hubkit mine` and the digest read each PR an open work item, request or reply names (at most `stale.max_reads` per run, a
short timeout each, through routed.gh_view) and add " \u2014 PR merged 2d ago, probably stale; last activity 5d ago" to the item
when every PR it names is merged or closed. Warn only."""

from __future__ import annotations

from . import routed
from .owed import age_text


def last_activity(i: dict) -> float:
    """The latest sign of life on an item: added, verified, routed, heard, chased or noted."""
    return max([float(i.get(k) or 0) for k in ("added", "verified", "routed_at", "last_heard", "last_chase", "reported")] + [float(n.get("t", 0)) for n in i.get("notes", [])])


def _closed_at(data: dict) -> float:
    for k in ("mergedAt", "closedAt"):
        if data.get(k):
            try:
                return routed._epoch(str(data[k]).replace("Z", "+00:00"))
            except ValueError:
                continue
    return 0.0


def notes(cfg: dict, items: list[dict], now: float) -> dict[str, str]:
    """Item id -> the stale suffix, for items whose every named PR is merged or closed."""
    o = cfg["stale"]
    if not o["enabled"]:
        return {}
    budget, seen = int(o["max_reads"]), {}
    out: dict[str, str] = {}
    for i in items:
        urls = [u for u in dict.fromkeys([*i.get("prs", []), *routed.refs(cfg, f"{i.get('title', '')} {i.get('note', '')}")]) if u.startswith("http")]
        if not urls:
            continue
        for u in urls:
            if u not in seen and budget > 0:
                budget -= 1
                seen[u] = routed.gh_view(cfg, u, float(o["gh_timeout_s"]))
        datas = [seen.get(u) for u in urls]
        if any(d is None or str(d.get("state", "")).upper() not in ("MERGED", "CLOSED") for d in datas):
            continue
        d = datas[0] or {}
        state = str(d["state"]).lower()
        when = _closed_at(d)
        out[i["id"]] = (f" — PR {state}{' ' + age_text(now - when) + ' ago' if when else ''}, probably stale; "
                        f"last activity {age_text(now - last_activity(i))} ago")
    return out


def digest_lines(cfg: dict, items: list[dict], now: float) -> list[str]:
    by = {i["id"]: i for i in items}
    return [f"  probably stale {k}: {by[k].get('title', '')[:70]}{v}" for k, v in notes(cfg, items, now).items()]
