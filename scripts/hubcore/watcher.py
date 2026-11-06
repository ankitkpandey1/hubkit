"""Checks that run in every tick (cron or watch), outside the hub session: the hub itself, peers stuck on a prompt,
the watch and cron checking each other, and the user notification."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time

from . import hooks, routed, slow, store
from .orders import Orders

USER_KEYS = ("hub:", "peer:")  # alerts that go to the user's desktop when no notify_cmd is configured
WAITING_STATES = ("waiting", "blocked", "needs_input", "permission")
HUB_SEEN = ("prompt", "stop", "routed", "answer", "session")


def live_jobs(cfg: dict, orders: Orders) -> int:
    """Routed items plus open orders: the work that needs a hub."""
    return len(routed.items(cfg)) + sum(o["state"] == "open" for o in orders.live())


def _last_hub_activity(sd: store.Path) -> float:
    """Latest heartbeat the hub's own hooks touched."""
    return max([(sd / "seen" / n).stat().st_mtime for n in HUB_SEEN if (sd / "seen" / n).exists()] or [0.0])


def hub_checks(cfg: dict, orders: Orders, now: float) -> list[dict]:
    """The hub is gone, stuck on one status, or has not acted on an alert, while jobs are live."""
    n = live_jobs(cfg, orders)
    if not n:
        return []
    w = cfg["watcher"]
    b = hooks.read_bind(cfg)
    if not hooks.bind_alive(b):
        return [{"key": "hub:dead", "msg": f"hub session is not running and {n} job{'s' if n > 1 else ''} are live. "
                 "NEXT: start the hub session and run hubkit bind"}]
    out: list[dict] = []
    sess = hooks.session_file(cfg, int(b["pid"])) or {}
    since = float(sess.get("statusUpdatedAt", 0)) / 1000
    if sess.get("status") == "busy" and since and now - since > float(w["stuck_min"]) * 60:
        out.append({"key": "hub:stuck", "msg": f"hub session has been busy for {int((now - since) / 60)} min with {n} live jobs; "
                    "check it is not stuck on a prompt"})
    act = _last_hub_activity(store.state(cfg))
    old = [r for r in store.read_rows(store.state(cfg) / "alerts.jsonl")[0]
           if r["ts"] > act and now - r["ts"] > float(w["unhandled_min"]) * 60 and not str(r.get("key", "")).startswith("hub:")]
    if old:
        first = min(old, key=lambda r: r["ts"])
        out.append({"key": "hub:unhandled", "msg": f"alert unhandled for {int((now - first['ts']) / 60)} min and the hub has not acted since "
                    f"({str(first.get('msg', ''))[:70]})"})
    return out


def peer_checks(cfg: dict, now: float) -> list[dict]:
    """A peer holding a live item sits on a permission prompt or waits for input (session status / waitingFor)."""
    out = []
    for owner in dict.fromkeys(i["owner"] for i in routed.items(cfg)):
        sess = slow.holder_session(cfg, owner)
        if not sess or not (sess.get("waitingFor") or str(sess.get("status")) in WAITING_STATES):
            continue
        mins = int((now - float(sess.get("statusUpdatedAt", 0)) / 1000) / 60)
        if mins >= int(cfg["watcher"]["waiting_min"]):
            ids = ",".join(i["id"] for i in routed.items(cfg) if i["owner"] == owner)
            what = store.safe(str(sess.get("waitingFor") or sess.get("status")), 40)
            out.append({"key": f"peer:{owner}:waiting", "msg": f"{owner} has waited {mins} min on {what} (holds {ids}). "
                        f"NEXT: ask the user to answer it in that session, or re-brief"})
    return out


def liveness_checks(cfg: dict, orders: Orders) -> list[dict]:
    """Watch and cron check each other: work is live and no watch runs, or cron is configured but has gone stale."""
    from . import cli, gate

    out = []
    started = [routed.started_at(cfg, i) for i in routed.items(cfg)] + [o["opened"] for o in orders.live() if o["state"] == "open"]
    grace = float(cfg["watcher"]["watch_grace_min"]) * 60  # the stop gate makes the hub arm a watch; give it a moment
    if started and time.time() - min(started) > grace and not gate.watch_live(cfg):
        out.append({"key": "watch:dead", "msg": f"hubkit watch is not running and work is live. NEXT: start in the background: {gate.WATCH_CMD}"})
    f = store.state(cfg) / "seen" / "cron"
    limit = float(cfg["watchdog"]["cron_stale_min"])
    quiet_min = (time.time() - f.stat().st_mtime) / 60 if f.exists() else (time.time() - min(started)) / 60 if started else 0.0
    if quiet_min > limit and cli.cron_configured():  # never written counts from the oldest live job
        out.append({"key": "cron:dead", "msg": f"cron has not run hubkit for over {int(limit)} min. NEXT: check the crontab line and cron's mail"})
    return out


