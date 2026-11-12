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

from . import (
    agentio,
    compat,
    config,
    hooks,
    inbound,
    install,
    ledger,
    lessons,
    playbooks,
    replies,
    roster,
    routed,
    status,
    store,
    trust,
    watcher,
)
from .orders import Orders
from .redact import count_hits_text


def cmd_how(a: argparse.Namespace, cfg: dict) -> int:
    if a.action == "ls":
        print("\n".join(f"{pb['name']}: {', '.join(pb['triggers'])}" for pb in playbooks.load_all()) or "no playbooks")
    elif a.action == "show":
        pb = playbooks.get(a.arg)
        print(f"{pb['name']} (updated {pb['updated']}; triggers: {', '.join(pb['triggers'])})\n{pb['steps']}")
        print("".join(f"quote: {q}\n" for q in pb["quotes"]), end="")
    elif a.action == "match":
        print("\n".join(playbooks.inject(playbooks.match(a.arg))) or "NO PLAYBOOK")
    else:
        if not a.steps_file:
            raise ValueError("save needs --steps-file")
        print(playbooks.save(a.arg, a.trigger, Path(a.steps_file).expanduser().read_text(encoding="utf-8"), a.quote, a.replace))
        playbooks.note_saved(cfg, time.time())
    return 0


def kv(pairs: list[str]) -> dict[str, str]:
    """Parse repeated NAME=VALUE arguments into a dict."""
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        out[k.strip()] = v.strip()
    return out


def cmd_order(a, cfg: dict) -> int:
    """Run an `order` verb; a refusal exits 3, an unknown id 4, bad usage 2 (see agentio)."""
    o = Orders(cfg)
    act = a.action
    if act in ("confirm", "move", "block", "result", "hold", "resume", "cancel", "close") and not a.id:
        raise ValueError(f"order {act} needs an order id")
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
    return 0


def cmd_work(a, cfg: dict) -> int:
    """Run a `work` verb (add, done, ls)."""
    if a.action in ("done", "block", "estimate", "verify") and not a.id:
        raise ValueError(f"work {a.action} needs a work id")
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
        if any(i["id"] == a.id and i.get("kind") == "request" for i in status.work_load(store.state(cfg))):
            raise store.Refused(f'{a.id} is a request: it closes only with evidence. NEXT: hubkit req done {a.id} --evidence "..."')
        from . import threads

        threads.check_done(cfg, a.id, a.note)  # a declared check needs a note saying what it showed
        before = threads.records(cfg).get(a.id, {}).get("thread", "")
        print(status.work_done(cfg, a.id, a.note))
        if before and (settled := threads.settle_line_for(cfg, a.id)):
            print(settled)
    else:
        for i in status.work_load(store.state(cfg)):
            print(f"{i['id']} [{i['kind']}] {i['title']}")
    return 0


def cmd_req(a: argparse.Namespace, cfg: dict) -> int:
    """Run a `req` verb (ls, done, drop, note); a refusal exits 3, an unknown id 4, bad usage 2."""
    from . import owed

    now = time.time()
    if a.action == "ls" and a.learned:
        print("\n".join(owed.learned_lines(cfg, now)))
    elif a.action == "ls":
        print("\n".join(owed.line(i, now) for i in owed.live(cfg)) or "no open requests")
    elif not a.id:
        raise ValueError(f"req {a.action} needs " + {"add": "the user's words", "relearn": "the verb"}.get(a.action, "an id"))
    elif a.action == "add":
        print(owed.add_by_hand(cfg, Orders(cfg), a.id))
    elif a.action == "relearn":
        print(owed.relearn(cfg, a.id))
    elif a.action == "show":
        print("\n".join(owed.show_lines(owed.show(cfg, a.id), now)))
    elif a.action == "done":
        print(owed.done(cfg, a.id, a.evidence, part=a.part))
    elif a.action == "drop":
        print(owed.drop(cfg, a.id, a.quote, part=a.part, not_a_request=a.not_a_request))
    else:
        print(owed.note(cfg, a.id, a.note))
    return 0


