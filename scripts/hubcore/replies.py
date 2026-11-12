"""Closing the loop on asks from other people: is the PR they asked about reviewed or merged, and were they told?

A `reply` work item (opened by `hubkit inbound ingest`) names PRs. The tick reads each PR once (routed.refresh_prs shares
its read) and marks the item `due` when every PR it names is merged, closed or reviewed at its current head: the answer
is owed now. How long a person usually waits for an answer is learned from the reply items closed for them."""

from __future__ import annotations

import re
import statistics
import time

from . import lifecycle, routed, status, store
from .orders import PR_REF, Orders

CLAIM_WORDS = ["merged", "approved", "approves", "reviewed", "lgtm", "review is done", "review done", "closed"]


def open_items(cfg: dict) -> list[dict]:
    """Live reply items."""
    return [i for i in status.work_load(store.state(cfg)) if i.get("kind") == "reply"]


def urls(cfg: dict, item: dict) -> list[str]:
    """The item's PRs, a bare #N resolved against `routed.repo` when that is set."""
    repo = str(routed.opts(cfg).get("repo", ""))
    return [u if u.startswith("http") or not repo else f"https://github.com/{repo}/pull/{routed.pr_num(u)}" for u in item.get("prs", [])]


def pr_urls(cfg: dict) -> set[str]:
    """Every readable PR URL of a live reply item (routed.refresh_prs reads them with its own)."""
    return {u for i in open_items(cfg) for u in urls(cfg, i) if u.startswith("http")}


def state_of(cfg: dict, info: dict | None) -> tuple[str, bool]:
    """(words, ready): the PR's state in words, and whether it is merged, closed or reviewed at its current head."""
    if not info:
        return "unreadable", False
    if info["state"] in ("MERGED", "CLOSED"):
        return info["state"].lower(), True
    for r in info["reviews"]:
        if routed.counts_as_review(cfg, r, info) and routed.same_head(r["commit"], info["head"]):
            return f"reviewed ({r['state'].lower()} by {r['login']}) at its head", True
    return "open, no review at its head", False


def note_states(cfg: dict, got: dict[str, dict | None], now: float) -> None:
    """Stamp each reply item with its PRs' states and whether the answer is due now. `got` is the tick's one read per PR."""
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        all_ = status.work_load(sd)
        for i in all_:
            if i.get("kind") != "reply":
                continue
            us = urls(cfg, i)
            states = {u: state_of(cfg, got.get(u)) for u in us}
            i["pr_states"] = {u: s for u, (s, _) in states.items()}
            was = bool(i.get("due"))
            i["due"] = bool(us) and all(ok for _, ok in states.values())
            if i["due"] and not was:
                i["due_at"] = now
        status.work_save(sd, all_)


def history(cfg: dict) -> dict[str, list[float]]:
    """Minutes from ask to reply per person, from the reply items closed so far (newest 20 each)."""
    out: dict[str, list[float]] = {}
    for f in sorted((store.state(cfg) / "archive").glob("archive_*.jsonl")):
        for r in store.read_rows(f)[0]:
            if r.get("type") == "work" and r.get("kind") == "reply" and r.get("outcome") == "done" and r.get("added") and r.get("ts"):
                m = (float(r["ts"]) - float(r["added"])) / 60
                if m >= 0:
                    out.setdefault(str(r.get("owner", "")), []).append(m)
    return {k: v[-20:] for k, v in out.items()}


def usual(cfg: dict, owner: str, hist: dict[str, list[float]]) -> float | None:
    """The person's own median time to reply, once there are `watcher.reply_min_samples` of them, else None."""
    xs = hist.get(owner, [])
    return statistics.median(xs) if len(xs) >= int(cfg["watcher"]["reply_min_samples"]) else None


def limit(cfg: dict, owner: str, hist: dict[str, list[float]]) -> float:
    """Minutes an ask to this person may wait before the age alert: their usual, never below `reply_floor_min`, never above
    `reply_owed_min` (which is also the limit while there are too few samples)."""
    ceiling = float(cfg["watcher"]["reply_owed_min"])
    m = usual(cfg, owner, hist)
    return ceiling if m is None else min(ceiling, max(float(cfg["watcher"]["reply_floor_min"]), m))


def _links(cfg: dict, i: dict) -> str:
    return "; ".join(f"{u} {s}" for u, s in (i.get("pr_states") or {}).items())


def alerts(cfg: dict, now: float) -> list[dict]:
    """An answer due now (every PR asked about is merged, closed or reviewed at its head) alerts at once with what to tell
    the person; any other reply past the person's own limit alerts on age."""
    hist = history(cfg)
    out = []
    for i in open_items(cfg):
        age = int((now - i["added"]) / 60)
        if i.get("due"):
            out.append({"key": f"{i['id']}:answer_due", "msg": (
                f"{i['id']} answer due now to {i['owner']} ({i.get('note', '')}): {_links(cfg, i)}. "
                f"NEXT: tell {i['owner']} the PR state with its link, then: hubkit inbound replied {i['id']} --note \"<what you sent>\"")})
        elif (now - i["added"]) / 60 > limit(cfg, i["owner"], hist):
            m = usual(cfg, i["owner"], hist)
            seen = f", usually answered in {int(m)} min" if m is not None else ""
            out.append({"key": f"{i['id']}:reply_owed", "msg": (
                f"{i['id']} reply owed to {i['owner']} for {age} min{seen} ({i['title'][:50]}). "
                f"NEXT: answer them, then: hubkit inbound replied {i['id']} --note \"<what you sent>\"")})
    return out


