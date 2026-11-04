"""Live work file, derived status.json, tick (watchdog), mep adapter, hook-error scan, EOD."""

from __future__ import annotations

import glob
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from . import config, learnings, store
from .redact import redact
from .orders import Orders

VERSION = 1
WORK_KINDS = ("active", "tracked", "priority")
STALE_MIN = 15
MAX_BLOCK_LINES = 25


# ---- live work file: only live items; finished items move to archive_<day> ----

def work_path(sd: Path) -> Path:
    return sd / "work.json"


def work_load(sd: Path) -> list[dict]:
    p = work_path(sd)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def work_save(sd: Path, items: list[dict]) -> None:
    store.write_json_atomic(work_path(sd), items)


def work_add(cfg: dict, kind: str, title: str, owner: str = "", note: str = "") -> dict:
    from .redact import redact_obj

    if kind not in WORK_KINDS:
        raise ValueError(f"kind must be one of {WORK_KINDS}")
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        items = work_load(sd)
        nid = "W" + str(max([int(i["id"][1:]) for i in items] + [0]) + 1)
        item = redact_obj({"id": nid, "kind": kind, "title": title, "owner": owner,
                           "note": note, "added": time.time()})
        items.append(item)
        work_save(sd, items)
    return item


def work_done(cfg: dict, wid: str, note: str = "") -> str:
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        items = work_load(sd)
        hit = next((i for i in items if i["id"] == wid), None)
        if hit is None:
            raise KeyError(f"no live item {wid}")
        now = time.time()
        day = store.day_of(cfg, now)
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
        r = subprocess.run([exe, "status", "--json"], capture_output=True, text=True,
                           timeout=cfg["mep"]["timeout_s"], env=env)
        titles = [i.get("title", "") for i in json.loads(r.stdout)["on_you"]]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as e:
        if prev and prev.get("state") in ("ok", "stale"):
            return {**prev, "state": "stale", "why": type(e).__name__,
                    "good_at": prev.get("good_at", prev.get("at", time.time()))}
        return {"state": "UNVERIFIED", "why": type(e).__name__}
    return {"state": "ok", "count": len(titles), "titles": titles[:5], "good_at": time.time()}


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
                        if att.get("type") in kinds and marker in cmd:
                            count += 1
                            last = json.loads(line).get("timestamp", last)
        except OSError:
            continue
    return {"count": count, "last": last}


def extensions(cfg: dict) -> list:
    d = config.path_of(cfg, "extensions_dir", Path(cfg["_config_dir"]) / "ext")
    mods = []
    for f in sorted(d.glob("*.py")) if d.is_dir() else []:
        spec = importlib.util.spec_from_file_location(f"hubkit_ext_{f.stem}", f)
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
    today = store.day_of(cfg, time.time())
    now = time.time()
    out = []
    for b in learnings.recent(lpath, 40):
        last = learnings.last_seen(b)
        if not last or (now - time.mktime(time.strptime(last, "%Y-%m-%d"))) > 30 * 86400:
            continue
        addr = learnings.addressed(b)
        tag = ""
        if addr:
            if (now - time.mktime(time.strptime(addr[0], "%Y-%m-%d"))) > 14 * 86400:
                continue
            tag = " (probation: addressed " + addr[0] + ")"
        out.append({"cls": learnings.block_class(b) + tag, "right": learnings.field(b, "right")})
    return out[-5:]



def build_status(cfg: dict) -> dict:
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
        "mep": mep_state(cfg, (read_status(cfg) or {}).get("mep")), "learnings": recent, "hook_errors": hook_errors(cfg),
        "extra": extra,
    }


def write_status(cfg: dict) -> dict:
    sd = store.state(cfg)
    st = build_status(cfg)
    store.write_json_atomic(sd / "status.json", st)
    return st


def read_status(cfg: dict) -> dict | None:
    p = store.state(cfg, create=False) / "status.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def render_block(cfg: dict, orders: Orders, st: dict | None, now: float | None = None) -> str:
    now = now or time.time()
    out = [f"hubkit: you are the {cfg['hub']['name']} session for {cfg['user']['name']}. "
           "Re-read the hub SKILL.md and the overlay before acting."]
    if st is None:
        out.append("status: UNVERIFIED (no status.json yet; run `hubkit tick`)")
    else:
        age = int((now - st["built"]) / 60)
        out.append(f"status built {age} min ago" + (f": UNVERIFIED, stale over {STALE_MIN} min" if age > STALE_MIN else ""))
        for kind in WORK_KINDS:
            mine = [i for i in st["work"] if i["kind"] == kind]
            if mine:
                out.append(f"{kind}: " + "; ".join(f"{i['id']} {i['title']}" for i in mine))
        m = st["mep"]
        if m["state"] in ("ok", "stale"):
            out.append(f"on the user (mep): {m['count']}; first: " + " | ".join(m["titles"][:3])
                       + (f" (STALE: last good {int((now - m['good_at']) / 60)} min ago, now {m['why']})" if m["state"] == "stale" else ""))
        elif m["state"] != "off":
            out.append(f"mep: {m['state']} ({m.get('why', '')})")
        he = st["hook_errors"]
        out.append(f"hook errors in scan window: {he['count']}" + (f" (last {he['last']})" if he["last"] else ""))
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
    return [r for r in store.read_rows(orders.path)[0] if r.get("id") == oid and r.get("op") == "hold"]


