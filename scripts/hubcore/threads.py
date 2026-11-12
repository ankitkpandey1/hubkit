"""Threads: one piece of work that became up to four records (the user's ask, the routed job, the PR, the reply owed).

A record joins a thread through an optional `thread` field holding the id of the root ask (an order or a request, or an
inbound ask when nothing older exists). Work items keep the field in `work.json`; orders (a folded ledger) keep theirs in
`thread-links.json` in the state dir. A root has no `thread` of its own. Nothing here closes anything: when every step under
a root is closed the tick alerts with the exact command to close the root, and the hub confirms."""

from __future__ import annotations

import re

from . import lifecycle, routed, status, store
from .orders import LIVE_STATES, Orders

ROOT_KINDS = ("order", "request")
LINKABLE = ("routed", "reply", "active", "tracked")  # kinds the automatic rules may link
FOR_LINE = re.compile(r"(?im)^[\s>*_-]*for:\s*(O-\d{6}-[0-9a-f]+|[A-Z]\d+)\b")
SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
ARCHIVE_FILES = 30


def opts(cfg: dict) -> dict:
    return cfg["threads"]


# ---- records: live work items, orders and (for history) the archive, in one shape ----

def _links_path(cfg: dict):
    return store.state(cfg) / "thread-links.json"


def order_links(cfg: dict) -> dict[str, str]:
    p = _links_path(cfg)
    if not p.exists():
        return {}
    import json

    try:
        return {str(k): str(v) for k, v in json.loads(p.read_text(encoding="utf-8")).items()}
    except ValueError, OSError:
        return {}


def _work_rec(cfg: dict, i: dict, live: bool) -> dict:
    kind = str(i.get("kind", ""))
    state = lifecycle.state_of(kind, i) if live and kind in ("routed", "request", "reply") else ("open" if live else str(i.get("outcome", "done")))
    prs = [*i.get("prs", []), *i.get("queue", [])] or routed.refs(cfg, str(i.get("title", "")), cap=None)
    return {"id": i["id"], "kind": kind, "live": live, "state": state, "title": str(i.get("title", "")), "added": float(i.get("added") or 0),
            "owner": str(i.get("owner", "")), "thread": str(i.get("thread") or ""), "prs": prs, "due": bool(i.get("due")),
            "closed_at": float(i.get("ts") or 0) if not live else 0.0, "note": str(i.get("done_note") or ""),
            "pr_states": i.get("pr_states") or {}, "done_when": str(i.get("done_when") or ""), "evidence": {}}


def _order_rec(cfg: dict, o: dict, links: dict[str, str], live: bool) -> dict:
    quote = str(o.get("quote", ""))
    return {"id": o["id"], "kind": "order", "live": live, "state": str(o.get("state") or o.get("outcome", "closed")), "title": quote,
            "added": float(o.get("opened") or 0), "owner": "", "thread": links.get(o["id"], ""), "prs": [*o.get("refs", []), *routed.refs(cfg, quote, cap=None)],
            "due": False, "closed_at": float(o.get("closed_at") or o.get("ts") or 0) if not live else 0.0, "note": "",
            "pr_states": {}, "done_when": "", "evidence": o.get("evidence") or {}, "order_kind": str(o.get("kind", ""))}


def records(cfg: dict, archived: bool = False) -> dict[str, dict]:
    """Every live record by id; with `archived`, also the closed ones found in the newest archive files (live wins)."""
    sd = store.state(cfg)
    links = order_links(cfg)
    out: dict[str, dict] = {}
    for i in status.work_load(sd):
        out[i["id"]] = _work_rec(cfg, i, True)
    orders = Orders(cfg)
    for oid, o in orders.all().items():
        if o["state"] in LIVE_STATES:
            out[oid] = _order_rec(cfg, o, links, True)
    if archived:
        for f in sorted((sd / "archive").glob("archive_*.jsonl"))[-ARCHIVE_FILES:]:
            for r in store.read_rows(f)[0]:
                rid = r.get("id")
                if not rid or rid in out and out[rid]["live"]:
                    continue
                if r.get("type") == "work":
                    out[rid] = _work_rec(cfg, r, False)
                elif r.get("type") == "order":
                    out[rid] = _order_rec(cfg, {**r, "opened": r.get("opened"), "state": r.get("outcome", "closed")}, links, False)
    return out


def thread_id(rec: dict) -> str:
    return rec["thread"] or rec["id"]


def members_of(recs: dict[str, dict], root: str) -> list[dict]:
    return sorted((r for r in recs.values() if r["id"] != root and r["thread"] == root), key=lambda r: r["added"])


