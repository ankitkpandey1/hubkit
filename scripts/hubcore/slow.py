"""Job records, estimates and the slowness check. Internal to the hub: nothing here is a user-facing report.

One record per live job (an order or a routed item): the job, its current status, when it started and when that
status began, the estimate, the holder, and that it is not done yet. Every tick rebuilds the records and checks them;
a job past its estimate (or, with no estimate, past its usual time) is SLOW, and a SLOW job with an estimate on
record blocks the stop gate until the hub addresses it.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import statistics
import subprocess
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import config, hooks, routed, store
from .orders import Orders
from .status import work_load, work_save

ESTIMATE = re.compile(r"(?im)^\s*estimate:\s*(\d{1,4})\s*m(?:in)?\b")
AGENT_ID = re.compile(r"^[0-9a-f]{12,}$")
BAD = {
    "FAILURE",
    "ERROR",
    "TIMED_OUT",
    "CANCELLED",
    "ACTION_REQUIRED",
    "STARTUP_FAILURE",
}
RUNNING = {"IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "EXPECTED"}


def parse_estimate(text: str) -> int | None:
    """Minutes from an `Estimate: <n>m` line of a brief, else None."""
    m = ESTIMATE.search(text)
    return int(m[1]) if m else None


def step_of(item: dict) -> str:
    """The step a routed item is in, by what it was asked to do."""
    return (
        "review" if item.get("review") else "fix push" if item.get("pushes") else "work"
    )


# ---- baseline: the usual duration per step, by median ----


def history(cfg: dict) -> dict[str, list[float]]:
    """Minutes taken per step key, from every archived routed item and closed order."""
    out: dict[str, list[float]] = defaultdict(list)
    for f in (store.state(cfg) / "archive").glob("archive_*.jsonl"):
        for r in store.read_rows(f)[0]:
            end = r.get("ts") or 0
            if r.get("type") == "work" and r.get("kind") == "routed":
                start = r.get("routed_at") or r.get("added") or end
                if end > start:
                    out[step_of(r)].append((end - start) / 60)
                if r.get("reported") and end > r["reported"]:
                    out["hub pickup"].append((end - r["reported"]) / 60)
            elif (
                r.get("type") == "order"
                and r.get("outcome") == "closed"
                and end > (r.get("opened") or end)
            ):
                out["order:" + str(r.get("kind"))].append((end - r["opened"]) / 60)
    return out


def usual(
    cfg: dict, hist: dict[str, list[float]], key: str, fallback: float
) -> tuple[float, int]:
    """(median minutes, samples). Too little history: the configured stall limit and the sample count it rests on."""
    xs = hist.get(key, [])
    if len(xs) >= int(cfg["slow"]["min_samples"]):
        return round(statistics.median(xs), 1), len(xs)
    return fallback, len(xs)


def item_default(cfg: dict, item: dict) -> tuple[float, str]:
    """Default estimate for a routed item and its basis, used when the brief has no Estimate line."""
    mins, n = usual(cfg, history(cfg), step_of(item), routed.limit(cfg, item))
    return mins, (
        f"median of {n}"
        if n >= int(cfg["slow"]["min_samples"])
        else f"stall limit, {n} samples"
    )


def order_default(cfg: dict, orders: Orders, o: dict) -> tuple[float, str]:
    """Default estimate for an order and its basis."""
    mins, n = usual(
        cfg, history(cfg), "order:" + o["kind"], float(cfg["slow"]["order_default_min"])
    )
    return mins, (
        f"median of {n}"
        if n >= int(cfg["slow"]["min_samples"])
        else f"configured default, {n} samples"
    )


# ---- why: real signals only, never a transcript's text ----


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def _tail(path: str, kb: int) -> list[str]:
    size = os.path.getsize(path)
    off = max(0, size - kb * 1024)
    with open(path, "rb") as fh:
        fh.seek(off)
        lines = fh.read().decode("utf-8", "replace").splitlines()
    return lines[1:] if off else lines


def _blocks(o: dict) -> list[dict]:
    c = (o.get("message") or {}).get("content")
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def activity(path: str, kb: int) -> tuple[dict[str, tuple[str, float]], int]:
    """(tool uses started and not yet answered: id -> (tool name, start epoch), unread queued messages).
    Reads tool names, ids, times and queue operations only."""
    open_tools: dict[str, tuple[str, float]] = {}
    queued = 0
    for line in _tail(path, kb):
        if "tool_use" not in line and "queue-operation" not in line:
            continue
        try:
            o = json.loads(line)
            if o.get("type") == "queue-operation":
                queued += {"enqueue": 1, "dequeue": -1, "remove": -1}.get(
                    str(o.get("operation")), 0
                )
            elif o.get("type") == "assistant":
                for b in _blocks(o):
                    if b.get("type") == "tool_use":
                        open_tools[str(b["id"])] = (
                            str(b["name"]),
                            _epoch(o["timestamp"]),
                        )
            elif o.get("type") == "user":
                for b in _blocks(o):
                    if b.get("type") == "tool_result":
                        open_tools.pop(str(b.get("tool_use_id")), None)
        except ValueError, KeyError, TypeError:
            continue
    return open_tools, max(queued, 0)


def holder_session(cfg: dict, owner: str) -> dict | None:
    """The live session file of a peer by name, or of the hub (owner "hub")."""
    if owner == "hub":
        b = hooks.read_bind(cfg)
        return hooks.session_file(cfg, int(b["pid"])) if hooks.bind_alive(b) else None
    for f in glob.glob(str(config.sessions_dir(cfg) / "*.json")):
        try:
            s = json.loads(Path(f).read_text(encoding="utf-8"))
        except OSError, ValueError:
            continue
        if s.get("name") == owner and hooks.proc_start(int(s.get("pid", 0))) == str(
            s.get("procStart")
        ):
            return s
    return None


def holder_transcript(cfg: dict, owner: str, sess: dict | None) -> str | None:
    """Transcript file of the holder's session, or of a background agent."""
    if sess:
        return routed.transcript_of(cfg, str(sess["sessionId"]))
    if AGENT_ID.match(owner):
        for pat in routed.opts(cfg)["activity_globs"]:
            hit = sorted(
                glob.glob(os.path.expanduser(pat.format(id=owner))),
                key=os.path.getmtime,
            )
            if hit:
                return hit[-1]
    return None


