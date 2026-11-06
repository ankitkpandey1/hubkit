"""hubkit command line."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import tomllib
from pathlib import Path

from . import compat, config, hooks, inbound, install, ledger, lessons, routed, status, store, trust, watcher
from .orders import Orders
from .redact import count_hits_text


def kv(pairs: list[str]) -> dict[str, str]:
    """Parse repeated NAME=VALUE arguments into a dict."""
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        out[k.strip()] = v.strip()
    return out


def cmd_order(a, cfg: dict) -> int:
    """Run an `order` verb; exit 2 with a message on an unknown id or refused action."""
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
            msg = o.move(a.id, a.note, a.estimate)
            if msg:
                print(msg)
        elif act == "block":
            print(o.block(a.id, a.on, a.fix, a.holder))
        elif act == "result":
            o.result(a.id, a.note)
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
    """Run a `work` verb (add, done, ls)."""
    try:
        if a.action == "add" and a.kind == "routed":
            if not (a.owner and a.title):
                raise ValueError("routed items need --owner and --title")
            prs = routed.refs(cfg, " ".join(a.pr))
            heads = {u: a.head for u in prs} if a.head else {}
            print(routed.record(cfg, a.owner, a.title, prs, heads, review=routed.is_review(cfg, a.title + " " + " ".join(a.pr)), estimate=a.estimate)["id"])
        elif a.action == "add":
            print(status.work_add(cfg, a.kind, a.title, a.owner, a.note, a.estimate)["id"])
        elif a.action == "block":
            print(watcher.set_blocked(cfg, a.id, a.on))
        elif a.action == "estimate":
            from . import slow

            print(slow.re_estimate(cfg, a.id, a.minutes or 0, a.why))
        elif a.action == "verify":
            print(status.work_verify(cfg, a.id))
        elif a.action == "done":
            print(status.work_done(cfg, a.id, a.note))
        else:
            for i in status.work_load(store.state(cfg)):
                print(f"{i['id']} [{i['kind']}] {i['title']}")
    except (KeyError, ValueError) as e:
        print(f"error: {e.args[0]}", file=sys.stderr)
        return 2
    return 0


def cron_configured() -> bool:
    """True when the user's scheduler (crontab, or Task Scheduler on Windows) holds an active entry that runs hubkit."""
    return any("hubkit" in x for x in compat.scheduler_lines())


def cron_line(cfg: dict, sd: Path) -> str:
    """The `cron:` doctor line. When cron is configured, a `seen/cron` older than `watchdog.cron_stale_min` (or
    never written) is flagged: every tick run touches it first, so a stale one means cron is not running hubkit."""
    f = sd / "seen" / "cron"
    if not cron_configured():
        return f"cron: no hubkit line in the {compat.scheduler_name()}"
    age = (time.time() - f.stat().st_mtime) / 60 if f.exists() else None
    if age is None or age > float(cfg["watchdog"]["cron_stale_min"]):
        shown = "never ran" if age is None else f"last ran {int(age)} min ago"
        return f"cron: STALE: a hubkit {compat.scheduler_name()} entry exists but {shown} (limit {cfg['watchdog']['cron_stale_min']} min); check cron's mail and that the line runs"
    return "cron: ok"


def intents_gaps(cfg: dict) -> list[str]:
    """Sections and keys the shipped intents.example.toml has and the user's own intents.toml lacks (a new release adds checks the old file never turns on)."""
    mine = config.intents_path(cfg)
    example = config.SKILL_DIR / "intents.example.toml"
    if mine == example or not mine.exists() or not example.exists():
        return []
    have, want = tomllib.loads(mine.read_text(encoding="utf-8")), tomllib.loads(example.read_text(encoding="utf-8"))
    gaps = [f"[{sec}]" for sec in want if sec not in have]
    gaps += [f"[{sec}] {key}" for sec in want if sec in have and sec != "kinds" for key in want[sec] if key not in have[sec]]
    return gaps


def duplicate_hooks(settings: Path) -> list[str]:
    """Events for which the user's settings.json runs `hubkit hook <event>` more than once (each run double-counts). Read only."""
    if not settings.exists():
        return []
    try:
        hooks_cfg = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
    except ValueError, AttributeError:
        return []
    seen: dict[str, int] = {}
    for blocks in hooks_cfg.values():
        for block in blocks if isinstance(blocks, list) else []:
            for h in block.get("hooks", []):
                m = re.search(r"hubkit\S*\s+hook\s+(\w+)", str(h.get("command", "")))
                if m:
                    seen[m.group(1)] = seen.get(m.group(1), 0) + 1
    return sorted(ev for ev, n in seen.items() if n > 1)