def _dur(m: float) -> str:
    return f"{int(m)}m" if m < 60 else f"{int(m // 60)}h{int(m % 60):02d}m"


def ls_lines(cfg: dict, now: float) -> list[str]:
    """One screen: each open ask with person, where, age, their usual wait, its PRs with state (read from GitHub now) and
    whether the answer is due."""
    items = open_items(cfg)
    if not items:
        return ["no open asks"]
    hist = history(cfg)
    timeout = float(routed.opts(cfg)["gh_timeout_s"])
    cache: dict[str, dict | None] = {}
    rows, due = [], 0
    for i in items:
        parts, ready = [], bool(urls(cfg, i))
        for u in urls(cfg, i):
            if u not in cache:
                cache[u] = routed.pr_info(cfg, u, timeout)
            s, ok = state_of(cfg, cache[u]) if u.startswith("http") else ("unresolved", False)
            parts.append(f"#{routed.pr_num(u)} {s}")
            ready &= ok
        due += ready
        m = usual(cfg, i["owner"], hist)
        rows.append(f"{i['id']} {i['owner']} · {i.get('note', '')} · asked {_dur((now - i['added']) / 60)} ago"
                    + (f" · usually answered in {_dur(m)}" if m is not None else "")
                    + f" · {'; '.join(parts) or 'no PR named'} · reply due: {'yes' if ready else 'no'}")
    return [f"{len(items)} open asks, {due} ready to answer", *rows]


def block_lines(work: list[dict], now: float) -> list[str]:
    """Status-block lines for open asks from people: at most 4, answers due first."""
    mine = sorted((i for i in work if i.get("kind") == "reply"), key=lambda i: (not i.get("due"), i["added"]))
    out = [f"reply {i['id']} {i['owner']} ({i.get('note', '')}) {int((now - i['added']) / 60)}m: "
           + ("ANSWER DUE NOW, " + "; ".join(f"#{routed.pr_num(u)} {s}" for u, s in (i.get("pr_states") or {}).items()) if i.get("due") else "owed")
           for i in mine[:4]]
    return out + ([f"reply: {len(mine) - 4} more asks owed; run hubkit inbound ls"] if len(mine) > 4 else [])


def mark_replied(cfg: dict, key: str, note: str) -> str:
    """Close the reply item with this work id or message id: the person was answered."""
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        all_ = status.work_load(sd)
        hit = next((i for i in all_ if i.get("kind") == "reply" and key in (i["id"], i.get("msg_id"))), None)
        if hit is None:
            raise KeyError(f"no open reply item {key}")
        lifecycle.check("reply", lifecycle.state_of("reply", hit), "replied", note=note)
        hit["replied"] = time.time()
        status.work_save(sd, all_)
    return status.work_done(cfg, hit["id"], note)


def auto_replied(cfg: dict, command: str) -> list[str]:
    """A chat-send command names a message id (that ask is answered) or a person (their asks that are due now, else their
    only open ask). Returns the ids marked."""
    items = open_items(cfg)
    by_id = [i for i in items if i.get("msg_id") and re.search(rf"(?<!\d){re.escape(str(i['msg_id']))}(?!\d)", command)]
    hit = by_id
    if not hit:
        who = [i for i in items if any(re.search(rf"(?i)(?<!\w){re.escape(n)}(?!\w)", command) for n in {i["owner"], i["owner"].split()[0]} if len(n) >= 3)]
        for owner in {i["owner"] for i in who}:
            theirs = [i for i in who if i["owner"] == owner]
            hit += [i for i in theirs if i.get("due")] or (theirs if len(theirs) == 1 else [])
    done = []
    for i in hit:
        try:
            mark_replied(cfg, i["id"], "sent: " + command[:120])
            done.append(i["id"])
        except KeyError:
            continue
    return done


def owed_reasons(cfg: dict, last: str) -> list[str]:
    """Stop-gate: the message says a PR was merged, approved or reviewed while an open reply item names that PR."""
    from . import gate

    words = [w.lower() for w in Orders(cfg).intents.get("stop_gate", {}).get("reply_claim_words", CLAIM_WORDS)]
    items = open_items(cfg)
    if not items:
        return []
    out: dict[str, str] = {}
    for sentence in re.split(r"(?<=[.!?])\s+|\n", last):
        low = sentence.lower()
        if not (PR_REF.search(sentence) or gate.REPO_PR.search(sentence)) or not any(gate._asserted(low, w) for w in words):
            continue
        named = routed.refs(cfg, sentence)
        for i in items:
            if routed.overlaps(named, urls(cfg, i)):
                out[i["id"]] = (f"hubkit: {i['owner']} asked about it in {i.get('note', '')} ({i['id']}); reply is owed. NEXT: tell {i['owner']} the PR state "
                                f"with its link, then: hubkit inbound replied {i['id']} --note \"<what you sent>\"")
    return list(out.values())