def ci_state(cfg: dict, url: str, timeout: float) -> str | None:
    """failing, pending or passing from the PR's checks; None when gh cannot say."""
    exe = routed._gh(cfg)
    if exe is None or not url.startswith("http"):
        return None
    try:
        r = subprocess.run(
            [exe, "pr", "view", url, "--json", "statusCheckRollup"],
            capture_output=True,
            encoding="utf-8", errors="replace",
            timeout=timeout,
            check=False,
        )
        checks = json.loads(r.stdout)["statusCheckRollup"]
    except OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError:
        return None
    if not checks:
        return None
    concl = [str(c.get("conclusion") or c.get("state") or "").upper() for c in checks]
    stat = [str(c.get("status") or "").upper() for c in checks]
    if any(x in BAD for x in concl):
        return "failing"
    return (
        "pending"
        if any(s in RUNNING for s in stat) or any(x in RUNNING or not x for x in concl)
        else "passing"
    )


def why(
    cfg: dict, owner: str, item: dict | None, now: float, deep: bool
) -> tuple[str, str]:
    """(text, signature) for a slow job. The signature has no durations, so it changes only when the cause does."""
    s = cfg["slow"]
    facts: list[str] = []  # signals that explain a delay
    sig: list[str] = []
    ctx = ""
    if owner == "unknown":
        return "unknown (no routed item is linked to this job)", "nolink"
    sess = holder_session(cfg, owner)
    if sess:
        age = int((now - float(sess.get("statusUpdatedAt", 0)) / 1000) / 60)
        ctx = f"session {sess.get('status', '?')} {age}m"
        if sess.get("waitingFor"):
            facts.append("waiting for " + store.safe(str(sess["waitingFor"]), 40))
            sig.append("waitingFor")
    elif owner != "hub" and not AGENT_ID.match(owner):
        facts.append("no live session of that name")
        sig.append("nosession")
    elif owner == "hub":
        facts.append("hub session not live")
        sig.append("nohub")
    path = holder_transcript(cfg, owner, sess)
    if path:
        try:
            tools, unread = activity(path, int(s["scan_kb"]))
            mt = os.path.getmtime(path)
        except OSError:
            tools, unread, mt = {}, 0, now
        if tools:
            name, t0 = min(tools.values(), key=lambda v: v[1])
            mins = int((now - t0) / 60)
            if mins >= int(s["tool_min"]):
                facts.append(
                    f"{name} running {mins}m"
                    if len(tools) == 1
                    else f"{len(tools)} tools running, oldest {name} {mins}m"
                )
                sig.append("tool:" + name)
        if unread:
            facts.append(f"{unread} message{'s' if unread > 1 else ''} unread")
            sig.append(f"unread:{unread}")
        if AGENT_ID.match(owner) and now - mt > 120:
            facts.append(f"transcript quiet {int((now - mt) / 60)}m")
            sig.append("quiet")
    if item:
        if any(
            e.get("kind") == "head_moved" and e["t"] >= (item.get("routed_at") or 0)
            for e in item.get("events", [])
        ):
            facts.append("PR head moved")
            sig.append("head")
        if deep and item.get("prs"):
            ci = ci_state(cfg, item["prs"][0], float(routed.opts(cfg)["gh_timeout_s"]))
            if ci in ("failing", "pending"):
                facts.append(f"CI {ci}")
                sig.append("ci:" + ci)
    text = "; ".join(facts) if facts else "unknown" + (f" ({ctx})" if ctx else "")
    return text, hashlib.sha1("|".join(sig).encode()).hexdigest()[:6]


