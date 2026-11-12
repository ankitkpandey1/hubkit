"""Live work file, derived status.json, tick (watchdog), mep adapter, hook-error scan, EOD."""

from __future__ import annotations

import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import config, learnings, store
from .orders import Orders
from .redact import redact

VERSION = 1
WORK_KINDS = ("active", "tracked", "priority", "routed", "ask", "reply")
STALE_MIN = 15
MAX_BLOCK_LINES = 25


# ---- live work file: only live items; finished items move to archive_<day> ----

def work_path(sd: Path) -> Path:
    """File holding live work items."""
    return sd / "work.json"


def work_load(sd: Path) -> list[dict]:
    """Live work items from the state dir."""
    p = work_path(sd)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def work_save(sd: Path, items: list[dict]) -> None:
    """Write live work items atomically."""
    store.write_json_atomic(work_path(sd), items)


def next_work_id(sd: Path, items: list[dict]) -> str:
    """The next work id (W1, W2, ...). Ids are never reused: the highest id ever issued is kept in `work.seq`
    (first seeded from the live items and the archive), so an archived item's id does not come back for another job.
    Call it under the work lock."""
    seq = sd / "work.seq"
    try:
        last = int(seq.read_text(encoding="utf-8").strip())
    except ValueError, OSError:
        archived = [r.get("id", "") for f in (sd / "archive").glob("archive_*.jsonl") for r in store.read_rows(f)[0] if r.get("type") == "work"]
        last = max([int(x[1:]) for x in archived if re.fullmatch(r"W\d+", str(x))] + [0])
    nxt = max([int(i["id"][1:]) for i in items] + [last]) + 1
    store.write_atomic(seq, str(nxt))
    return f"W{nxt}"


def work_add(cfg: dict, kind: str, title: str, owner: str = "", note: str = "", estimate: int | None = None) -> dict:
    """Add a live work item and return it."""
    from .redact import redact_obj

    if kind not in WORK_KINDS:
        raise ValueError(f"kind must be one of {WORK_KINDS}")
    sd = store.state(cfg)
    if kind == "ask" and not note.strip():
        raise ValueError("an ask needs --note: the ordered work it blocks. A question that blocks nothing is not asked")
    with store.locked(sd / "work.lock"):
        items = work_load(sd)
        again = next((i for i in items if kind == "ask" and i.get("kind") == "ask" and i["title"].strip().lower() == title.strip().lower()), None)
        if again is not None:  # asking the same thing again is counted, not a new item
            again["asked"] = int(again.get("asked", 1)) + 1
            work_save(sd, items)
            return again
        item = redact_obj({"id": next_work_id(sd, items), "kind": kind, "title": title, "owner": owner,
                           "note": note, "added": time.time(),
                           **({"estimate_min": estimate, "estimates": [{"t": time.time(), "min": estimate, "source": "brief"}]} if estimate else {})})
        items.append(item)
        work_save(sd, items)
    return item


def work_verify(cfg: dict, wid: str) -> str:
    """Stamp a work item as checked against reality just now."""
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        items = work_load(sd)
        hit = next((i for i in items if i["id"] == wid), None)
        if hit is None:
            raise KeyError(f"no live item {wid}")
        hit["verified"] = time.time()
        work_save(sd, items)
    return f"{wid} verified {store.clock(cfg, hit['verified'])}"


def work_done(cfg: dict, wid: str, note: str = "") -> str:
    """Archive a work item with a note; returns a confirmation line."""
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        items = work_load(sd)
        hit = next((i for i in items if i["id"] == wid), None)
        if hit is None:
            raise KeyError(f"no live item {wid}")
        now = time.time()
        day = store.day_of(cfg, now)
        from . import roster

        roster.record_job(cfg, hit, note, now)
        store.append_row(sd / "archive" / f"archive_{day}.jsonl",
                         {"type": "work", **hit, "outcome": "done", "done_note": note, "ts": now})
        work_save(sd, [i for i in items if i["id"] != wid])
    return f"{wid} archived to archive_{day}"


# ---- adapters ----