# ---- writing a link ----

def _set_thread(cfg: dict, rid: str, root: str) -> None:
    """Set (or, with an empty root, clear) the thread of one record."""
    import json

    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        items = status.work_load(sd)
        hit = next((i for i in items if i["id"] == rid), None)
        if hit is not None:
            if root:
                hit["thread"] = root
            else:
                hit.pop("thread", None)
            status.work_save(sd, items)
            return
    with store.locked(sd / "thread-links.lock"):
        links = order_links(cfg)
        if root:
            links[rid] = root
        else:
            links.pop(rid, None)
        store.write_atomic(_links_path(cfg), json.dumps(links, indent=1))


def _need(recs: dict[str, dict], rid: str, what: str) -> dict:
    rec = recs.get(rid)
    if rec is None:
        raise KeyError(f"no {what} {rid}. NEXT: hubkit thread ls")
    return rec


def link(cfg: dict, rid: str, root: str) -> str:
    """By hand: put `rid` (a live record) under `root` (any record, live or closed)."""
    recs = records(cfg, archived=True)
    rec = _need(recs, rid, "record")
    if not rec["live"]:
        raise store.Refused(f"{rid} is closed and cannot be moved. NEXT: hubkit thread show {thread_id(rec)}")
    top = thread_id(_need(recs, root, "root"))
    if top == rid:
        raise store.Refused(f"{rid} cannot be linked under itself. NEXT: hubkit thread show {rid}")
    for m in members_of(recs, rid):  # rid was a root: its members follow it
        _set_thread(cfg, m["id"], top)
    _set_thread(cfg, rid, top)
    return f"{rid} linked under {top}"


def unlink(cfg: dict, rid: str) -> str:
    recs = records(cfg, archived=True)
    rec = _need(recs, rid, "record")
    if not rec["thread"]:
        if members_of(recs, rid):
            raise store.Refused(f"{rid} is the root of a thread; unlink its members first. NEXT: hubkit thread show {rid}")
        return f"{rid} is in no thread"
    _set_thread(cfg, rid, "")
    return f"{rid} unlinked from {rec['thread']}"


# ---- the automatic rules ----

def _explicit(text: str) -> str:
    m = FOR_LINE.search(text or "")
    return m.group(1) if m else ""


def _find_root(cfg: dict, recs: dict[str, dict], rec: dict, text: str, sole: bool, explicit_only: bool) -> tuple[str, str]:
    """(root id, why) for a record, or ('', reason). Ambiguous never links."""
    from . import owed

    if (ex := _explicit(text)):
        hit = recs.get(ex)
        if hit is None or hit["id"] == rec["id"] or thread_id(hit) == rec["id"]:
            return "", f"For: {ex} names no other record"
        return thread_id(hit), f"For: {ex}"
    if explicit_only:
        return "", ""
    prs = rec["prs"] or routed.refs(cfg, text, cap=None)
    others = [r for r in recs.values() if r["live"] and r["id"] != rec["id"] and thread_id(r) != rec["id"]]
    if prs:
        same = [r for r in others if routed.overlaps(prs, r["prs"])]
        cand = {thread_id(r) for r in same if r["kind"] in ROOT_KINDS or r["thread"]}
        if not cand:
            cand = {r["id"] for r in same if r["kind"] == "reply" and rec["kind"] != "reply"}
        if len(cand) == 1:
            return next(iter(cand)), "it names the same PR"
        if len(cand) > 1:
            return "", "ambiguous: the PR is in several threads"
    if not sole:
        return "", ""
    roots = [r for r in others if r["kind"] in ROOT_KINDS and not r["thread"] and r["state"] not in ("unconfirmed",)]
    if len(roots) == 1:
        r = roots[0]
        if prs and r["prs"] and not routed.overlaps(prs, r["prs"]):
            return "", "the only open ask names other PRs"
        return r["id"], "the only open ask"
    mine = owed.stems(text)
    fits = []
    for r in roots:
        theirs = owed.stems(r["title"])
        shared = len(mine & theirs)
        if theirs and shared >= int(opts(cfg)["min_shared"]) and shared / len(theirs) >= float(opts(cfg)["root_share"]):
            fits.append(r["id"])
    if len(fits) == 1:
        return fits[0], "it shares key words with that ask"
    return "", "ambiguous: several open asks fit" if len(fits) > 1 else ""