def warn_root_closed(a: argparse.Namespace, cfg: dict, rc: int) -> int:
    """After an order or request that is the root of a thread was closed, cancelled or dropped: warn about steps still open under it."""
    if rc == 0 and a.id and (a.cmd, a.action) in (("order", "close"), ("order", "cancel"), ("req", "done"), ("req", "drop")):
        from . import threads

        if warning := threads.open_members_warning(cfg, a.id):
            print(warning)
    return rc


def cmd_thread(a: argparse.Namespace, cfg: dict) -> int:
    """Run a `thread` verb (ls, show, link, unlink); a refusal exits 3, an unknown id 4, bad usage 2."""
    from . import threads

    if a.action == "ls":
        print("\n".join(threads.ls_lines(cfg, time.time())))
        return 0
    if not a.id:
        raise ValueError(f"thread {a.action} needs an id")
    if a.action == "show":
        print("\n".join(threads.show(cfg, a.id, time.time())[0]))
    elif a.action == "link":
        if not a.root:
            raise ValueError("thread link needs the root id")
        print(threads.link(cfg, a.id, a.root))
    else:
        print(threads.unlink(cfg, a.id))
    return 0


def cmd_standing(a: argparse.Namespace, cfg: dict) -> int:
    """Run a `standing` verb (ls, add, drop); a refusal exits 3, an unknown id 4, bad usage 2."""
    from . import standrules

    if a.action == "ls":
        print("\n".join(standrules.ls_lines(cfg, time.time())))
    elif a.action == "add":
        r = standrules.add(cfg, a.on, a.quote, a.do, repo=a.repo, pr=a.pr, person=a.person, word=a.word, until=a.until, cmd=a.next_cmd, kind=a.kind)
        print(f"{r['id']} saved: on {r['on']}, {r['action']}. NEXT: hubkit standing ls")
    elif not a.id:
        raise ValueError("standing drop needs a rule id")
    else:
        print(standrules.drop(cfg, a.id, a.quote))
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


def cmd_gate(a, cfg: dict) -> int:
    """`hubkit gate ls | false-alarm ID --why TEXT | restore ID | set ID hold|warn|off`."""
    from . import gatectl

    if a.action == "ls":
        print("\n".join(gatectl.ls_lines(cfg)))
        return 0
    if not a.id:
        raise ValueError(f"gate {a.action} needs a check id (hubkit gate ls)")
    now = time.time()
    if a.action == "false-alarm":
        print(gatectl.false_alarm(cfg, a.id, a.why, now))
    elif a.action == "restore":
        print(gatectl.restore(cfg, a.id, now))
    else:
        print(gatectl.set_mode(cfg, a.id, a.mode, now))
    return 0


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
    from . import gatectl

    if down := gatectl.downgraded(cfg):
        print(f"WARNING Stop-gate checks downgraded to warn after false alarms: {', '.join(down)}. See hubkit gate ls; hubkit gate restore ID.")
    rows, bad = store.read_rows(sd / "orders.jsonl")
    print(f"orders.jsonl: {len(rows)} rows, {bad} bad lines")
    errs = store.read_rows(sd / "hook-errors.jsonl")[0]
    print(f"hook-errors.jsonl: {len(errs)} rows")
    from . import lifecycle

    print(lifecycle.audit_line(cfg))
    from . import eodclock

    print(eodclock.doctor_line(cfg))
    if not eod_scheduled() and eodclock.parse(cfg) is None:
        print(f"WARNING no {compat.scheduler_name()} entry runs `hubkit eod`: the end-of-day review and its learning pass only happen when someone asks for them.")
    dup = duplicate_hooks(Path(os.environ.get("HUBKIT_SETTINGS") or Path.home() / ".claude" / "settings.json"))
    if dup:
        print(f"WARNING settings.json runs hubkit hook more than once for: {', '.join(dup)}. Each run double-counts prompts and answers; remove the duplicate entries.")
    gaps = intents_gaps(cfg)
    if gaps:
        print(f"WARNING intents.toml lacks what intents.example.toml has: {', '.join(gaps)}. Copy those lines from the example; the checks they configure use built-in defaults or stay off.")
    print(f"transcript scan: {status.hook_errors(cfg)}")
    from . import learnings

    ov = learnings.overlay_path(cfg)
    n_ov = len(ov.read_text(encoding="utf-8", errors="replace").splitlines()) if ov.exists() else 0
    cap_ov = int(cfg["learn"]["overlay_max_lines"])
    print(f"overlay: {n_ov} lines of {cap_ov}" + (" (AT OR OVER THE CAP: merge or drop rules before promoting more)" if n_ov >= cap_ov else ""))
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