def eod_scheduled() -> bool:
    """True when a scheduler entry runs `hubkit ... eod`."""
    return any("hubkit" in x and re.search(r"\beod\b", x) for x in compat.scheduler_lines())


def cmd_doctor(a, cfg: dict) -> int:
    """Print config, bind, hook heartbeat and state health; exit 1 on secret-shaped strings or private terms."""
    sd = store.state(cfg)
    problems = 0
    print(f"config: {cfg['_config_path']} ({'found' if Path(cfg['_config_path']).exists() else 'defaults'})")
    print(f"state: {sd}")
    b = hooks.read_bind(cfg)
    print(f"bind: {'live ' + str(b.get('name')) if hooks.bind_alive(b) else 'none or dead'}")
    for name in ("session", "prompt", "answer", "routed", "stop", "tick", "cron"):
        f = sd / "seen" / name
        print(f"seen/{name}: {int((time.time() - f.stat().st_mtime) / 60)} min ago" if f.exists() else f"seen/{name}: never")
    print(cron_line(cfg, sd))
    rows, bad = store.read_rows(sd / "orders.jsonl")
    print(f"orders.jsonl: {len(rows)} rows, {bad} bad lines")
    errs = store.read_rows(sd / "hook-errors.jsonl")[0]
    print(f"hook-errors.jsonl: {len(errs)} rows")
    if not eod_scheduled():
        print(f"WARNING no {compat.scheduler_name()} entry runs `hubkit eod`: the end-of-day review and its learning pass only happen when someone asks for them.")
    dup = duplicate_hooks(Path(os.environ.get("HUBKIT_SETTINGS") or Path.home() / ".claude" / "settings.json"))
    if dup:
        print(f"WARNING settings.json runs hubkit hook more than once for: {', '.join(dup)}. Each run double-counts prompts and answers; remove the duplicate entries.")
    gaps = intents_gaps(cfg)
    if gaps:
        print(f"WARNING intents.toml lacks what intents.example.toml has: {', '.join(gaps)}. Copy those lines from the example; the checks they configure use built-in defaults or stay off.")
    print(f"transcript scan: {status.hook_errors(cfg)}")
    hits = sum(count_hits_text(p.read_text(encoding="utf-8", errors="replace")) for p in sd.rglob("*.jsonl"))
    print(f"secret-shaped strings in state (count only): {hits}")
    problems += hits
    if a.check_clean:
        terms = [t.lower() for t in cfg["private"]["terms"]]
        dirty = []
        for p in config.SKILL_DIR.rglob("*"):
            if p.is_file() and not {"__pycache__", ".pytest_cache", ".git", ".ruff_cache", ".mypy_cache"} & set(p.relative_to(config.SKILL_DIR).parts) \
                    and p.suffix in (".py", ".md", ".toml", ".txt", ".yml", ".yaml", ".gitignore", ""):
                low = p.read_text(encoding="utf-8", errors="replace").lower()
                dirty += [str(p.relative_to(config.SKILL_DIR)) for t in terms if t and t in low]
        print(f"private terms in skill files (names only, no matches shown): {sorted(set(dirty)) or 'none'} [{len(terms)} terms]")
        problems += len(dirty)
    return 1 if problems else 0


def run() -> None:
    """Console-script entry point (`uv tool install hubkit-cli`)."""
    sys.exit(main())


