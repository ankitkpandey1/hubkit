"""What an AI agent that drives hubkit needs: `--json` views, stable exit codes, a NEXT line on every error, `hubkit next`, help examples.

Exit codes: 0 ok, 2 bad usage, 3 refused (valid request, hubkit will not do it), 4 not found (unknown id or name).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time

from . import store

OK, USAGE, REFUSED, NOT_FOUND = 0, 2, 3, 4

EXIT_CODES = """exit codes:
  0  ok
  2  bad usage: a missing or invalid argument
  3  refused: the request is valid but hubkit will not do it (missing evidence, the state forbids it)
  4  not found: an unknown id or name
Every error ends with a line "NEXT: <exact command>". Read verbs take --json: one JSON document on stdout.
With --json an error prints {"error", "next", "code"} on stdout as well as the stderr lines."""

EXAMPLES = {
    "hubkit": ["hubkit next", "hubkit mine", "hubkit order ls --json"],
    "hook": ["hubkit hook stop"],
    "bind": ["hubkit bind", "hubkit unbind"],
    "unbind": ["hubkit unbind"],
    "skills": ["hubkit skills --install ~/.claude/skills"],
    "hooks": ["hubkit hooks", "hubkit hooks --merge-guard"],
    "trust": ["hubkit trust ls", "hubkit trust record --class merge --outcome ok"],
    "peers": ["hubkit peers ls --json", "hubkit peers set peer-a --role reviewer --free", "hubkit peers forget peer-a"],
    "inbound": ["hubkit inbound ls --json", "hubkit inbound replied W3 --note \"told them it is merged\"", "hubkit inbound ingest --file inbox.txt"],
    "asks": ["hubkit asks ls --since 12h"],
    "order": ["hubkit order ls --json", "hubkit order open --kind merge --quote \"merge it now\"",
              "hubkit order close O1 --evidence pr_merged=https://github.com/acme/repo/pull/1"],
    "req": ["hubkit req ls --json", "hubkit req note R1 --note \"waiting on CI\"", "hubkit req done R1 --evidence \"merged: <sha>\""],
    "mine": ["hubkit mine", "hubkit mine --json"],
    "thread": ["hubkit thread ls --json", "hubkit thread show R1", "hubkit thread link W3 R1", "hubkit thread unlink W3"],
    "standing": ["hubkit standing ls --json", "hubkit standing add --on approved --quote \"merge when approved\" --action merge --repo acme/repo",
                 "hubkit standing drop S1 --quote \"stop doing that\""],
    "work": ["hubkit work ls --json", "hubkit work add --kind active --title \"fix the build\"", "hubkit work done W1 --note \"fixed in abc1234\""],
    "tick": ["hubkit tick --no-notify", "hubkit tick --if-bound"],
    "watch": ["hubkit watch --interval 300 --max-cycles 12"],
    "status": ["hubkit status", "hubkit status --json"],
    "find": ["hubkit find login --since 2d", "hubkit find acme/repo --json"],
    "digest": ["hubkit digest --since 12h", "hubkit digest --json"],
    "how": ["hubkit how ls --json", "hubkit how match \"ship the release\"", "hubkit how show release"],
    "eod": ["hubkit eod", "hubkit eod --note \"all closed\""],
    "gate": ["hubkit gate ls --json", "hubkit gate false-alarm utc-time --why \"the time was right\"", "hubkit gate set utc-time warn"],
    "doctor": ["hubkit doctor", "hubkit doctor --json", "hubkit doctor --check-clean"],
    "next": ["hubkit next", "hubkit next --json"],
}

JSON_VERBS = ("order", "work", "req", "mine", "thread", "peers", "inbound", "find", "digest", "gate", "standing", "how", "doctor", "next", "status")

LS_CMD = {"thread": "hubkit thread ls", "order": "hubkit order ls", "work": "hubkit work ls", "req": "hubkit req ls", "standing": "hubkit standing ls",
          "inbound": "hubkit inbound ls", "peers": "hubkit peers ls", "how": "hubkit how ls", "gate": "hubkit gate ls"}

USAGE_NEXT = {
    ("order", "open"): 'hubkit order open --kind <kind> --quote "<the user\'s words>"',
    ("order", "close"): "hubkit order close {id} --evidence name=value",
    ("order", "cancel"): 'hubkit order cancel {id} --quote "<the user\'s words>"',
    ("order", "hold"): 'hubkit order hold {id} --quote "<the user\'s words>"',
    ("order", "block"): 'hubkit order block {id} --on "<what blocks it>" --fix "<action running now>" --holder <who>',
    ("order", "result"): 'hubkit order result {id} --note "<what was done and what came of it>"',
    ("req", "done"): 'hubkit req done {id} --evidence "<a sha, a URL, a command and its result>"',
    ("req", "drop"): 'hubkit req drop {id} --quote "<the user\'s words>"',
    ("req", "add"): 'hubkit req add "<the user\'s words>"',
    ("req", "relearn"): "hubkit req ls --learned",
    ("req", "show"): "hubkit req ls",
    ("req", "note"): 'hubkit req note {id} --note "<progress, or what blocks it>"',
    ("thread", "link"): "hubkit thread link {id} <root id>",
    ("thread", "unlink"): "hubkit thread unlink {id}",
    ("thread", "show"): "hubkit thread show {id}",
    ("standing", "add"): 'hubkit standing add --on approved --quote "<the user\'s words>" --action "<what to do>"',
    ("standing", "drop"): 'hubkit standing drop {id} --quote "<the user\'s words>"',
    ("gate", "false-alarm"): 'hubkit gate false-alarm {id} --why "<what was wrong>"',
    ("gate", "set"): "hubkit gate set {id} hold|warn|off",
    ("inbound", "replied"): 'hubkit inbound replied {id} --note "<what was sent>"',
    ("work", "add"): 'hubkit work add --kind active --title "<title>"',
    ("peers", "set"): "hubkit peers set {id} --role <role> --free",
    ("how", "save"): "hubkit how save <name> --trigger <word> --steps-file <file> --quote \"<the user's words>\"",
}


def split_next(msg: str) -> tuple[str, str]:
    """(message, command) when the message carries `NEXT: command`, else (message, '')."""
    head, sep, tail = msg.partition("NEXT:")
    if sep and tail.strip().startswith("hubkit"):
        return head.strip().rstrip(".").rstrip(), tail.strip()
    return msg.strip(), ""


def code_of(err: BaseException) -> int:
    if isinstance(err, KeyError):
        return NOT_FOUND
    if isinstance(err, store.Refused):
        return REFUSED
    return USAGE


def next_for(cmd: str, action: str, code: int, ident: str, msg: str) -> str:
    """The exact command that moves on from this error."""
    ident = ident or "ID"
    if "unknown check" in msg:
        return "hubkit gate ls"
    if code == NOT_FOUND:
        return LS_CMD.get(cmd, "hubkit mine")
    if code == REFUSED:
        if cmd == "order" and action == "close":
            return USAGE_NEXT[cmd, action].format(id=ident)
        return f"hubkit order status {ident}" if cmd == "order" else LS_CMD.get(cmd, "hubkit mine")
    if (cmd, action) in USAGE_NEXT:
        return USAGE_NEXT[cmd, action].format(id=ident)
    return "hubkit doctor" if cmd in ("bind", "unbind", "hooks", "skills") else f"hubkit {cmd} --help"


def fail(a: argparse.Namespace, err: BaseException) -> int:
    """Print the error with its NEXT line (stderr, and a JSON document on stdout under --json); return its exit code."""
    raw = str(err.args[0]) if err.args else str(err)
    msg, nxt = split_next(raw)
    code = code_of(err)
    nxt = nxt or next_for(a.cmd, getattr(a, "action", ""), code, getattr(a, "id", "") or getattr(a, "name", ""), msg)
    print(f"error: {msg}\nNEXT: {nxt}", file=sys.stderr)
    if getattr(a, "json", False):
        print(json.dumps({"error": msg, "next": nxt, "code": code}))
    return code


class HubParser(argparse.ArgumentParser):
    """Usage errors end with a NEXT line (and a JSON error under --json); exit 2."""

    want_json = False

    def error(self, message: str) -> None:  # type: ignore[override]
        nxt = f"{self.prog} --help"
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}\nNEXT: {nxt}", file=sys.stderr)
        if HubParser.want_json:
            print(json.dumps({"error": message, "next": nxt, "code": USAGE}))
        self.exit(USAGE)


def decorate(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Give each verb its `--json` flag (read verbs) and its examples."""
    for name, sp in sub.choices.items():
        if name in JSON_VERBS and not any("--json" in x.option_strings for x in sp._actions):
            sp.add_argument("--json", action="store_true", help="print one JSON document on stdout")
        ex = EXAMPLES.get(name)
        if ex:
            sp.epilog = "examples:\n" + "\n".join(f"  {e}" for e in ex)
            sp.formatter_class = argparse.RawDescriptionHelpFormatter