def mep_binary(cfg: dict) -> str | None:
    """Resolve the mep CLI: configured path or name, PATH, then common user bin dirs (cron has a short PATH)."""
    want = os.path.expanduser(cfg["tools"]["mep"])
    if os.sep in want:
        return want if os.access(want, os.X_OK) else None
    found = shutil.which(want)
    if found:
        return found
    for d in ("~/.local/bin", "/usr/local/bin", "~/bin"):
        cand = os.path.join(os.path.expanduser(d), want)
        if os.access(cand, os.X_OK):
            return cand
    return None


def mep_state(cfg: dict, prev: dict | None = None) -> dict:
    """mep status. A slow or failing call keeps the last good answer, labelled stale, instead of UNVERIFIED."""
    if not cfg["mep"]["enabled"]:
        return {"state": "off"}
    env = dict(os.environ)
    if cfg["mep"].get("data_dir"):
        env["MISEENPLACE_DIR"] = os.path.expanduser(cfg["mep"]["data_dir"])
    exe = mep_binary(cfg)
    try:
        if exe is None:
            raise FileNotFoundError("mep not found")
        r = subprocess.run([exe, "status", "--json"], capture_output=True, encoding="utf-8", errors="replace",
                           timeout=cfg["mep"]["timeout_s"], env=env)
        titles = [i.get("title", "") for i in json.loads(r.stdout)["on_you"]]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as e:
        if prev and prev.get("state") in ("ok", "stale"):
            return {**prev, "state": "stale", "why": type(e).__name__,
                    "good_at": prev.get("good_at", prev.get("at", time.time()))}
        why = "mep CLI not found; it is optional, set [mep] enabled = false in config.toml" if exe is None else type(e).__name__
        return {"state": "UNVERIFIED", "why": why}
    return {"state": "ok", "count": len(titles), "titles": titles[:5], "good_at": time.time()}


def _is_our_gate(att: dict) -> bool:
    """The Stop gate blocks on purpose (exit 0 with a decision); the harness logs that as a blocking error, but it
    is the hook working, not failing."""
    msg = att.get("blockingError")
    text = msg.get("blockingError", "") if isinstance(msg, dict) else ""
    return att.get("hookEvent") == "Stop" and str(text).startswith("hubkit")


def hook_errors(cfg: dict) -> dict:
    """Scan recent transcripts for failures of our hooks. Lives outside the hooks."""
    cutoff = time.time() - cfg["watchdog"]["transcript_scan_hours"] * 3600
    marker = "hubkit"
    count, last = 0, ""
    kinds = ("hook_non_blocking_error", "hook_cancelled", "hook_blocking_error")
    for f in glob.glob(os.path.expanduser(cfg["paths"]["transcripts_glob"])):
        try:
            if os.path.getmtime(f) < cutoff:
                continue
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if marker in line and any(k in line for k in kinds):
                        try:
                            att = json.loads(line).get("attachment", {})
                        except ValueError:
                            continue
                        cmd = json.dumps(att)
                        if att.get("type") in kinds and marker in cmd and not _is_our_gate(att):
                            count += 1
                            last = json.loads(line).get("timestamp", last)
        except OSError:
            continue
    return {"count": count, "last": last}


def extensions(cfg: dict) -> list:
    """Load the user's ext/*.py modules; a broken one is logged and skipped."""
    d = config.path_of(cfg, "extensions_dir", Path(cfg["_config_dir"]) / "ext")
    mods = []
    for f in sorted(d.glob("*.py")) if d.is_dir() else []:
        spec = importlib.util.spec_from_file_location(f"hubkit_ext_{f.stem}", f)
        if spec is None or spec.loader is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
            mods.append(mod)
        except Exception as e:  # a broken extension must not break the tick
            store.append_row(config.state_dir(cfg) / "hook-errors.jsonl",
                             {"where": f"ext:{f.name}", "error": repr(e)[:200]})
    return mods


# ---- status.json ----