def main(argv: list[str] | None = None) -> int:
    """Parse argv and dispatch one verb. Returns the process exit code."""
    compat.utf8_stdio()
    p = argparse.ArgumentParser(prog="hubkit")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("hook")
    h.add_argument("event", choices=sorted(hooks.HOOKS))

    b = sub.add_parser("bind")
    b.add_argument("--pid", type=int)
    sub.add_parser("unbind")

    sk = sub.add_parser("skills")
    sk.add_argument("--install", metavar="DIR", required=True, help="the skills directory of your agent tool, for example ~/.claude/skills")

    hk = sub.add_parser("hooks")
    hk.add_argument("--skills-dir", default="$HOME/.claude/skills")
    hk.add_argument("--merge-guard", action="store_true", help="include the warn-only PreToolUse merge check")

    tr = sub.add_parser("trust")
    tr.add_argument("action", choices=["record", "ls"])
    tr.add_argument("--class", dest="cls", default="")
    tr.add_argument("--outcome", default="")

    ib = sub.add_parser("inbound")
    ib.add_argument("action", choices=["ingest"])
    ib.add_argument("--file", help="the inbox watcher's output (default: stdin)")

    k = sub.add_parser("asks")
    k.add_argument("action", choices=["ls"])
    k.add_argument("--since", default="12h", help="how far back, for example 30m, 12h, 2d")

    o = sub.add_parser("order")
    o.add_argument("action", choices=["ls", "status", "open", "confirm", "move", "block", "result", "hold", "resume", "cancel", "close", "metrics"])
    o.add_argument("id", nargs="?")
    o.add_argument("--kind")
    o.add_argument("--quote", default="")
    o.add_argument("--note", default="")
    o.add_argument("--on", default="", help="block: what the order is blocked on")
    o.add_argument("--fix", default="", help="block: the action running now to clear it")
    o.add_argument("--holder", default="", help="block: who runs the fix")
    o.add_argument("--evidence", action="append", default=[], help="name=value (value 'file:<path>' is checked)")
    o.add_argument("--day")
    o.add_argument("--unconfirmed", action="store_true")
    o.add_argument("--estimate", type=int, help="move: minutes this order should take (default: the usual time)")

    w = sub.add_parser("work")
    w.add_argument("action", choices=["add", "done", "ls", "estimate", "block", "verify"])
    w.add_argument("id", nargs="?")
    w.add_argument("--kind", default="active")
    w.add_argument("--title", default="")
    w.add_argument("--owner", default="")
    w.add_argument("--note", default="")
    w.add_argument("--pr", action="append", default=[], help="routed: PR URL (repeatable)")
    w.add_argument("--estimate", type=int, help="minutes this job should take")
    w.add_argument("--minutes", type=int, help="estimate: the new total minutes")
    w.add_argument("--on", default="", help="block: the job this one waits on (empty clears)")
    w.add_argument("--why", default="", help="estimate: the reason for the new estimate")
    w.add_argument("--head", default="", help="routed: head sha the work was routed at")

    t = sub.add_parser("tick")
    t.add_argument("--no-notify", action="store_true")
    t.add_argument("--if-bound", action="store_true", help="no-op unless a hub is bound or orders are open (for cron)")
    wt = sub.add_parser("watch")
    wt.add_argument("--interval", type=float, default=300)
    wt.add_argument("--max-cycles", type=int, default=12)

    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true")
    fd = sub.add_parser("find")
    fd.add_argument("word")
    fd.add_argument("--since", default="2d", help="how far back for asks, peer prompts and PRs, for example 12h, 2d")
    dg = sub.add_parser("digest")
    dg.add_argument("--since", default="12h", help="how far back, for example 30m, 12h, 2d")
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
            if a.if_bound and not (hooks.bind_alive(hooks.read_bind(cfg)) or watcher.live_jobs(cfg, Orders(cfg))):
                return 0
            for x in status.tick(cfg, notify=not a.no_notify):
                print("ALERT " + x["msg"])
        elif a.cmd == "skills":
            print("\n".join(install.install_skills(Path(a.install).expanduser())))
        elif a.cmd == "hooks":
            print(install.hooks_block(a.skills_dir, a.merge_guard))
        elif a.cmd == "trust":
            print(trust.record(cfg, a.cls, a.outcome) if a.action == "record" else "\n".join(f"{c}: {m}" for c, m in sorted(trust.mode(cfg).items())) or "no outcomes recorded")
        elif a.cmd == "inbound":
            print("\n".join(inbound.ingest(cfg, Path(a.file).read_text(encoding="utf-8") if a.file else sys.stdin.read())) or "nothing new")
        elif a.cmd == "asks":
            print("\n".join(lessons.asks_lines(cfg, a.since)))
        elif a.cmd == "watch":
            return status.watch(cfg, a.interval, a.max_cycles)
        elif a.cmd == "status":
            st_ = status.write_status(cfg)
            print(json.dumps(st_, indent=1) if a.json else status.render_block(cfg, Orders(cfg), st_))
        elif a.cmd == "find":
            print("\n".join(ledger.find(cfg, a.word, a.since)))
        elif a.cmd == "digest":
            print("\n".join(status.digest(cfg, a.since)))
        elif a.cmd == "eod":
            day = a.day or store.day_of(cfg, time.time())
            out = status.eod(cfg, day, a.note)
            print(out.read_text(encoding="utf-8"), end="")
        elif a.cmd == "doctor":
            return cmd_doctor(a, cfg)
    except (RuntimeError, ValueError, store.StateError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    return 0
