"""Hook entry points. Every hook: no-op outside the live bound hub, fail open, exit 0."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

from . import config, store
from .orders import Orders, hook_intake
from .redact import redact
from .status import read_status, render_block


# ---- bind: hub identity = session id + pid + procStart, liveness read from /proc ----

def proc_start(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def session_file(cfg: dict, pid: int) -> dict | None:
    try:
        return json.loads((config.sessions_dir(cfg) / f"{pid}.json").read_text())
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
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            return None
    return None


def bind_path(cfg: dict) -> Path:
    return store.state(cfg, create=False) / "hub.bind"


def read_bind(cfg: dict) -> dict | None:
    try:
        return json.loads(bind_path(cfg).read_text())
    except (OSError, ValueError):
        return None


def bind_alive(b: dict | None) -> bool:
    return bool(b) and proc_start(int(b["pid"])) == str(b["procStart"])


def bind(cfg: dict, pid: int | None) -> str:
    pid = pid or find_session_pid(cfg)
    sess = session_file(cfg, pid) if pid else None
    if not sess:
        raise RuntimeError("no Claude session found for this process (pass --pid)")
    if sess.get("kind") != "interactive":
        raise RuntimeError(f"session kind is {sess.get('kind')!r}, only interactive sessions can be the hub")
    cur = read_bind(cfg)
    if bind_alive(cur) and cur["session_id"] != sess["sessionId"]:
        raise RuntimeError(f"live hub already bound: {cur.get('name')} (pid {cur['pid']})")
    rec = {"session_id": sess["sessionId"], "pid": pid, "procStart": proc_start(pid),
           "name": sess.get("name", ""), "bound_at": time.time()}
    store.state(cfg)  # create the state dir
    store.write_atomic(bind_path(cfg), json.dumps(rec))
    return f"bound {rec['name'] or rec['session_id']} (pid {pid})"


def unbind(cfg: dict) -> str:
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
    return bool(sess) and sess.get("kind") == "interactive" and sess.get("sessionId") == b["session_id"]


# ---- the hooks ----

def hook_session(cfg: dict, payload: dict) -> str:
    if not is_hub(cfg, payload):
        return ""  # nothing printed, nothing written outside the bound hub
    store.touch_seen(store.state(cfg), "session")
    return render_block(cfg, Orders(cfg), read_status(cfg))


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
    if len(rows) > 200:
        store.write_atomic(recent_path, "".join(json.dumps(r) + "\n" for r in rows[-50:]))
    if not why:
        return ""
    quote = redact(text)[0][:200]
    return (f'hubkit CORRECTION ({why}): record it this turn. Quote: "{quote}". '
            "Run learn.py add --class <existing-or-new-kebab> --gate <G1..G8> --quote <the words> "
            "--situation .. --wrong .. --right .. --evidence .. (reuse a class from the block if it is the same mistake).")


def hook_prompt(cfg: dict, payload: dict) -> str:
    if not is_hub(cfg, payload):
        return ""
    sd = store.state(cfg)
    store.touch_seen(sd, "prompt")
    text = payload.get("prompt") or ""
    origin = (payload.get("origin") or {}).get("kind")
    if not store.is_human(text, origin):
        return ""
    orders = Orders(cfg)
    lines = [hook_intake(orders, text)]
    corr = correction_line(orders, sd, text, time.time())
    return "\n".join([*lines[0], *([corr] if corr else [])])


def hook_stop(cfg: dict, payload: dict) -> str:
    """Log only: would the guard have blocked? Never prints, never blocks."""
    if not is_hub(cfg, payload) or payload.get("stop_hook_active"):
        return ""
    sd = store.state(cfg)
    store.touch_seen(sd, "stop")
    last = (payload.get("last_assistant_message") or "").strip()
    live = [o for o in Orders(cfg).live() if o["state"] == "open"]
    tail = last.splitlines()[-1] if last else ""
    phrases = cfg["stop_guard"]["phrases"]
    if live and (tail.endswith("?") or any(p in last.lower() for p in phrases)):
        store.append_row(sd / "stop-guard.jsonl",
                         {"would_block": True, "order": live[0]["id"], "last_line": store.safe(tail, 200)})
    return ""


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


HOOKS = {"session": hook_session, "prompt": hook_prompt, "stop": hook_stop, "record": hook_record}


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
