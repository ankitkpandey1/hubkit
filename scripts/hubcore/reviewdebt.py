"""The user's own review debt: PRs where their review was asked again and the head has moved since they last reviewed.

A rate-limited sweep in the tick (`[review_debt] every_min`) asks GitHub for the open PRs that request the user's review
(`gh search prs --review-requested <login> --state open`), reads each (at most `max_reads`) and keeps those the user
reviewed before whose last review is not at the current head. The result is one state file, `review_debt.json`; `hubkit mine`
shows it under ON THE USER, the digest lists it, and the tick alerts once per PR per head. Read only: it opens no order,
and a missing or failing gh leaves everything as it was."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import routed, store


def _opts(cfg: dict) -> dict:
    return cfg["review_debt"]


def login(cfg: dict) -> str:
    return str(cfg["user"].get("github_login", "")).strip()


def enabled(cfg: dict) -> bool:
    """`enabled = true/false` decides; the default "auto" is on only when `user.github_login` is set."""
    want = _opts(cfg)["enabled"]
    return bool(login(cfg)) and (want if isinstance(want, bool) else True)


def _path(cfg: dict) -> Path:
    return store.state(cfg) / "review_debt.json"


def load(cfg: dict) -> dict:
    try:
        data = json.loads(_path(cfg).read_text(encoding="utf-8"))
    except OSError, ValueError:
        data = {}
    return {"tried": float(data.get("tried", 0)), "at": float(data.get("at", 0)), "items": list(data.get("items", [])), "alerted": list(data.get("alerted", []))}


def _search(cfg: dict) -> list[dict] | None:
    """The open PRs asking for the user's review, or None when gh is missing, slow or answers nonsense."""
    exe = routed._gh(cfg)
    if exe is None:
        return None
    cmd = [exe, "search", "prs", "--review-requested", login(cfg), "--state", "open", "--limit", str(int(_opts(cfg)["max_prs"])),
           "--json", "url,title,number,repository,updatedAt"]
    try:
        r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=float(_opts(cfg)["gh_timeout_s"]), check=False)
        data = json.loads(r.stdout)
    except OSError, subprocess.SubprocessError, ValueError:
        return None
    return [x for x in data if isinstance(x, dict) and str(x.get("url", "")).startswith("http")] if isinstance(data, list) and r.returncode == 0 else None


def _owed(cfg: dict, pr: dict, now: float) -> dict | None:
    """The debt record for one PR the user reviewed before, or None (never reviewed, or the last review is at the head)."""
    timeout = float(_opts(cfg)["gh_timeout_s"])
    info = routed.pr_info(cfg, pr["url"], timeout)
    if not info or info.get("state") not in ("", "OPEN"):
        return None
    me = login(cfg).lower()
    mine = sorted((r for r in info["reviews"] if r["login"].lower() == me and r["state"].upper() != "PENDING"), key=lambda r: r["at"])
    if not mine or routed.same_head(mine[-1]["commit"], info["head"]):
        return None
    last = mine[-1]
    since = last["at"]
    raw = routed.gh_view(cfg, pr["url"], timeout) or {}
    try:
        since = max(since, routed._epoch(str(raw["commits"][-1]["committedDate"]).replace("Z", "+00:00")))
    except KeyError, IndexError, TypeError, ValueError:
        pass
    return {"url": pr["url"], "title": str(pr.get("title", ""))[:100], "head": info["head"], "last_commit": last["commit"],
            "last_state": last["state"].lower(), "last_at": last["at"], "since": min(since, now)}


def sweep(cfg: dict, now: float, force: bool = False) -> list[dict]:
    """Run the sweep when `every_min` has passed since the last try. Returns the alerts for debts not yet alerted at their head."""
    if not enabled(cfg):
        return []
    st = load(cfg)
    if not force and now - st["tried"] < float(_opts(cfg)["every_min"]) * 60:
        return []
    st["tried"] = now
    found = _search(cfg)
    if found is None:  # gh missing or failing: keep what we knew, try again after every_min
        store.write_json_atomic(_path(cfg), st)
        return []
    items = []
    for pr in found[:int(_opts(cfg)["max_reads"])]:
        if debt := _owed(cfg, pr, now):
            items.append(debt)
    st["items"], st["at"] = items, now
    out = []
    for d in items:
        key = f"{d['url']}@{d['head']}"
        if key not in st["alerted"]:
            st["alerted"].append(key)
            out.append({"key": f"review_debt:{routed.pr_key(d['url'])}:{d['head'][:8]}",
                        "msg": f"the user owes a re-review: {d['url']} ({d['title'][:50]}) moved to {d['head'][:7]} after their {d['last_state']} review at {d['last_commit'][:7]}. "
                               f"NEXT: tell the user in the next report with the link, or route a re-review at head {d['head'][:7]} if they said to (the hub does not review for them)"})
    st["alerted"] = st["alerted"][-300:]
    store.write_json_atomic(_path(cfg), st)
    return out


def rows(cfg: dict, now: float) -> list[str]:
    """Rows for `hubkit mine` under ON THE USER: link, what moved, how long it has waited."""
    from .owed import age_text

    st = load(cfg)
    if not enabled(cfg) or not st["items"]:
        return []
    old = f" (as of {age_text(now - st['at'])} ago)" if now - st["at"] > 3 * float(_opts(cfg)["every_min"]) * 60 else ""
    return [f"  (re-review) {d['title'][:60]} {d['url']}: head {d['head'][:7]} moved after their {d['last_state']} review at {d['last_commit'][:7]}, waiting {age_text(now - d['since'])}{old}"
            for d in st["items"]]


def digest_lines(cfg: dict, now: float) -> list[str]:
    from .owed import age_text

    st = load(cfg)
    if not enabled(cfg) or not st["items"]:
        return []
    return [f"  waits on you: re-review {d['url']} ({age_text(now - d['since'])})" for d in st["items"]]