def auto(cfg: dict, rid: str, text: str = "", sole: bool = False, allow_root: bool = False, explicit_only: bool = False) -> list[str]:
    """Link one live record by the rules (a `For:` line, then the same PR, then the only open ask or shared key words when
    `sole`). A record that already has a thread keeps it unless a `For:` line says otherwise. Returns the lines to print."""
    recs = records(cfg)
    rec = recs.get(rid)
    if rec is None:
        return []
    text = text or rec["title"]
    if rec["kind"] in ROOT_KINDS and not allow_root:
        return []
    if rec["thread"] and not _explicit(text):
        return []
    if members_of(recs, rid):
        return []
    root, why = _find_root(cfg, recs, rec, text, sole, explicit_only)
    if root and root != rec["thread"]:
        _set_thread(cfg, rid, root)
        return [f"hubkit: {rid} linked under {root} ({why}); change it with: hubkit thread unlink {rid}"]
    if rec["kind"] == "reply" and not rec["thread"] and rec["prs"] and not root and not why.startswith("ambiguous"):
        kids = [r for r in recs.values() if r["live"] and r["kind"] == "routed" and not r["thread"] and routed.overlaps(rec["prs"], r["prs"])]
        for k in kids:  # the inbound ask is the root when nothing older exists
            _set_thread(cfg, k["id"], rid)
        if kids:
            return [f"hubkit: {', '.join(k['id'] for k in kids)} linked under {rid} (the ask from {rec['owner']} names the same PR)"]
    return [f"hubkit: {rid} not linked: {why}"] if why.startswith(("ambiguous", "For:")) else []


def autolink_pass(cfg: dict) -> int:
    """The tick: link live records that name the same PR as a thread, once (no key-word guesses). Returns how many."""
    n = 0
    for r in list(records(cfg).values()):
        if r["live"] and r["kind"] in LINKABLE and not r["thread"] and r["prs"] and any("linked under" in x for x in auto(cfg, r["id"])):
            n += 1
    return n


# ---- the view ----

def _age(now: float, then: float) -> str:
    from . import owed

    return owed.age_text(now - then)


def _step(cfg: dict, root: dict, live: list[dict], closed: list[dict]) -> tuple[str, str, str]:
    """(step in words, whose court in words, group key: user|hub|peers|others) from the thread's open members."""
    due = [m for m in live if m["kind"] == "reply" and m["due"]]
    routed_ = [m for m in live if m["kind"] == "routed"]
    if due:
        m = due[0]
        return f"reply owed to {m['owner']}: the PR work is settled", "the hub", "hub"
    if routed_:
        m = routed_[-1]
        if m["state"] in ("reported", "answered"):
            return f"{m['owner']} reported on {m['id']}; check it", "the hub", "hub"
        return f"{m['id']} with {m['owner']} ({m['state']})", m["owner"], "peers"
    work = [m for m in live if m["kind"] not in ("reply",)]
    if work:
        m = work[-1]
        return (f"waiting: {m['title'][:50]}", "others", "others") if m["kind"] == "tracked" else (f"{m['id']} in progress", "the hub", "hub")
    if live:
        m = live[0]
        return f"reply owed to {m['owner']} once the PR work is settled", m["owner"], "others"
    if root["live"] and root["state"] == "unconfirmed":
        return "the order is unconfirmed", "the user", "user"
    return "every step is closed; the root can be closed", "the hub", "hub"


def build(cfg: dict, now: float) -> list[dict]:
    """The threads (a root with at least one member, live or closed) that still have something open, oldest root first."""
    recs = records(cfg, archived=True)
    by: dict[str, list[dict]] = {}
    for r in recs.values():
        if r["thread"]:
            by.setdefault(r["thread"], []).append(r)
    out = []
    for tid, mem in by.items():
        root = recs.get(tid)
        mem = sorted(mem, key=lambda r: r["added"])
        live = [m for m in mem if m["live"]]
        if root is None:
            root = {"id": tid, "kind": "?", "live": False, "state": "unknown", "title": "(root not found)", "added": mem[0]["added"], "owner": "", "thread": "",
                    "prs": [], "due": False, "closed_at": 0.0, "note": "", "pr_states": {}, "done_when": "", "evidence": {}}
        if not (live or root["live"]):
            continue
        closed = [m for m in mem if not m["live"]]
        step, court, group = _step(cfg, root, live, closed)
        settled = root["live"] and not live and bool(closed)
        out.append({"id": tid, "kind": root["kind"], "title": root["title"], "state": root["state"], "root_live": root["live"], "added": root["added"],
                    "age": _age(now, root["added"]), "step": step, "court": court, "group": group, "settled": settled,
                    "members": [{k: m[k] for k in ("id", "kind", "state", "live", "owner", "title", "added", "prs", "note", "closed_at")} for m in mem]})
    return sorted(out, key=lambda t: t["added"])


