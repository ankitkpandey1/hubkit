"""Worker check-ups (class `workers-not-nudged`): every open routed or active item and every recent peer, with its live
session state, sorted into reported-within-ETA / idle / overdue / waiting-on-permission-prompt / free.

TASKS he gave (priority.py) are listed first and checked on their own, shorter interval ([checkup] task_interval_min, default
15; `hubkit checkup interval <min>`; per task `hubkit work every <id> --minutes N`). Other rows keep EVERY_MIN (30).
A task's row says who holds it, whether that worker is live and inside its ETA, and the nudge; a task past one nudge is
re-briefed to another owner at once (fast track). Fast track never bypasses a guard, a permission prompt, a denial or a
deploy/merge/message rule: it only orders the lists and shortens the clocks.

Read-only. hubkit never sends a message: the hub reads the view and sends the nudges, then runs `hubkit checkup done`.
"""

from __future__ import annotations

import time

from . import routed, status, store, tasks

EVERY_MIN = 30
NOWORKER = "no-worker"
OVERRIDE_FILE = "checkup.json"
WITHIN, IDLE, OVERDUE, WAITING, FREE = (
    "reported-within-ETA",
    "idle",
    "overdue",
    "waiting-on-permission-prompt",
    "free",
)
WAITING_STATES = ("waiting", "blocked", "needs_input", "permission")
PEER_RECENT_H = 24


def every_min(cfg: dict) -> float:
    """Minutes between check-ups of the non-task rows."""
    return float(cfg.get("checkup", {}).get("interval_min", EVERY_MIN))


def set_interval(cfg: dict, minutes: int) -> str:
    """Set the task check interval for this state dir (tasks.py owns it)."""
    tasks.set_interval(cfg, minutes)
    return f"task check interval is {minutes} min (non-task rows: {int(every_min(cfg))} min)"


def _routed_state(cfg: dict, i: dict, ss: dict, now: float) -> tuple[str, str]:
    if ss.get("waiting"):
        return WAITING, "answer its prompt in that session, or re-brief"
    base = routed.base(cfg, i)
    heard = (
        f"heard {int(store.awake_min(cfg, i['last_heard'], now))}m ago"
        if i.get("last_heard")
        else "never heard"
    )
    if routed.eta_holds(cfg, i, now):
        left = int(
            float(i["estimate_min"])
            - store.awake_min(cfg, routed.started_at(cfg, i), now)
        )
        return WITHIN, f"{heard}; its ETA has {left}m left"
    est = i.get("estimate_min")
    late = routed.overdue(cfg, i, now) is not None or (
        est is not None
        and store.awake_min(cfg, routed.started_at(cfg, i), now) > float(est)
    )
    if store.awake_min(cfg, base, now) <= routed.limit(cfg, i) and not late:
        return WITHIN, heard
    return (
        (OVERDUE if late else IDLE),
        f"{heard}; silent {int(store.awake_min(cfg, base, now))}m (limit {int(routed.limit(cfg, i))})",
    )


def _own_state(i: dict, now: float) -> tuple[str, str]:
    est = i.get("estimate_min")
    spent = (now - float(i["added"])) / 60
    if est is None:
        return IDLE, f"no estimate, {int(spent)}m spent"
    return (
        (WITHIN, f"{int(spent)}m of {float(est):g}m")
        if spent <= float(est)
        else (OVERDUE, f"{int(spent)}m spent, estimate {float(est):g}m")
    )


def _nudged(cfg: dict, i: dict, now: float) -> bool:
    """The hub already chased this owner: at the current silence step, or within the last EVERY_MIN minutes."""
    lvl = routed.eff_level(cfg, i, now)
    at_step = lvl is not None and i.get("chased_level") is not None and i["chased_level"] >= lvl
    return at_step or any(store.awake_min(cfg, float(t), now) < every_min(cfg) for t in i.get("addressed", []))


def rows(cfg: dict, now: float) -> list[dict]:
    """Tasks first, then one row per open routed or active item, then one per recent live peer holding nothing (free)."""
    out: list[dict] = tasks.task_rows(cfg, now)
    owners: set[str] = set()
    for i in routed.items(cfg):
        ss = tasks.session_state(cfg, i["owner"])
        state, why = _routed_state(cfg, i, ss, now)
        owners.add(store.norm_name(i["owner"]))
        out.append(
            {
                "id": i["id"],
                "who": i["owner"],
                "item": i["title"][:50],
                "state": state,
                "why": why,
                "session": ss["text"],
                "socket": ss.get("socket"),
                "nudged": _nudged(cfg, i, now),
            }
        )
    for i in status.work_load(store.state(cfg)):
        if i.get("kind") == "active" and not i["title"].startswith("draft: "):
            state, why = _own_state(i, now)
            out.append(
                {
                    "id": i["id"],
                    "who": "hub",
                    "item": i["title"][:50],
                    "state": state,
                    "why": why,
                    "session": "this session",
                    "socket": None,
                }
            )
    for name, ts in sorted(routed.peers(cfg).items(), key=lambda kv: -float(kv[1])):
        if store.norm_name(name) in owners or now - float(ts) > PEER_RECENT_H * 3600:
            continue
        ss = tasks.session_state(cfg, name)
        if ss["live"]:
            out.append(
                {
                    "id": "-",
                    "who": name,
                    "item": "",
                    "state": WAITING if ss["waiting"] else FREE,
                    "why": f"last wrote {int((now - float(ts)) / 60)}m ago",
                    "session": ss["text"],
                    "socket": ss.get("socket"),
                }
            )
    return out