def json_view(a: argparse.Namespace, cfg: dict) -> object | None:
    """The JSON document for a read verb, or None when this verb or action has none (it then prints as usual)."""
    from . import gatectl, ledger, owed, playbooks, replies, roster, standrules, status, threads
    from .orders import Orders

    now = time.time()
    act = getattr(a, "action", "")
    if a.cmd == "order" and act in ("ls", "status"):
        o = Orders(cfg)
        return {"orders": [o.get(a.id)] if act == "status" and a.id else o.live()}
    if a.cmd == "work" and act == "ls":
        return {"work": status.work_load(store.state(cfg))}
    if a.cmd == "req" and act == "ls" and getattr(a, "learned", False):
        return owed.learned(cfg, now)
    if a.cmd == "req" and act == "ls":
        return {"requests": owed.live(cfg)}
    if a.cmd == "req" and act == "show" and a.id:
        return {"request": owed.show(cfg, a.id)}
    if a.cmd == "mine":
        return {"lines": owed.mine(cfg), "threads": threads.build(cfg, now)}
    if a.cmd == "thread" and act == "ls":
        return {"threads": threads.build(cfg, now)}
    if a.cmd == "thread" and act == "show" and a.id:
        return {"lines": (shown := threads.show(cfg, a.id, now))[0], "thread": shown[1]}
    if a.cmd == "peers" and act == "ls":
        return roster.load(cfg)
    if a.cmd == "inbound" and act == "ls":
        return {"lines": replies.ls_lines(cfg, now)}
    if a.cmd == "find":
        return {"lines": ledger.find(cfg, a.word, a.since)}
    if a.cmd == "digest":
        return {"lines": status.digest(cfg, a.since)}
    if a.cmd == "gate" and act == "ls":
        return {"lines": gatectl.ls_lines(cfg)}
    if a.cmd == "standing" and act == "ls":
        return {"rules": standrules.active(cfg, now)}
    if a.cmd == "how" and act == "ls":
        return {"playbooks": playbooks.load_all()}
    if a.cmd == "next":
        return pick(cfg, now)
    return None