def block_learnings(cfg: dict, lpath: Path) -> list[dict]:
    """Learnings for the block: seen in the last 30 days, 5 at most; addressed ones are on probation 14 days."""
    now = time.time()
    out = []
    for b in learnings.recent(lpath, 40):
        last = learnings.last_seen(b)
        if not last or (now - time.mktime(time.strptime(last, "%Y-%m-%d"))) > 30 * 86400:
            continue
        addr = learnings.addressed(b)
        tag = ""
        if learnings.field(b, "ineffective"):
            tag = " (INEFFECTIVE: addressed yet seen again; enforce in code or rewrite)"
        elif addr:
            if (now - time.mktime(time.strptime(addr[0], "%Y-%m-%d"))) > 14 * 86400:
                continue
            tag = " (probation: addressed " + addr[0] + ")"
        out.append({"cls": learnings.block_class(b) + tag, "right": learnings.field(b, "right")})
    return out[-5:]



def build_status(cfg: dict) -> dict:
    """Assemble the status document: work, orders, mep, learnings, hook errors."""
    sd = store.state(cfg)
    orders = Orders(cfg)
    lpath = learnings.default_file(cfg)
    recent = block_learnings(cfg, lpath)
    extra: list[str] = []
    for mod in extensions(cfg):
        if hasattr(mod, "status_lines"):
            try:
                extra += [str(x) for x in mod.status_lines(cfg)]
            except Exception as e:
                extra.append(f"extension {mod.__name__} failed: {type(e).__name__}")
    return {
        "version": VERSION, "built": time.time(), "work": work_load(sd),
        "orders": [orders.status_line(o) for o in orders.live()],
        "mep": mep_state(cfg, (read_status(cfg) or {}).get("mep")), "learnings": recent, "regressed": learnings.regressed(lpath), "hook_errors": hook_errors(cfg),
        "extra": extra, "jobs": job_records(cfg, orders), "watcher": watcher_lines(cfg, orders), "decisions": decisions_tail(cfg),
    }