def evaluate(cfg: dict, orders: Orders, hook_err: dict, now: float) -> list[dict]:
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
        if not o["moves"] and age > spec.get("first_action_min", 5):
            found.append({"key": f"{o['id']}:no_first_action",
                          "msg": f"{o['id']} {o['kind']}: no first action after {int(age)} min"})
        idle = (now - orders.last_movement(o)) / 60
        if idle > spec.get("stall_min", 10):
            found.append({"key": f"{o['id']}:stalled",
                          "msg": f"{o['id']} {o['kind']}: no movement for {int(idle)} min"})
        if o.get("deadline") and now > o["deadline"]:
            found.append({"key": f"{o['id']}:deadline",
                          "msg": f"{o['id']} {o['kind']}: past its deadline {store.clock(cfg, o['deadline'])}"})
    if hook_err["count"]:
        found.append({"key": f"hook_errors:{hook_err['count']}",
                      "msg": f"{hook_err['count']} hubkit hook failures in transcripts (last {hook_err['last']})"})
    for mod in extensions(cfg):
        if hasattr(mod, "tick_checks"):
            try:
                found += [{"key": str(a["key"]), "msg": str(a["msg"])} for a in mod.tick_checks(cfg, orders.live())]
            except Exception as e:
                found.append({"key": f"ext:{mod.__name__}", "msg": f"extension failed: {type(e).__name__}"})
    return found


def tick(cfg: dict, notify: bool = True, now: float | None = None) -> list[dict]:
    """One watchdog pass. Returns the NEW alerts (back-off applied)."""
    now = now or time.time()
    sd = store.state(cfg)
    with store.locked(sd / "status.lock", blocking=False) as got:
        if not got:
            return []
        orders = Orders(cfg)
        orders.repair_archive()
        apath = sd / "alerts.jsonl"
        prior = store.read_rows(apath)[0]
        new = []
        for a in evaluate(cfg, orders, hook_errors(cfg), now):
            if a.get("action"):
                orders.expire(a["action"][1], "unconfirmed order expired (no confirmation)")
            same = [r for r in prior if r.get("key") == a["key"]]
            last = max([r["ts"] for r in same], default=0.0)
            if now - last >= _gap(len(same)):
                store.append_row(apath, {**a, "ts": now})
                new.append(a)
        write_status(cfg)
        store.touch_seen(sd, "tick")
    cmd = cfg["tools"]["notify_cmd"]
    if notify and cmd and new:
        try:
            subprocess.run([*shlex.split(cmd), "hubkit: " + "; ".join(a["msg"] for a in new)],
                           timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    return new


def watch(cfg: dict, interval: float, max_cycles: int) -> int:
    """Loop tick; exit 0 on the first new alert (the exit wakes the hub), 3 after max_cycles."""
    for i in range(max_cycles):
        if tick(cfg, notify=True):
            return 0
        if i + 1 < max_cycles:
            time.sleep(interval)
    return 3


# ---- EOD ----

def glance(cfg: dict, day: str) -> list[str]:
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


def reflection(cfg: dict, day: str) -> list[str]:
    """Self-reflection from facts, no model: orders that needed chasing or stalled, still open."""
    orders = Orders(cfg)
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
    return lines


LIVE = ("open", "unconfirmed", "held")  # expired orders are finished


def eod(cfg: dict, day: str, note: str = "") -> Path:
    sd = store.state(cfg)
    Orders(cfg).repair_archive()
    body = glance(cfg, day) + [""] + reflection(cfg, day)
    if note:
        body += ["", "Self-improvement note:", "  " + learnings.clean(note)]
    out = sd / "eod" / f"eod_{day}.md"
    store.write_atomic(out, "\n".join(redact(x)[0] for x in body) + "\n")
    store.touch_seen(sd, "eod")
    return out
