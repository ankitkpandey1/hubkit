"""hubkit command line."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from . import config, hooks, learnings, routed, status, store
from .orders import Orders
from .redact import count_hits_text


def kv(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        out[k.strip()] = v.strip()
    return out


def cmd_order(a, cfg: dict) -> int:
    o = Orders(cfg)
    act = a.action
    try:
        if act == "ls":
            for x in o.live():
                print(o.status_line(x))
        elif act == "status":
            items = [o.get(a.id)] if a.id else o.live()
            for x in items:
                print(o.status_line(x))
        elif act == "open":
            x = o.open(a.kind, a.quote, state="unconfirmed" if a.unconfirmed else "open")
            print(o.status_line(x))
        elif act == "confirm":
            o.confirm(a.id)
        elif act == "move":
            o.move(a.id, a.note)
        elif act == "hold":
            o.hold(a.id, a.quote)
        elif act == "resume":
            o.resume(a.id)
        elif act == "cancel":
            o.cancel(a.id, a.quote)
        elif act == "close":
            print(o.close(a.id, kv(a.evidence)))
        elif act == "metrics":
            print(json.dumps(o.metrics(a.day or store.day_of(cfg, time.time()))))
    except (KeyError, ValueError) as e:
        print(f"error: {e.args[0] if e.args else e}", file=sys.stderr)
        return 2
    return 0


def cmd_work(a, cfg: dict) -> int:
    try:
        if a.action == "add" and a.kind == "routed":
            if not (a.owner and a.title):
                raise ValueError("routed items need --owner and --title")
            prs = routed.refs(cfg, " ".join(a.pr))
            heads = {u: a.head for u in prs} if a.head else {}
            print(routed.record(cfg, a.owner, a.title, prs, heads, review=routed.is_review(cfg, a.title + " " + " ".join(a.pr)))["id"])
        elif a.action == "add":
            print(status.work_add(cfg, a.kind, a.title, a.owner, a.note)["id"])
        elif a.action == "done":
            print(status.work_done(cfg, a.id, a.note))
        else:
            for i in status.work_load(store.state(cfg)):
                print(f"{i['id']} [{i['kind']}] {i['title']}")
    except (KeyError, ValueError) as e:
        print(f"error: {e.args[0]}", file=sys.stderr)
        return 2
    return 0


def cmd_doctor(a, cfg: dict) -> int:
    sd = store.state(cfg)
    problems = 0
    print(f"config: {cfg['_config_path']} ({'found' if Path(cfg['_config_path']).exists() else 'defaults'})")
    print(f"state: {sd}")
    b = hooks.read_bind(cfg)
    print(f"bind: {'live ' + str(b.get('name')) if hooks.bind_alive(b) else 'none or dead'}")
    for name in ("session", "prompt", "answer", "routed", "stop", "tick", "cron"):
        f = sd / "seen" / name
        print(f"seen/{name}: {int((time.time() - f.stat().st_mtime) / 60)} min ago" if f.exists() else f"seen/{name}: never")
    rows, bad = store.read_rows(sd / "orders.jsonl")
    print(f"orders.jsonl: {len(rows)} rows, {bad} bad lines")
    errs = store.read_rows(sd / "hook-errors.jsonl")[0]
    print(f"hook-errors.jsonl: {len(errs)} rows")
    print(f"transcript scan: {status.hook_errors(cfg)}")
    hits = sum(count_hits_text(p.read_text(errors="replace")) for p in sd.rglob("*.jsonl"))
    print(f"secret-shaped strings in state (count only): {hits}")
    problems += hits
    if a.check_clean:
        terms = [t.lower() for t in cfg["private"]["terms"]]
        dirty = []
        for p in config.SKILL_DIR.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts \
                    and p.suffix in (".py", ".md", ".toml", ".txt", ".gitignore", ""):
                low = p.read_text(errors="replace").lower()
                dirty += [str(p.relative_to(config.SKILL_DIR)) for t in terms if t and t in low]
        print(f"private terms in skill files (names only, no matches shown): {sorted(set(dirty)) or 'none'} [{len(terms)} terms]")
        problems += len(dirty)
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hubkit")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("hook")
    h.add_argument("event", choices=sorted(hooks.HOOKS))

    b = sub.add_parser("bind")
    b.add_argument("--pid", type=int)
    sub.add_parser("unbind")

    o = sub.add_parser("order")
    o.add_argument("action", choices=["ls", "status", "open", "confirm", "move", "hold", "resume", "cancel", "close", "metrics"])
    o.add_argument("id", nargs="?")
    o.add_argument("--kind")
    o.add_argument("--quote", default="")
    o.add_argument("--note", default="")
    o.add_argument("--evidence", action="append", default=[], help="name=value (value 'file:<path>' is checked)")
    o.add_argument("--day")
    o.add_argument("--unconfirmed", action="store_true")

    w = sub.add_parser("work")
    w.add_argument("action", choices=["add", "done", "ls"])
    w.add_argument("id", nargs="?")
    w.add_argument("--kind", default="active")
    w.add_argument("--title", default="")
    w.add_argument("--owner", default="")
    w.add_argument("--note", default="")
    w.add_argument("--pr", action="append", default=[], help="routed: PR URL (repeatable)")
    w.add_argument("--head", default="", help="routed: head sha the work was routed at")

    t = sub.add_parser("tick")
    t.add_argument("--no-notify", action="store_true")
    t.add_argument("--if-bound", action="store_true", help="no-op unless a hub is bound or orders are open (for cron)")
    wt = sub.add_parser("watch")
    wt.add_argument("--interval", type=float, default=300)
    wt.add_argument("--max-cycles", type=int, default=12)

    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true")
    e = sub.add_parser("eod")
    e.add_argument("--day")
    e.add_argument("--note", default="")

    d = sub.add_parser("doctor")
    d.add_argument("--check-clean", action="store_true")

    a = p.parse_args(argv)
    if a.cmd == "hook":
        return hooks.main(a.event)
    cfg = config.load()
    try:
        if a.cmd == "bind":
            print(hooks.bind(cfg, a.pid))
        elif a.cmd == "unbind":
            print(hooks.unbind(cfg))
        elif a.cmd == "order":
            return cmd_order(a, cfg)
        elif a.cmd == "work":
            return cmd_work(a, cfg)
        elif a.cmd == "tick":
            store.touch_seen(store.state(cfg), "cron")
            if a.if_bound and not (hooks.bind_alive(hooks.read_bind(cfg)) or Orders(cfg).live()):
                return 0
            for x in status.tick(cfg, notify=not a.no_notify):
                print("ALERT " + x["msg"])
        elif a.cmd == "watch":
            return status.watch(cfg, a.interval, a.max_cycles)
        elif a.cmd == "status":
            st_ = status.write_status(cfg)
            print(json.dumps(st_, indent=1) if a.json else status.render_block(cfg, Orders(cfg), st_))
        elif a.cmd == "eod":
            day = a.day or store.day_of(cfg, time.time())
            out = status.eod(cfg, day, a.note)
            print(out.read_text(), end="")
        elif a.cmd == "doctor":
            return cmd_doctor(a, cfg)
    except (RuntimeError, store.StateError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    return 0