def decisions_tail(cfg: dict, n: int = 5) -> list[str]:
    """The last lines of the active slug's decisions.md (`[think] decisions`), so a ruling survives a restart."""
    path = cfg["think"]["decisions"]
    if not path:
        return []
    try:
        lines = [x.strip() for x in Path(path).expanduser().read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        return []
    return [store.safe(x, 200) for x in lines[-n:]]


def watcher_lines(cfg: dict, orders: Orders) -> list[str]:
    """What the outside watcher finds right now, for the hub to see (a failure is logged and shows nothing)."""
    from . import watcher

    try:
        return [f"watcher: {a['msg']}" for a in watcher.checks(cfg, orders, time.time())]
    except Exception as e:
        store.append_row(config.state_dir(cfg) / "hook-errors.jsonl", {"where": "watcher_lines", "error": repr(e)[:200]})
        return []


def job_records(cfg: dict, orders: Orders) -> list[dict]:
    """The live job records (see slow.py). A failure is logged and leaves the block without them."""
    from . import slow

    try:
        return slow.jobs(cfg, orders, time.time(), deep=True)
    except Exception as e:
        store.append_row(config.state_dir(cfg) / "hook-errors.jsonl", {"where": "job_records", "error": repr(e)[:200]})
        return []


def write_status(cfg: dict) -> dict:
    """Build status.json, write it and return it."""
    sd = store.state(cfg)
    st = build_status(cfg)
    store.write_json_atomic(sd / "status.json", st)
    return st


def read_status(cfg: dict) -> dict | None:
    """The last status.json, or None."""
    p = store.state(cfg, create=False) / "status.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def render_block(cfg: dict, orders: Orders, st: dict | None, now: float | None = None) -> str:
    """The text block printed at session start and with each prompt."""
    now = now or time.time()
    out = [f"hubkit: you are the {cfg['hub']['name']} session for {cfg['user']['name']}. "
           "Re-read the hub SKILL.md and the overlay before acting."]
    if st is None:
        out.append("status: UNVERIFIED (no status.json yet; run `hubkit tick`)")
    else:
        out += [f"hubkit REGRESSED {c}: the rule did not hold; add a check or rewrite it" for c in st.get("regressed", [])]
        age = int((now - st["built"]) / 60)
        out.append(f"status built {age} min ago" + (f": UNVERIFIED, stale over {STALE_MIN} min" if age > STALE_MIN else ""))
        for kind in ("priority", *(k for k in WORK_KINDS if k != "priority")):
            if kind in ("routed", "reply"):
                continue  # listed below with owner, age and last-heard / PR state
            mine = [i for i in st["work"] if i["kind"] == kind]
            if mine:
                ahead = ""
                if kind == "priority" and any(i["kind"] == "active" for i in st["work"]):
                    ahead = " | AHEAD: active work is running while this waits; switch, or say why not"
                out.append(f"{kind}: " + "; ".join(f"{i['id']} {i['title']} (checked {int((now - float(i.get('verified') or i['added'])) / 60)}m ago)" for i in mine) + ahead)
        from . import replies, routed, trust

        out += routed.block_lines(cfg, st["work"], now)
        out += replies.block_lines(st["work"], now)
        from . import owed

        out += owed.block_lines(st["work"], now, int(cfg["requests"]["block_lines"]))
        from . import sweeps

        out += sweeps.block_lines(cfg, st["work"], now)
        out += trust.lines(cfg)
        from . import roster

        out += roster.block_lines(cfg, now)
        m = st["mep"]
        if m["state"] in ("ok", "stale"):
            out.append(f"on the user (mep): {m['count']}; first: " + " | ".join(m["titles"][:3])
                       + (f" (STALE: last good {int((now - m['good_at']) / 60)} min ago, now {m['why']})" if m["state"] == "stale" else ""))
        elif m["state"] != "off":
            out.append(f"mep: {m['state']} ({m.get('why', '')})")
        he = st["hook_errors"]
        out.append(f"hook errors in scan window: {he['count']}" + (f" (last {he['last']})" if he["last"] else ""))
        from . import gate, slow

        out += slow.block_lines(st.get("jobs", []), now)
        if st.get("jobs") and not gate.watch_live(cfg):
            out.append(f"hubkit watch is NOT running with live jobs. NEXT: start in the background: {gate.WATCH_CMD}")
        if st.get("decisions"):
            out.append("recent decisions (decisions.md):")
            out += ["  " + x for x in st["decisions"]]
        out += st.get("watcher", [])
        out += st.get("extra", [])
        if st["learnings"]:
            out.append("recent learnings:")
            out += [f"  {x['cls']}: {x['right'][:140]}" for x in st["learnings"]]
    live = orders.live()
    out.append(f"open orders: {len(live)}")
    out += ["  " + orders.status_line(o, now) + f' | asked: "{o["quote"][:100]}"' for o in live]
    cap = MAX_BLOCK_LINES
    if len(out) > cap:
        out = out[: cap - 1] + [f"... {len(out) - cap + 1} more lines cut; run hubkit status"]
    return "\n".join(out)


# ---- tick: watchdog ----

def _gap(n: int) -> float:
    return min(1800.0, 600.0 * 2 ** max(n - 1, 0)) if n else 0.0


def held_rows(orders: Orders, oid: str) -> list[dict]:
    """Ledger `hold` rows for an order."""
    return [r for r in store.read_rows(orders.path)[0] if r.get("id") == oid and r.get("op") == "hold"]


def own_work_alerts(cfg: dict, now: float) -> list[dict]:
    """The hub's own work (active, priority) needs an estimate, and an overrun one is said out loud."""
    out = []
    for i in work_load(store.state(cfg)):
        if i.get("kind") not in ("active", "priority"):
            continue
        age = (now - i["added"]) / 60
        est = i.get("estimate_min")
        if est is None and age > 5:
            out.append({"key": f"{i['id']}:no_estimate", "msg": f"{i['id']} {i['title'][:50]}: own work with no estimate after {int(age)} min. NEXT: hubkit work estimate {i['id']} --minutes N --why \"<basis>\""})
        elif (now - float(i.get("verified") or i["added"])) / 60 > float(cfg["watcher"]["verify_stale_min"]) and age > float(cfg["watcher"]["verify_stale_min"]):
            out.append({"key": f"{i['id']}:unverified", "msg": f"{i['id']} {i['title'][:50]}: last checked against reality {int((now - float(i.get('verified') or i['added'])) / 60)} min ago. NEXT: check it, then: hubkit work verify {i['id']}"})
        elif est is not None and age > float(est):
            out.append({"key": f"{i['id']}:own_over", "msg": f"{i['id']} {i['title'][:50]}: {int(age)} min spent, estimate {est:g}. NEXT: finish it, or re-estimate with a reason: hubkit work estimate {i['id']} --minutes N --why \"..\""})
    return out


def evaluate(cfg: dict, orders: Orders, hook_err: dict, now: float) -> list[dict]:
    """Watchdog conditions that hold now, as alert dicts."""
    found: list[dict] = []
    ttl = float(orders.intents.get("intake", {}).get("unconfirmed_ttl_min", 30))
    held_max = float(orders.intents.get("intake", {}).get("held_review_min", 120))
    for o in orders.live():
        age_min = (now - o["opened"]) / 60
        if o["state"] == "unconfirmed" and age_min > ttl:
            found.append({"key": f"{o['id']}:unconfirmed_expired", "action": ("expire", o["id"]),
                          "msg": f"{o['id']} {o['kind']}: unconfirmed for {int(age_min)} min, expired"})
        if o["state"] == "held":
            since = max([o["opened"]] + [r["ts"] for r in held_rows(orders, o["id"])])
            if (now - since) / 60 > held_max:
                found.append({"key": f"{o['id']}:held_long",
                              "msg": f"{o['id']} {o['kind']}: held for {int((now - since) / 60)} min; resume or cancel it"})
        if o["state"] != "open":  # held and unconfirmed orders have no clock
            continue
        spec = orders.intents["kinds"].get(o["kind"], {})
        age = (now - o["opened"]) / 60
        if not (o["moves"] or o["results"]) and age > spec.get("first_action_min", 5):
            found.append({"key": f"{o['id']}:no_first_action",
                          "msg": f"{o['id']} {o['kind']}: no first action after {int(age)} min"})
        idle = (now - orders.last_movement(o)) / 60
        if idle > spec.get("stall_min", 10):
            found.append({"key": f"{o['id']}:stalled",
                          "msg": f"{o['id']} {o['kind']}: no movement for {int(idle)} min"})
        why = orders.blocker_problem(o, now)
        if why:
            found.append({"key": f"{o['id']}:blocker:{'nofix' if not o['blocker']['fix'] else 'stale'}",
                          "msg": f"{why}. NEXT: start the fix on a free owner and run: hubkit order block {o['id']} --on \"..\" --fix \"..\" "
                                 f"--holder <who>; when it ran: hubkit order result {o['id']} --note \"..\""})
        if o.get("deadline") and now > o["deadline"]:
            found.append({"key": f"{o['id']}:deadline",
                          "msg": f"{o['id']} {o['kind']}: past its deadline {store.clock(cfg, o['deadline'])}"})
    try:
        from . import inbound, owed, routed, slow, watcher

        found += owed.alerts(cfg, now)
        found += routed.alerts(cfg, now)
        found += slow.alerts(cfg, orders, now)
        found += watcher.checks(cfg, orders, now)
        found += inbound.alerts(cfg, now)
        found += own_work_alerts(cfg, now)
        from . import sweeps

        found += sweeps.alerts(cfg, now)
    except Exception as e:  # routed tracking must not break the tick
        found.append({"key": "routed:failed", "msg": f"routed-work check failed: {type(e).__name__}"})
    if hook_err["count"]:
        found.append({"key": f"hook_errors:{hook_err['last']}",
                      "msg": f"{hook_err['count']} hubkit hook failures in transcripts (last {hook_err['last']})"})
    for mod in extensions(cfg):
        if hasattr(mod, "tick_checks"):
            try:
                found += [{"key": str(a["key"]), "msg": str(a["msg"])} for a in mod.tick_checks(cfg, orders.live())]
            except Exception as e:
                found.append({"key": f"ext:{mod.__name__}", "msg": f"extension failed: {type(e).__name__}"})
    return found


def routed_pass(cfg: dict, now: float) -> list[dict]:
    """Heard-from scan of the hub transcript, then one gh read per PR. Failures never reach the tick."""
    from . import hooks, routed

    try:
        b = hooks.read_bind(cfg)
        path = routed.transcript_of(cfg, b["session_id"]) if hooks.bind_alive(b) else None
        if path:
            routed.scan_transcript(cfg, path)
        routed.agent_activity(cfg, now)
        return routed.refresh_prs(cfg, now)
    except Exception as e:
        store.append_row(config.state_dir(cfg) / "hook-errors.jsonl", {"where": "routed_pass", "error": repr(e)[:200]})
    return []


def tick(cfg: dict, notify: bool = True, now: float | None = None) -> list[dict]:
    """One watchdog pass. Returns the NEW alerts (back-off applied)."""
    now = now or time.time()
    sd = store.state(cfg)
    with store.locked(sd / "status.lock", blocking=False) as got:
        if not got:
            return []
        orders = Orders(cfg)
        orders.repair_archive()
        from . import sweeps

        closed = [*sweeps.tick_pass(cfg, orders, now), *routed_pass(cfg, now)]  # standing rules read the PRs before a review item closes
        apath = sd / "alerts.jsonl"
        prior = store.read_rows(apath)[0]
        new = []
        for a in [*closed, *evaluate(cfg, orders, hook_errors(cfg), now)]:
            if a.get("action"):
                orders.expire(a["action"][1], "unconfirmed order expired (no confirmation)")
            same = [r for r in prior if r.get("key") == a["key"]]
            last = max([r["ts"] for r in same], default=0.0)
            if now - last >= _gap(len(same)):
                store.append_row(apath, {**a, "ts": now})
                new.append(a)
        write_status(cfg)
        store.touch_seen(sd, "tick")
    if notify and new:
        from . import watcher

        watcher.notify(cfg, new)
    return new


def watch(cfg: dict, interval: float, max_cycles: int) -> int:
    """Loop tick; exit 0 on the first alert (the exit wakes the hub), 3 after max_cycles.

    An alert a concurrent cron tick already logged since the watch started also ends the watch, so
    the hub is woken even when the cron run consumed the back-off slot first.
    """
    from . import gate, hooks

    if gate.watch_live(cfg):
        print("watch already running; this one exits without waking anyone")
        return 4
    pidfile = store.state(cfg) / "watch.pid"
    store.write_atomic(pidfile, f"{os.getpid()} {hooks.proc_start(os.getpid())}")
    try:
        return _watch_loop(cfg, interval, max_cycles)
    finally:
        pidfile.unlink(missing_ok=True)


def _watch_loop(cfg: dict, interval: float, max_cycles: int) -> int:
    began = time.time()
    for i in range(max_cycles):
        tick(cfg, notify=True)
        fresh = [r for r in store.read_rows(store.state(cfg) / "alerts.jsonl")[0] if r["ts"] >= began]
        if fresh:
            for r in fresh:
                print("ALERT " + r["msg"])
            return 0
        if i + 1 < max_cycles:
            time.sleep(interval)
    return 3


# ---- EOD ----

def glance(cfg: dict, day: str) -> list[str]:
    """The day-at-a-glance lines of the end-of-day review."""
    from . import owed

    sd = store.state(cfg)
    orders = Orders(cfg)
    arch = store.read_rows(sd / "archive" / f"archive_{day}.jsonl")[0]
    ao = [r for r in arch if r.get("type") == "order"]
    aw = [r for r in arch if r.get("type") == "work"]
    m = orders.metrics(day)
    live = orders.live()
    alerts = [r for r in store.read_rows(sd / "alerts.jsonl")[0] if store.day_of(cfg, r["ts"]) == day]
    rev = learnings.review(learnings.default_file(cfg), day)
    held = [r for r in store.read_rows(orders.path)[0]
            if r.get("op") == "hold" and store.day_of(cfg, r["ts"]) == day]
    return [
        f"Day at a glance {day}",
        f"orders opened {m['orders']}, closed {sum(r['outcome'] == 'closed' for r in ao)}, "
        f"cancelled {sum(r['outcome'] == 'cancelled' for r in ao)}, held {len(held)}, still open {len(live)}",
        f"chases {m['chases']} ({m['chases_per_order']} per order)",
        owed.glance_line(cfg, day),
        f"work items finished {len(aw)}, live now {len(work_load(sd))}",
        f"watchdog alerts {len(alerts)}",
        f"hook errors (scan window) {hook_errors(cfg)['count']}",
        "Learnings vs archive:",
        f"  new today: {', '.join(rev['new']) or '-'}",
        f"  repeated today: {', '.join(rev['repeated']) or '-'}",
        f"  addressed and holding: {', '.join(rev['holding']) or '-'}",
        f"  addressed but repeated (not fixed): {', '.join(rev['regressed']) or '-'}",
        f"  seen 2+ times, not addressed (promote or fix): {', '.join(rev['unaddressed']) or '-'}",
    ]


def digest(cfg: dict, span: str = "12h", now: float | None = None) -> list[str]:
    """What happened since `span` ago, for a user coming back: work finished, orders closed, alerts, what waits on the user."""
    from .lessons import parse_span

    now = now or time.time()
    since = now - parse_span(span)
    sd = store.state(cfg)
    arch: list[dict] = []
    for back in range(0, int((now - since) // 86400) + 2):
        day = store.day_of(cfg, now - back * 86400)
        arch += store.read_rows(sd / "archive" / f"archive_{day}.jsonl")[0]
    arch = [r for r in arch if float(r.get("ts", 0)) >= since]
    work = [r for r in arch if r.get("type") == "work"]
    orders = [r for r in arch if r.get("type") == "order"]
    alerts = [r for r in store.read_rows(sd / "alerts.jsonl")[0] if float(r.get("ts", 0)) >= since]
    live = work_load(sd)
    asks = [i for i in live if i.get("kind") == "ask"]
    out = [f"Digest, last {span}"]
    out += [f"  finished {r['id']}: {r.get('title', '')} ({r.get('done_note') or 'no note'})" for r in work] or ["  nothing finished"]
    out += [f"  order {r['id']} {r.get('outcome')}" for r in orders]
    out += [f"  alert: {r.get('msg', '')}" for r in alerts[-5:]]
    out += [f"  waits on you {i['id']}: {i.get('title', '')}" for i in asks] or ["  nothing waits on you"]
    out.append(f"  still live: {len(live) - len(asks)} job(s)")
    from . import sweeps

    out += sweeps.digest_lines(cfg, now)
    out.append("  everything open by whose court: hubkit mine")
    from . import ledger

    return out + ledger.day_lines(cfg, store.day_of(cfg, since))


def reflection(cfg: dict, day: str) -> list[str]:
    """Self-reflection from facts, no model: orders that needed chasing or stalled, still open."""
    orders = Orders(cfg)
    from . import slow

    lines = ["Self-reflection (from the record):"]
    for o in orders.all().values():
        if store.day_of(cfg, o["opened"]) != day:
            continue
        if o["chases"]:
            lines.append(f"  chased {o['chases']}x: {o['id']} {o['kind']} (first words: \"{o['quote'][:80]}\")")
        if o["state"] in LIVE:
            lines.append(f"  still {o['state']}: {o['id']} {o['kind']}")
    if len(lines) == 1:
        lines.append("  no order needed a chase and none is left open")
    return lines + slow.reflection_lines(cfg, day)


LIVE = ("open", "unconfirmed", "held")  # expired orders are finished


def eod(cfg: dict, day: str, note: str = "") -> Path:
    """Write and return the end-of-day review file for `day`."""
    sd = store.state(cfg)
    Orders(cfg).repair_archive()
    from . import ledger

    body = glance(cfg, day) + [""] + ledger.day_lines(cfg, day) + [""] + reflection(cfg, day)
    if note:
        body += ["", "Self-improvement note:", "  " + learnings.clean(note)]
    out = sd / "eod" / f"eod_{day}.md"
    store.write_atomic(out, "\n".join(redact(x)[0] for x in body) + "\n")
    store.touch_seen(sd, "eod")
    return out