def _member_line(m: dict) -> str:
    what = f"reply owed to {m['owner']}" if m["kind"] == "reply" and m["live"] else m["kind"] + (f" {m['owner']}" if m["owner"] else "")
    return f"    {m['id']} {what} {m['state']}"


def thread_rows(t: dict) -> list[str]:
    head = f"  {t['id']} {t['age']}: {t['title'][:80]} -> {t['step']} (on {t['court']})"
    return [head, *(_member_line(m) for m in t["members"])]


def fold_rows(cfg: dict, now: float, user: list[str], hub: list[str], peers: list[str], others: list[str]) -> list[list[str]]:
    """`hubkit mine`: one entry per thread in the group of its current step; the rows of its root and members are dropped."""
    ts = build(cfg, now)
    hide = {x for t in ts for x in (t["id"], *(m["id"] for m in t["members"]))}
    keep = [[r for r in rows if not (r.split() and r.split()[0] in hide)] for rows in (user, hub, peers, others)]
    for t in ts:
        keep[("user", "hub", "peers", "others").index(t["group"])] += thread_rows(t)
    return keep


def ls_lines(cfg: dict, now: float) -> list[str]:
    ts = build(cfg, now)
    if not ts:
        return ["no threads"]
    return [f"{t['id']} {t['age']}: {t['title'][:70]} -> {t['step']} (on {t['court']}); {len(t['members'])} step(s), "
            f"{sum(m['live'] for m in t['members'])} open" for t in ts]


def show(cfg: dict, root: str, now: float) -> tuple[list[str], dict]:
    """The full chain with times: asked, routed, PR state, reply. Returns (lines, record)."""
    recs = records(cfg, archived=True)
    rec = _need(recs, root, "record")
    root = thread_id(rec)
    rec = _need(recs, root, "root")
    t = next((x for x in build(cfg, now) if x["id"] == root), None)
    chain = sorted([rec, *members_of(recs, root)], key=lambda r: r["added"])
    verb = {"order": "asked", "request": "asked", "routed": "routed", "reply": "asked by"}
    lines = [f"{root} \"{rec['title'][:100]}\" ({rec['kind']}, {rec['state']})"]
    for r in chain:
        who = f" {r['owner']}" if r["owner"] else ""
        lines.append(f"  {store.clock(cfg, r['added'])} {verb.get(r['kind'], 'opened')}{who}: {r['id']} [{r['kind']}] {r['state']} {r['title'][:70]}")
        for u, s in (r["pr_states"] or {}).items():
            lines.append(f"      PR {u}: {s}")
        if not r["live"] and r["closed_at"]:
            lines.append(f"      closed {store.clock(cfg, r['closed_at'])}" + (f": {r['note'][:100]}" if r["note"] else ""))
        if r["kind"] == "reply" and r["live"]:
            lines.append(f"      reply owed to {r['owner']}" + (" (due now)" if r["due"] else ""))
        if r["done_when"]:
            lines.append(f"      done when: {r['done_when']}")
    if t:
        lines.append(f"now: {t['step']} (on {t['court']})")
        if t["settled"]:
            lines.append(settle_msg(cfg, rec, [m for m in chain if m is not rec]))
    else:
        lines.append("now: no open step")
    return lines, t or {"id": root, "title": rec["title"], "members": []}


# ---- closing the root ----

def _clean(text: str, n: int = 80) -> str:
    return " ".join(text.replace('"', "'").split())[:n]


def settle_msg(cfg: dict, root: dict, members: list[dict]) -> str:
    """The line printed when every step under a root is closed: what closed, and the exact command to close the root."""
    closed = [m for m in members if not m["live"]]
    what = "; ".join(f"{m['id']} {m['kind']} {m['state']}" + (f": {_clean(m['note'])}" if m["note"] else "") for m in closed[:4])
    head = f"{root['id']} \"{_clean(root['title'], 60)}\": every step under it is closed ({what})."
    notes = " ".join(m["note"] for m in closed)
    sha = (SHA.findall(notes) or [""])[0]
    url = next((u for m in closed for u in m["prs"] if u.startswith("http")), "")
    if root["kind"] == "order":
        needs = Orders(cfg).intents["kinds"].get(root.get("order_kind", ""), {}).get("done_when", [])
        val = " ".join(x for x in (url, sha) if x) or "<what shows it>"
        ev = " ".join(f'--evidence "{n}={val}"' for n in needs) or '--evidence name=value'
        return f"{head} NEXT: hubkit order close {root['id']} {ev}"
    if root["kind"] == "reply":
        return f"{head} NEXT: hubkit inbound replied {root['id']} --note \"<what you sent>\""
    summary = _clean("; ".join(f"{m['id']} {m['state']}" + (f" ({_clean(m['note'], 50)})" if m["note"] else "") for m in closed[:3]) or "all steps closed", 200)
    return f"{head} NEXT: hubkit req done {root['id']} --evidence \"{summary}\""