def checks(cfg: dict, orders: Orders, now: float) -> list[dict]:
    """Every watcher alert that holds now."""
    return [*hub_checks(cfg, orders, now), *peer_checks(cfg, now), *liveness_checks(cfg, orders), *stale_agent_checks(cfg, now)]


def stale_agent_checks(cfg: dict, now: float) -> list[dict]:
    """A background agent reported finished long ago and its item is still listed (a review item waits on its PR)."""
    limit = float(cfg["watcher"]["stale_agent_min"])
    return [
        {"key": f"agent:{i['owner']}:stale", "msg": f"agent {i['owner']} finished {int((now - i['reported']) / 60)} min ago and {i['id']} is still listed. "
         "NEXT: close the item (hubkit work done) or act on the PR it waits for"}
        for i in routed.items(cfg)
        if i.get("tool_use_id") and i.get("reported") and (now - float(i["reported"])) / 60 > limit
    ]


def desktop_argv(msg: str, platform: str = sys.platform) -> list[str] | None:
    """The default desktop notification command for this platform, or None when no tool is installed."""
    if platform.startswith("linux") and shutil.which("notify-send"):
        return ["notify-send", "-u", "critical", "-a", "hubkit", "hubkit", msg]
    if platform == "darwin" and shutil.which("osascript"):
        safe = msg.replace("\\", " ").replace('"', "'")
        return ["osascript", "-e", f'display notification "{safe}" with title "hubkit"']
    return None


def notify(cfg: dict, new: list[dict]) -> None:
    """Tell the user's desktop about new alerts. `tools.notify_cmd` (every alert) wins; with none set, the platform's
    default is used for the alerts that are about the hub or a stuck peer. Never posts to chat."""
    cmd = cfg["tools"]["notify_cmd"]
    if cmd:
        argv: list[str] | None = [*shlex.split(cmd), "hubkit: " + "; ".join(a["msg"] for a in new)]
    else:
        mine = [a["msg"] for a in new if str(a["key"]).startswith(USER_KEYS)]
        argv = desktop_argv("; ".join(mine)) if mine else None
    if not argv or (not cmd and not cfg["watcher"]["notify_user"]):
        return
    try:
        subprocess.run(argv, timeout=5, check=False)
    except OSError, subprocess.SubprocessError:
        pass


def excused(cfg: dict, item: dict, now: float) -> str | None:
    """Why a silent item is not a stall: it is blocked on another live job, or its holder is visibly working
    (session busy and its transcript growing). An active holder is excused for 3x the limit only."""
    live = {i["id"] for i in routed.items(cfg)}
    if item.get("blocked_on") in live:
        return f"blocked on {item['blocked_on']}"
    if (now - routed.base(cfg, item)) / 60 > 3 * routed.limit(cfg, item):
        return None
    owner = item["owner"]
    sess = slow.holder_session(cfg, owner)
    path = slow.holder_transcript(cfg, owner, sess)
    if sess and str(sess.get("status")) == "busy" and path:
        try:
            if now - os.path.getmtime(path) < float(cfg["watcher"]["active_s"]):
                return "holder is working"
        except OSError:
            return None
    return None


def set_blocked(cfg: dict, wid: str, on: str) -> str:
    """Link a routed item to the job it waits on (empty `on` clears it)."""
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        items = routed.work_load(sd)
        hit = next((i for i in items if i["id"] == wid and i.get("kind") == "routed"), None)
        if hit is None:
            raise KeyError(f"no live routed item {wid}")
        if on and on == wid:
            raise ValueError("an item cannot block on itself")
        if on and not any(i["id"] == on for i in items):
            raise KeyError(f"no live item {on}")
        hit["blocked_on"] = on
        routed.work_save(sd, items)
    return f"{wid} blocked on {on}" if on else f"{wid} no longer blocked"