def _id_of(text: str) -> str:
    m = re.search(r"\b(O-\d{6}-[0-9a-f]+|[A-Z]\d+)\b", text)
    return m.group(1) if m else ""


def _item(kind: str, text: str, nxt: str = "", ident: str = "") -> dict:
    why, _, tail = text.partition("NEXT:")
    m = re.search(r"hubkit .*?(?=, or |; | then |$)", tail)
    return {"kind": kind, "id": ident or _id_of(text), "why": why.strip().rstrip("."), "next": m.group(0).strip() if m else nxt}


def pick(cfg: dict, now: float) -> dict:
    """The single most urgent action for the hub now, built from the lines the hooks and the tick already produce."""
    from . import gate, routed, status
    from .orders import Orders

    orders = Orders(cfg)
    for line in gate.blocker_reasons(cfg, "", now):
        return _item("blocker", line)
    alerts = status.evaluate(cfg, orders, {"count": 0, "last": ""}, now)

    def first(*names: str) -> list[dict]:
        return [x for x in alerts if x["key"].split(":")[1:2] and x["key"].split(":")[1] in names]

    for a in first("deadline", "no_first_action", "stalled", "held_long", "unconfirmed_expired"):
        oid = a["key"].split(":")[0]
        return _item("overdue_order", a["msg"], f"hubkit order status {oid}", oid)
    for a in first("answer_due"):
        return _item("reply_due", a["msg"])
    for line in routed.prompt_lines(cfg, now):
        return _item("stalled_routed", line)
    for a in first("routed_stalled", "head_moved"):
        return _item("stalled_routed", a["msg"])
    for a in first("reply_owed"):
        return _item("reply_owed", a["msg"])
    for a in first("request_overdue"):
        return _item("overdue_request", a["msg"])
    rest = [a for a in alerts if not a["key"].startswith("routed:")]
    if rest:
        return _item("alert", rest[0]["msg"], "hubkit status")
    asks = [i for i in status.work_load(store.state(cfg)) if i.get("kind") == "ask"]
    if asks:
        i = asks[0]
        return {"kind": "waiting_on_user", "id": i["id"], "why": f"waiting on the user: {i['title']}" + (f" (blocks: {i['note']})" if i.get("note") else ""),
                "next": f"hubkit work done {i['id']} --note \"<the user's answer>\" (once they answer)"}
    return {"kind": "none", "id": "", "why": "nothing is due", "next": "hubkit mine"}


def render_next(item: dict) -> str:
    if item["kind"] == "none":
        return f"{item['why']}\nNEXT: {item['next']}"
    return f"{item['kind']} {item['id']}: {item['why']}\nNEXT: {item['next']}"
