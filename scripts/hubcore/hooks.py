"""Hook entry points. Every hook: no-op outside the live bound hub, fail open, exit 0."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import TypeGuard

from . import compat, config, gate, lessons, routed, store
from .orders import Orders, hook_intake
from .redact import redact
from .status import read_status, render_block

# ---- bind: hub identity = session id + pid + procStart, liveness read through compat ----

def proc_start(pid: int) -> str | None:
    """Start-time token of a live process (see compat), or None when it is gone."""
    return compat.proc_start(pid)


def session_file(cfg: dict, pid: int) -> dict | None:
    """The Claude Code session file for a pid, or None."""
    try:
        return json.loads((config.sessions_dir(cfg) / f"{pid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def find_session_pid(cfg: dict) -> int | None:
    """Walk up the parent chain from this process to the nearest pid with a session file."""
    pid = os.getppid()
    for _ in range(12):
        if pid <= 1:
            return None
        if session_file(cfg, pid):
            return pid
        parent = compat.parent_pid(pid)
        if parent is None:
            return None
        pid = parent
    return None


def bind_path(cfg: dict) -> Path:
    """File that records which session is the hub."""
    return store.state(cfg, create=False) / "hub.bind"


def read_bind(cfg: dict) -> dict | None:
    """The hub bind record, or None when absent or unreadable."""
    try:
        return json.loads(bind_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def bind_alive(b: dict | None) -> TypeGuard[dict]:
    """True when the bind record names a process that is still the same process (pid plus start time)."""
    if not b:
        return False
    return proc_start(int(b["pid"])) == str(b["procStart"])


def bind(cfg: dict, pid: int | None) -> str:
    """Bind the session of `pid` (or of this process tree) as the hub; refuses a second live hub."""
    pid = pid or find_session_pid(cfg)
    sess = session_file(cfg, pid) if pid else None
    if not sess or not pid:
        raise RuntimeError("no Claude session found for this process (pass --pid)")
    if sess.get("kind") != "interactive":
        raise RuntimeError(f"session kind is {sess.get('kind')!r}, only interactive sessions can be the hub")
    cur = read_bind(cfg)
    if bind_alive(cur) and cur["session_id"] != sess["sessionId"]:
        raise RuntimeError(f"live hub already bound: {cur.get('name')} (pid {cur['pid']})")
    rec = {"session_id": sess["sessionId"], "pid": pid, "procStart": proc_start(pid),
           "name": sess.get("name", ""), "cwd": sess.get("cwd", ""), "bound_at": time.time()}
    store.state(cfg)  # create the state dir
    store.write_atomic(bind_path(cfg), json.dumps(rec))
    return f"bound {rec['name'] or rec['session_id']} (pid {pid})"


def unbind(cfg: dict) -> str:
    """Remove the hub bind."""
    bind_path(cfg).unlink(missing_ok=True)
    return "unbound"


def is_hub(cfg: dict, payload: dict) -> bool:
    """True only for the live bound interactive hub session."""
    if os.environ.get("HUBKIT_DISABLE"):
        return False
    b = read_bind(cfg)
    if not bind_alive(b) or payload.get("session_id") != b["session_id"]:
        return False
    sess = session_file(cfg, int(b["pid"]))
    return sess is not None and sess.get("kind") == "interactive" and sess.get("sessionId") == b["session_id"]


# ---- the hooks ----

def _session_by_id(cfg: dict, session_id: str | None) -> tuple[int | None, dict | None]:
    """The running session file for this session id (the hook payload names it)."""
    for f in config.sessions_dir(cfg).glob("*.json"):
        sess = session_file(cfg, int(f.stem)) if f.stem.isdigit() else None
        if sess and session_id and sess.get("sessionId") == session_id and proc_start(int(f.stem)) == str(sess.get("procStart")):
            return int(f.stem), sess
    return None, None


def _rebind_candidate(cfg: dict, payload: dict) -> int | None:
    """Pid of this session when it may take over a dead bind, else None.

    Only a session that resumes the bound session id, or that has the configured hub name AND the
    bound cwd, qualifies. A live bind is never taken over; any other session is left alone.
    """
    b = read_bind(cfg)
    if not b or bind_alive(b):
        return None
    pid, sess = _session_by_id(cfg, payload.get("session_id"))
    if not sess or sess.get("kind") != "interactive":
        return None
    resumed = sess["sessionId"] == b["session_id"]
    named = bool(b.get("cwd")) and sess.get("cwd") == b["cwd"] and sess.get("name") == cfg["hub"]["name"]
    return pid if resumed or named else None


def hook_session(cfg: dict, payload: dict) -> str:
    """SessionStart hook: print the status block, and re-bind a hub that resumed."""
    rebound = ""
    if not is_hub(cfg, payload):
        pid = _rebind_candidate(cfg, payload)
        if pid is None:
            return ""  # nothing printed, nothing written outside the bound hub
        rebound = f"hubkit: {bind(cfg, pid)}; the old bind was dead (re-bound by the SessionStart hook)\n"
    store.touch_seen(store.state(cfg), "session")
    return rebound + render_block(cfg, Orders(cfg), read_status(cfg))


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 2}


def correction_line(orders: Orders, sd: Path, text: str, now: float) -> str:
    """Detect a correction (configured phrases, or the same instruction repeated) and inject a record prompt."""
    cfg = orders.intents.get("corrections", {})
    why = ""
    if any(re.search(p, text) for p in cfg.get("patterns", [])):
        why = "correction phrase"
    recent_path = sd / "recent-prompts.jsonl"
    rows, _ = store.read_rows(recent_path)
    mine = _words(text)
    window = float(cfg.get("repeat_window_min", 30)) * 60
    if not why and len(mine) >= 4:
        for r in rows:
            if now - r["ts"] <= window and len(_words(r["text"])) >= 4:
                other = _words(r["text"])
                if len(mine & other) / len(mine | other) >= float(cfg.get("repeat_similarity", 0.6)):
                    why = "repeated instruction"
                    break
    store.append_row(recent_path, {"text": store.safe(text, 300), "ts": now})
    if len(rows) > 2000:  # `hubkit asks ls` reads this file: keep a day or more of the user's words
        store.write_atomic(recent_path, "".join(json.dumps(r) + "\n" for r in rows[-1000:]))
    if not why:
        return ""
    quote = redact(text)[0][:200]
    return (f'hubkit CORRECTION ({why}): record it this turn. Quote: "{quote}". '
            "Run learn.py add --class <existing-or-new-kebab> --gate <G1..G8> --quote <the words> "
            "--situation .. --wrong .. --right .. --evidence .. (reuse a class from the block if it is the same mistake).")


def unordered_chase_lines(cfg: dict, orders: Orders, text: str, now: float) -> list[str]:
    """A short chase ("any progress?") with no order live: nothing counts it, so answer it from the job list."""
    intake = orders.intents.get("intake", {})
    chase = intake.get("chase")
    if not chase or orders.live() or len(text) > int(intake.get("verb_max_chars", 120)) or not re.search(chase, text):
        return []
    from . import slow

    store.append_row(store.state(cfg) / "unordered-chases.jsonl", {"ts": now})
    jobs = slow.jobs(cfg, orders, now)
    return ["hubkit: no order is live, so this chase is on the job list:", *(slow.block_lines(jobs, now) or ["no live job either: say so plainly"])]


def hook_prompt(cfg: dict, payload: dict) -> str:
    """UserPromptSubmit hook: open, chase, hold or cancel orders from the user's words; inject correction and overdue lines."""
    if not is_hub(cfg, payload):
        return ""
    sd = store.state(cfg)
    store.touch_seen(sd, "prompt")
    text = payload.get("prompt") or ""
    origin = (payload.get("origin") or {}).get("kind")
    now = time.time()
    routed.scan_transcript(cfg, payload.get("transcript_path"))  # heard-from before the stall clock is read
    if peer := routed.peer_name(text):  # a peer message that did reach this hook: heard now, opens nothing
        routed.heard(cfg, [(now, peer, text)])
    if not store.is_human(text, origin):
        return ""
    orders = Orders(cfg)
    corr = correction_line(orders, sd, text, now)
    return "\n".join([*routed.prompt_lines(cfg, now), *hook_intake(orders, text), *unordered_chase_lines(cfg, orders, text, now), *([corr] if corr else [])])