# ---- the job records ----


def _addressed_after(its: list[dict], t: float) -> bool:
    return any(a >= t for i in its for a in i.get("addressed", []))


def _linked(cfg: dict, o: dict, live: list[dict]) -> list[dict]:
    """Routed items an order is tied to: a live item id named in the order's words or move notes (`W52`), or a PR
    the order and the item both name (same repo and number). Nothing else links them."""
    text = " ".join([o["quote"], *[n for _, n in o["moves"]]])
    ids = set(re.findall(r"\bW\d+\b", text))
    prs = [*o["refs"], *routed.refs(cfg, text, None)]
    return [i for i in live if i["id"] in ids or routed.overlaps([*i["prs"], *i.get("queue", [])], prs)]


def _owner_job(cfg: dict, item: dict, hist: dict, now: float, live_ids: set[str]) -> dict:
    """The record of one routed item."""
    reported = item.get("reported") or 0
    on_hub = bool(reported) and reported > (item.get("routed_at") or 0)
    step = "hub pickup" if on_hub else step_of(item)
    since = reported if on_hub else routed.started_at(cfg, item)
    started = routed.started_at(cfg, item)
    explicit = (not on_hub) and item.get("estimate_min") is not None
    if explicit:
        est, basis = float(item["estimate_min"]), (
            "defaulted" if item.get("estimate_defaulted") else "estimate"
        )
    else:
        est, n = usual(cfg, hist, step, routed.limit(cfg, item))
        basis = (
            "usual" if n >= int(cfg["slow"]["min_samples"]) else f"limit, {n} samples"
        )
    clock = since if on_hub else started
    thresh = clock + est * 60 * (1 + (0 if on_hub else len(item.get("addressed", []))))
    return {
        "id": item["id"],
        "type": "routed",
        "job": item["title"][:50],
        "status": step,
        "started": started,
        "since": since,
        "estimate": est,
        "basis": basis,
        "holder": "hub" if on_hub else item["owner"],
        "done": False,
        "over": item.get("blocked_on") not in live_ids and now > thresh and (now - clock) > float(cfg["slow"]["floor_min"]) * 60,
        "gate": explicit,
        "owner": item["owner"],
    }