def build_parser() -> argparse.ArgumentParser:
    """The whole argument parser (tests parse the help examples with it)."""
    p = agentio.HubParser(prog="hubkit", epilog="examples:\n" + "\n".join(f"  {e}" for e in agentio.EXAMPLES["hubkit"]) + "\n\n" + agentio.EXIT_CODES,
                          formatter_class=argparse.RawDescriptionHelpFormatter)
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

    pe = sub.add_parser("peers", help="the peer roster: role, free/busy, record")
    pe.add_argument("action", choices=["ls", "set", "forget"])
    pe.add_argument("name", nargs="?", default="")
    pe.add_argument("--role")
    pe.add_argument("--note")
    pe.add_argument("--busy", action="store_true")
    pe.add_argument("--free", action="store_true")

    ib = sub.add_parser("inbound")
    ib.add_argument("action", choices=["ingest", "ls", "replied"])
    ib.add_argument("id", nargs="?", help="replied: the work id (W3) or message id of the ask")
    ib.add_argument("--note", default="", help="replied: what was sent")
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

    rq = sub.add_parser("req", help="requests: the user's plain asks, open until done with evidence")
    rq.add_argument("action", choices=["ls", "show", "add", "done", "drop", "note", "relearn"])
    rq.add_argument("id", nargs="?", help="the request id (add: the user's words; relearn: the muted verb)")
    rq.add_argument("--part", type=int, default=None, help="done/drop: close this one part of a multi-part request")
    rq.add_argument("--not-a-request", action="store_true", help="drop: a false intake, no quote needed; intake learns from it")
    rq.add_argument("--learned", action="store_true", help="ls: the verbs intake muted and why")
    rq.add_argument("--evidence", default="", help="done: what shows it is done (required)")
    rq.add_argument("--quote", default="", help="drop: the user's words that cancel it (required)")
    rq.add_argument("--note", default="", help="note: progress, or what blocks it")
    sub.add_parser("mine", help="everything open, one entry per thread, grouped by whose court it is in (read only)")
    th = sub.add_parser("thread", help="threads: the ask, its routed jobs, PRs and replies owed, as one")
    th.add_argument("action", choices=["ls", "show", "link", "unlink"])
    th.add_argument("id", nargs="?", help="show: any id in the thread; link/unlink: the record to move")
    th.add_argument("root", nargs="?", help="link: the root ask (an order or request id)")
    sr = sub.add_parser("standing", help="standing rules: what the user said to do every time something happens")
    sr.add_argument("action", choices=["ls", "add", "drop"])
    sr.add_argument("id", nargs="?", help="drop: the rule id (S1)")
    sr.add_argument("--on", default="", help="add: the trigger, approved (a tracked PR is approved at its head) or inbound (a new ask from a person)")
    sr.add_argument("--quote", default="", help="add: the user's own words that gave the rule (required); drop: the words that cancel it")
    sr.add_argument("--action", dest="do", default="", help="add: what to do when it fires (words, or an order kind such as merge)")
    sr.add_argument("--cmd", dest="next_cmd", default="", help="add: the exact NEXT command; {pr} {sha} {repo} for approved, {person} {where} {msg} for inbound")
    sr.add_argument("--kind", default="", help="add: open an order of this kind (default: the kind the action's words name, else a work item)")
    sr.add_argument("--repo", default="", help="add, approved: only PRs of owner/name")
    sr.add_argument("--pr", default="", help="add, approved: only this PR (URL)")
    sr.add_argument("--from", dest="person", default="", help="add, inbound: only asks from this person")
    sr.add_argument("--word", default="", help="add, inbound: only asks holding this word")
    sr.add_argument("--until", default="", help="add: stop firing after this date (2026-12-01 or 2026-12-01 18:00, the user's zone)")

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
    hw = sub.add_parser("how", help="playbooks: how the user's recurring commands are done")
    hw.add_argument("action", choices=["ls", "show", "save", "match"])
    hw.add_argument("arg", nargs="?", default="", help="show/save: the playbook name; match: the text")
    hw.add_argument("--trigger", action="append", default=[], help="save: a word or phrase, or re:<regex>; repeatable")
    hw.add_argument("--steps-file", default="")
    hw.add_argument("--quote", action="append", default=[], help="save: the user's words that asked for this")
    hw.add_argument("--replace", action="store_true", help="save: overwrite an existing playbook (the old one is kept as .bak-<date>)")
    e = sub.add_parser("eod")
    e.add_argument("--day")
    e.add_argument("--note", default="")

    gt = sub.add_parser("gate", help="Stop-gate checks: modes, false alarms")
    gt.add_argument("action", choices=["ls", "false-alarm", "restore", "set"])
    gt.add_argument("id", nargs="?", default="", help="the check id shown in the hold line")
    gt.add_argument("mode", nargs="?", default="", help="set: hold, warn or off")
    gt.add_argument("--why", default="")

    d = sub.add_parser("doctor")
    d.add_argument("--check-clean", action="store_true")
    sub.add_parser("next", help="the one most urgent action for the hub now, with its exact command")
    agentio.decorate(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    """Parse argv and dispatch one verb. Returns the process exit code."""
    compat.utf8_stdio()
    agentio.HubParser.want_json = "--json" in (sys.argv[1:] if argv is None else argv)
    a = build_parser().parse_args(argv)
    if a.cmd == "hook":
        return hooks.main(a.event)
    cfg = config.load()
    try:
        if getattr(a, "json", False) and a.cmd != "doctor":
            doc = agentio.json_view(a, cfg)
            if doc is not None:
                print(json.dumps(doc, indent=1, default=str))
                return 0
        if a.cmd == "bind":
            print(hooks.bind(cfg, a.pid))
        elif a.cmd == "unbind":
            print(hooks.unbind(cfg))
        elif a.cmd == "order":
            return warn_root_closed(a, cfg, cmd_order(a, cfg))
        elif a.cmd == "work":
            return cmd_work(a, cfg)
        elif a.cmd == "req":
            return warn_root_closed(a, cfg, cmd_req(a, cfg))
        elif a.cmd == "standing":
            return cmd_standing(a, cfg)
        elif a.cmd == "thread":
            return cmd_thread(a, cfg)
        elif a.cmd == "mine":
            from . import owed

            print("\n".join(owed.mine(cfg)))
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
        elif a.cmd == "peers":
            if a.action == "ls":
                print("\n".join(roster.ls_lines(cfg)))
            elif not a.name:
                raise ValueError(f"peers {a.action} needs a NAME")
            elif a.action == "forget":
                print(roster.forget(cfg, a.name))
            elif a.busy and a.free:
                raise ValueError("--busy and --free are exclusive")
            else:
                print(roster.set_by_hand(cfg, a.name, a.role, a.note, True if a.free else False if a.busy else None, time.time()))
        elif a.cmd == "inbound":
            if a.action == "ls":
                print("\n".join(replies.ls_lines(cfg, time.time())))
            elif a.action == "replied":
                if not a.id or not a.note.strip():
                    raise ValueError("inbound replied needs an id and --note \"<what was sent>\"")
                print(replies.mark_replied(cfg, a.id, a.note))
            else:
                print("\n".join(inbound.ingest(cfg, Path(a.file).read_text(encoding="utf-8") if a.file else sys.stdin.read())) or "nothing new")
        elif a.cmd == "how":
            return cmd_how(a, cfg)
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
        elif a.cmd == "gate":
            return cmd_gate(a, cfg)
        elif a.cmd == "doctor":
            if a.json:
                import contextlib
                import io

                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cmd_doctor(a, cfg)
                print(json.dumps({"ok": rc == 0, "exit": rc, "lines": buf.getvalue().splitlines()}, indent=1))
                return rc
            return cmd_doctor(a, cfg)
        elif a.cmd == "next":
            print(agentio.render_next(agentio.pick(cfg, time.time())))
    except (RuntimeError, ValueError, KeyError, store.StateError) as err:
        return agentio.fail(a, err)
    return 0