def _answer_text(resp: object) -> str:
    """The user's answer text from an AskUserQuestion tool_response (dict with answers, or plain text)."""
    if isinstance(resp, dict):
        ans = resp.get("answers")
        return "\n".join(str(v) for v in ans.values()) if isinstance(ans, dict) else ""
    return resp if isinstance(resp, str) else ""


def hook_answer(cfg: dict, payload: dict) -> str:
    """A card answer (AskUserQuestion) goes through the same intake as a typed prompt."""
    if not is_hub(cfg, payload) or payload.get("tool_name") != "AskUserQuestion":
        return ""
    store.touch_seen(store.state(cfg), "answer")
    text = _answer_text(payload.get("tool_response"))
    if not store.is_human(text):
        return ""
    routed.close_asks(cfg, time.time())  # the user answered the card: nothing is blocked on him now
    return "\n".join(hook_intake(Orders(cfg), text))


def hook_routed(cfg: dict, payload: dict) -> str:
    """SendMessage / Agent from the hub: open or refresh the routed item (see routed.from_tool)."""
    tool = payload.get("tool_name")
    if not is_hub(cfg, payload) or tool not in ("SendMessage", "Agent"):
        return ""
    store.touch_seen(store.state(cfg), "routed")
    routed.scan_transcript(cfg, payload.get("transcript_path"))
    ti = payload.get("tool_input") or {}
    forbids = lessons.brief_forbids_fix(cfg, str(ti.get("message") or ti.get("prompt") or ""))
    item = routed.from_tool(cfg, tool, payload.get("tool_input") or {}, payload.get("tool_response"),
                            str(payload.get("tool_use_id") or ""))
    if not item:
        return forbids or ""
    out = f"hubkit: tracking {item['id']} routed to {item['owner']}; stall clock {routed.opts(cfg)['stall_min']} min"
    if item.get("estimate_min") is not None:
        out += f"; estimate {item['estimate_min']:g}m"
        if item.get("estimate_defaulted"):
            out += (f" DEFAULTED ({item.get('estimate_basis', '')}), the brief had no `Estimate: <n>m` line. "
                    f"Set it: hubkit work estimate {item['id']} --minutes N --why \"...\"")
    out += queue_warning(cfg, item)
    for w in (lessons.brief_missing(cfg, str(ti.get("message") or ti.get("prompt") or "")), lessons.agent_cap_warning(cfg)):
        out += f"\n{w}" if w else ""
    return out + (f"\n{forbids}" if forbids else "")