def _order_job(
    cfg: dict, orders: Orders, o: dict, live: list[dict], hist: dict, now: float
) -> dict | None:
    if o["state"] not in ("open", "held"):
        return None
    if o["state"] == "held":
        return {
            "id": o["id"],
            "type": "order",
            "job": o["kind"],
            "status": "held by user",
            "started": o["opened"],
            "since": o["opened"],
            "estimate": None,
            "basis": "",
            "holder": "user",
            "done": False,
            "over": False,
            "gate": False,
            "owner": "user",
        }
    link = _linked(cfg, o, live)
    holder = "/".join(dict.fromkeys(i["owner"] for i in link)) if link else "unknown"
    explicit = o.get("estimate_min") is not None
    if explicit:
        est = float(o["estimate_min"])
        basis = "defaulted" if o["estimates"][-1]["source"] == "default" else "estimate"
    else:
        est, basis = order_default(cfg, orders, o)[0], "usual"
    thresh = o["opened"] + est * 60
    last = max([o["opened"], *[t for t, _ in o["moves"]]])
    note = o["moves"][-1][1][:40] if o["moves"] else "no move yet"
    return {
        "id": o["id"],
        "type": "order",
        "job": o["kind"],
        "status": f"open, {note}",
        "started": o["opened"],
        "since": last,
        "estimate": est,
        "basis": basis,
        "holder": holder,
        "done": False,
        "over": now > thresh and not _addressed_after(link, thresh),
        "gate": explicit,
        "owner": holder,
    }


def jobs(cfg: dict, orders: Orders, now: float, deep: bool = False) -> list[dict]:
    """One record per live order and routed item; an overrun job also carries why, sig and its NEXT."""
    hist = history(cfg)
    live = routed.items(cfg)
    out = [
        j for o in orders.live() if (j := _order_job(cfg, orders, o, live, hist, now))
    ]
    out += [_owner_job(cfg, i, hist, now, {x['id'] for x in live}) for i in live]
    by_id = {i["id"]: i for i in live}
    for j in out:
        if j["over"]:
            j["why"], j["sig"] = why(cfg, j["owner"], by_id.get(j["id"]), now, deep)
            j["next"] = next_action(cfg, j, by_id.get(j["id"]), now)
    return out


def next_action(cfg: dict, job: dict, item: dict | None, now: float) -> str:
    """The one thing the hub does about an overrun, using the existing rule on re-briefing a reporting owner."""
    est = f"or hubkit work estimate {job['id']} --minutes N --why \"<reason>\" (N above the {int((now - job['started']) / 60)}m spent)"
    if job["holder"] == "unknown":
        return f"find the holder and name its item id (W<n>) in an order move note, or act on it, {est}"
    if job["holder"] == "hub":
        return f"act on it now, {est}"
    o = job["owner"]
    heard = (item or {}).get("last_heard")
    limit = routed.limit(cfg, item) if item else float(routed.opts(cfg)["stall_min"])
    if heard and now - heard < limit * 60:
        return f"SendMessage {o} for an ETA (it reported {int((now - heard) / 60)} min ago, so do not re-brief it), {est}"
    return f"SendMessage {o} for a one-line state, or re-brief to another owner, {est}"


def line(job: dict, now: float) -> str:
    """The SLOW line for an overrun job."""
    mins = (
        int((now - job["since"]) / 60)
        if job["status"] == "hub pickup"
        else int((now - job["started"]) / 60)
    )
    word = "estimate" if job["basis"] in ("estimate", "defaulted") else "usual"
    return (
        f"SLOW {job['id']} {job['status']} {mins}m, {word} {job['estimate']:g}m ({job['basis']}); holder {job['holder']}; "
        f"why: {job['why']}. NEXT: {job['next']}"
    )


def alerts(cfg: dict, orders: Orders, now: float) -> list[dict]:
    """Tick alerts: one per overrun job, keyed by the cause so a repeat is back-off and a new cause is a new alert."""
    return [
        {"key": f"{j['id']}:slow:{j['sig']}", "msg": line(j, now)}
        for j in jobs(cfg, orders, now, deep=True)
        if j["over"]
    ]