def nudge(r: dict) -> str:
    """The one action for a row. The hub sends it; hubkit never does."""
    if r.get("task"):
        return tasks.task_nudge(r["task_item"], r["state"])
    if r["state"] == WAITING:
        return f"ask the user to answer {r['who']}'s prompt, or re-brief {r['id']}"
    if r["state"] in (IDLE, OVERDUE):
        return (
            f"SendMessage {r['who']}: one-line state and a new ETA for {r['id']}"
            if r["who"] != "hub"
            else f"finish {r['id']} or re-estimate it"
        )
    return ""


def last_done(cfg: dict) -> float:
    rs = store.read_rows(store.state(cfg, create=False) / "checkups.jsonl")[0]
    return max((float(r["ts"]) for r in rs), default=0.0)


def render(cfg: dict, now: float | None = None) -> list[str]:
    now = now or time.time()
    rs = rows(cfg, now)
    last = last_done(cfg)
    out = [
        f"checkup: {len(rs)} row(s); last checkup {'never' if not last else str(int(store.awake_min(cfg, last, now))) + ' min ago (awake)'}; "
        f"tasks every {int(tasks.task_interval_min(cfg))} min, other rows every {int(every_min(cfg))} min; hubkit sends nothing, you do"
    ]
    for r in rs:
        sock = (
            ""
            if r["socket"] is None
            else (", socket ok" if r["socket"] else ", no socket")
        )
        out.append(
            f"  {r['id']:<6} {r['who'][:22]:<22} {r['state']:<28} {r['item']} | {r['why']} | session: {r['session']}{sock}"
            + (f" | TASK, done-when: {r['done_when']}" if r.get("task") else "")
        )
        if n := nudge(r):
            out.append(f"         NEXT: {n}")
    return out


def done(cfg: dict) -> str:
    """Record that the check-up was done now (the hub has sent its nudges)."""
    now = time.time()
    rs = rows(cfg, now)
    store.append_row(
        store.state(cfg) / "checkups.jsonl",
        {
            "items": len([r for r in rs if r["id"] != "-"]),
            "not_within": len(
                [r for r in rs if r["id"] != "-" and r["state"] != WITHIN]
            ),
        },
    )
    late = {r["id"]: r["state"] != WITHIN and tasks.task_cover(cfg, r["task_item"], now) is not None for r in rs if r.get("task")}  # covered by an ask or a blocker is not a missed nudge
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        items = status.work_load(sd)
        for i in items:
            if i.get("task") and i["id"] in late:
                i["checked"] = now
                i["nudges"] = int(i.get("nudges", 0)) + 1 if late[i["id"]] else 0
        status.work_save(sd, items)
    return f"checkup recorded; next due in {int(every_min(cfg))} min (tasks: {int(tasks.task_interval_min(cfg))} min)"


def due_line(cfg: dict, now: float) -> str:
    """A line when EVERY_MIN awake minutes passed without a checkup and any item is not reported-within-ETA, else empty."""
    last = last_done(cfg)
    if last and store.awake_min(cfg, last, now) < every_min(cfg):
        return ""
    bad = [r for r in rows(cfg, now) if r["id"] != "-" and not r.get("task") and r["state"] != WITHIN and not r.get("nudged")]
    if not bad:
        return ""
    names = ", ".join(r["id"] for r in bad[:8])
    return (
        f"hubkit CHECKUP DUE: {len(bad)} item(s) not reported within ETA: {names}, last checkup "
        f"{'never' if not last else str(int(store.awake_min(cfg, last, now))) + ' min ago'}. NEXT: hubkit checkup, SendMessage each idle or overdue "
        "owner for a one-line state and an ETA (the hub sends; hubkit never does), then: hubkit checkup done"
    )


def alerts(cfg: dict, now: float) -> list[dict]:
    """Watchdog: the same line, so the watch wakes the hub when a checkup is due."""
    line = due_line(cfg, now)
    return [{"key": "checkup:due", "msg": line}] if line else []  # task check alerts come from tasks.alerts (watcher.checks)


def interval_of(cfg: dict, task: dict) -> float:
    return tasks.interval_of(cfg, task)


def task_interval_min(cfg: dict) -> float:
    return tasks.task_interval_min(cfg)