def hook_pretool(cfg: dict, payload: dict) -> str:
    """PreToolUse on Bash and TaskStop: warn (never deny) about an unordered merge, an unconfirmed post, a banned command, a stopped agent."""
    tool = str(payload.get("tool_name") or "")
    if not is_hub(cfg, payload) or tool not in ("Bash", "TaskStop", "AskUserQuestion"):
        return ""
    warning = lessons.tool_warning(cfg, tool, payload.get("tool_input") or {})
    if not warning:
        return ""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": warning}})


def queue_warning(cfg: dict, item: dict) -> str:
    """A new job sent to an owner who already holds another live job, while an order is open: it queues behind that job."""
    if not item.get("behind") or len(item.get("events", [])) != 1 or not any(o["state"] == "open" for o in Orders(cfg).live()):
        return ""
    return (f"\nhubkit WARNING: {item['owner']} already holds {', '.join(item['behind'])}; {item['id']} queues behind it while an order is open. "
            "An urgent step never waits in a busy agent's queue. NEXT: send it to a free owner now")


def hook_stop(cfg: dict, payload: dict) -> str:
    """Block the turn end (JSON decision, which reaches the model; plain Stop stdout does not) while the hub owes
    something: arm the watch, chase a stalled item, back a verdict, or justify a question. Once per stop:
    `stop_hook_active` means the harness already sent the hub back, so it is let through."""
    if not is_hub(cfg, payload) or payload.get("stop_hook_active"):
        return ""
    sd = store.state(cfg)
    store.touch_seen(sd, "stop")
    routed.scan_transcript(cfg, payload.get("transcript_path"))
    last = (payload.get("last_assistant_message") or "").strip()
    reasons = gate.stop_reasons(cfg, last, transcript=payload.get("transcript_path"), cwd=payload.get("cwd"))
    if not reasons:
        return ""
    store.append_row(sd / "stop-guard.jsonl", {"blocked": True, "reasons": [store.safe(r, 300) for r in reasons]})
    return json.dumps({"decision": "block", "reason": "\n".join(reasons)})


def hook_record(cfg: dict, payload: dict) -> str:
    """Schema canary: keys and value types of the real payload, never values."""
    if not is_hub(cfg, payload):
        return ""
    sd = store.state(cfg)
    ev = payload.get("hook_event_name", "unknown")
    src = payload.get("source", "")
    sample = {k: type(v).__name__ for k, v in sorted(payload.items())}
    store.write_atomic(sd / "payload-samples" / f"{ev}{'-' + src if src else ''}.json",
                       json.dumps({"keys": sample, "at": time.time()}))
    return ""


HOOKS = {"session": hook_session, "prompt": hook_prompt, "stop": hook_stop, "record": hook_record, "answer": hook_answer, "routed": hook_routed, "pretool": hook_pretool}


def main(event: str) -> int:
    """Run one hook. Always exit 0. Errors are logged and shown only for the bound hub session."""
    payload: dict = {}
    cfg: dict = {}
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        payload = payload if isinstance(payload, dict) else {}
        cfg = config.load()
        out = HOOKS[event](cfg, payload)
        if event != "record":
            hook_record(cfg, payload)  # real payload keys and types, hub session only
        if out:
            print(out)
    except BaseException as e:  # a hook must never break the session
        try:
            if cfg and is_hub(cfg, payload):  # outside the hub: silent, no shared error file
                store.append_row(
                    config.state_dir(cfg) / "hook-errors.jsonl",
                    {"event": event, "error": repr(e)[:200], "trace": traceback.format_exc()[-600:]},
                )
                if event in ("session", "prompt"):
                    print(f"hubkit: hook {event} failed ({type(e).__name__}); see hook-errors.jsonl")
        except BaseException:
            pass
    return 0