def gate_lines(cfg: dict, now: float) -> list[str]:
    """Stop-gate reasons: overrun jobs that carry an estimate on record and are not addressed."""
    return [
        "hubkit OVERRUN: " + line(j, now)
        for j in jobs(cfg, Orders(cfg), now)
        if j["over"] and j["gate"]
    ]


def block_lines(st_jobs: list[dict], now: float) -> list[str]:
    """One short line per live job for the status block (hub context)."""
    out = []
    for j in st_jobs:
        t = int((now - j["since"]) / 60)
        est = f"/{j['estimate']:g}m" if j.get("estimate") is not None else ""
        s = f"job {j['id']} {j['status'][:28]} {t}m{est} holder {j['holder']}"
        if j.get("over"):
            s += f" SLOW: {j.get('why', '')[:60]}"
        out.append(s)
    return out


# ---- re-estimate: recorded beside the old one, never over it ----


def re_estimate(
    cfg: dict, jid: str, minutes: int, reason: str, now: float | None = None
) -> str:
    """Record a new estimate with its reason; refused when it does not exceed the time already spent."""
    now = now or time.time()
    if not reason.strip():
        raise ValueError("a new estimate needs --why")
    if jid.startswith("O-"):
        o = Orders(cfg).get(jid)
        if o["state"] not in ("open", "held"):
            raise ValueError(f"{jid} is {o['state']}; only a live order takes an estimate")
        spent = (now - o["opened"]) / 60
        _need_more(jid, minutes, spent)
        Orders(cfg).estimate(jid, minutes, "hub", reason)
        return (
            f"{jid} estimate {minutes}m recorded ({len(o['estimates']) + 1} on record)"
        )
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        items = work_load(sd)
        hit = next(
            (i for i in items if i["id"] == jid and i.get("kind") == "routed"), None
        )
        if hit is None:
            raise KeyError(f"no live routed item {jid}")
        _need_more(jid, minutes, (now - routed.started_at(cfg, hit)) / 60)
        hit["estimates"] = [
            *hit.get("estimates", []),
            {"t": now, "min": minutes, "source": "hub", "why": store.safe(reason, 200)},
        ]
        hit.update(estimate_min=minutes, estimate_defaulted=False, addressed=[])
        work_save(sd, items)
    return f"{jid} estimate {minutes}m recorded ({len(hit['estimates'])} on record)"


def _need_more(jid: str, minutes: int, spent: float) -> None:
    if minutes <= spent:
        raise ValueError(
            f"{jid}: a new estimate must exceed the {int(spent)}m already spent; chase the holder or re-brief instead"
        )


# ---- end of day: estimate vs actual, for the hub's own learning ----


def reflection_lines(cfg: dict, day: str) -> list[str]:
    """Estimate vs actual per kind for items finished on `day`, and how often they were re-estimated."""
    rows = store.read_rows(store.state(cfg) / "archive" / f"archive_{day}.jsonl")[0]
    per: dict[str, list[tuple[float, float, int]]] = defaultdict(list)
    for r in rows:
        ests = r.get("estimates") or []
        if not ests:
            continue
        start = (
            r.get("routed_at") or r.get("added")
            if r.get("type") == "work"
            else r.get("opened")
        )
        key = step_of(r) if r.get("type") == "work" else "order:" + str(r.get("kind"))
        per[key].append((float(ests[0]["min"]), (r["ts"] - start) / 60, len(ests) - 1))
    if not per:
        return ["  estimate vs actual: nothing finished today carried an estimate"]
    out = ["  estimate vs actual (hub's own; first estimate vs minutes taken):"]
    for k, v in sorted(per.items()):
        out.append(
            f"    {k}: {len(v)} done, estimated {statistics.mean(x[0] for x in v):.0f}m, took {statistics.mean(x[1] for x in v):.0f}m, "
            f"over estimate {sum(x[1] > x[0] for x in v)}, re-estimated {sum(x[2] for x in v)}x"
        )
    return out