def settled(cfg: dict, now: float) -> list[dict]:
    """One alert per live root whose members are all closed (the tick's own back-off repeats it)."""
    recs = records(cfg, archived=True)
    return [{"key": f"{t['id']}:thread_settled", "msg": settle_msg(cfg, recs[t["id"]], members_of(recs, t["id"]))} for t in build(cfg, now) if t["settled"] and t["id"] in recs]


def settle_line_for(cfg: dict, rid: str) -> str:
    """After `work done rid`: the settle line of its thread, when that closed the last step."""
    recs = records(cfg, archived=True)
    rec = recs.get(rid)
    if rec is None or not rec["thread"] or rec["thread"] not in recs:
        return ""
    root = recs[rec["thread"]]
    mem = members_of(recs, root["id"])
    return settle_msg(cfg, root, mem) if root["live"] and mem and not any(m["live"] for m in mem) else ""


def open_members_warning(cfg: dict, root: str) -> str:
    """After a root closed: a warning naming the steps still open under it (warn, never refuse)."""
    recs = records(cfg, archived=True)
    live = [m for m in members_of(recs, root) if m["live"]]
    if not live:
        return ""
    names = ", ".join(f"{m['id']} {m['kind']}{' ' + m['owner'] if m['owner'] else ''} ({m['state']})" for m in live)
    return (f"hubkit WARNING: {root} is closed but {len(live)} step(s) under it are still open: {names}. "
            f"NEXT: hubkit thread show {root}")


# ---- declared checks for work with no PR ----

def parse_done_when(cfg: dict, text: str) -> str:
    """The text after a `Done-when:` marker line of a brief, or ''."""
    for ln in (text or "").splitlines():
        s = ln.strip().lstrip("-*>_ ").strip()
        for mk in routed.opts(cfg)["done_when_markers"]:
            if s.lower().startswith(mk.lower()):
                return store.safe(s[len(mk):].strip(" *_"), 200)
    return ""


def check_done(cfg: dict, wid: str, note: str) -> None:
    """`work done` on an item that declared a check needs a note saying what the check showed."""
    hit = next((i for i in status.work_load(store.state(cfg)) if i["id"] == wid), None)
    if hit and hit.get("done_when") and not note.strip():
        raise store.Refused(f"{wid} declared its check (Done-when: {hit['done_when']}) and is closed only with a note saying what it showed. "
                            f"NEXT: hubkit work done {wid} --note \"<what the check showed>\"")


def on_route(cfg: dict, item: dict, text: str) -> list[str]:
    """After a SendMessage or Agent brief was recorded: keep its `Done-when:`, link it, warn when nothing can show it is done."""
    out: list[str] = []
    new = len(item.get("events", [])) == 1
    dw = parse_done_when(cfg, text)
    if dw and not item.get("review"):
        sd = store.state(cfg)
        with store.locked(sd / "work.lock"):
            items = status.work_load(sd)
            hit = next((i for i in items if i["id"] == item["id"]), None)
            if hit is not None:
                hit["done_when"] = dw
                status.work_save(sd, items)
        item["done_when"] = dw
    out += auto(cfg, item["id"], text, sole=True, explicit_only=not new)
    if new and not item.get("review") and not item.get("prs") and not item.get("queue") and not item.get("done_when") \
            and routed.opts(cfg)["require_done_when"] == "warn":
        out.append(f"hubkit: {item['id']}: this job names no PR and no `Done-when:` line, so nothing can show it is done. "
                   "NEXT: add `Done-when: <command, file or URL that shows it>` to the brief")
    return out


def alerts(cfg: dict, now: float) -> list[dict]:
    return settled(cfg, now)


__all__ = ["alerts", "auto", "autolink_pass", "build", "check_done", "fold_rows", "link", "ls_lines", "on_route", "open_members_warning",
           "records", "settle_line_for", "settled", "show", "unlink"]
