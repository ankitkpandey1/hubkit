"""Adaptive Stop-gate strength: each check has an id and a mode (hold, warn, off). A check the user keeps calling a
false alarm downgrades itself from hold to warn; a correction soon after it warned puts it back to hold."""

from __future__ import annotations

import re
import time

from . import store

KNOWN = ["watch", "routed", "blocker", "unpushed", "unswept-asks", "unchecked-claim", "pending-claim", "unrun-claim", "utc-time",
         "bare-refs", "slow", "requests", "ask-stale", "verdict", "ci", "reply-owed", "merge-state", "playbook", "prose-ask",
         "urgent-ask", "question", "report-length", "report-code"]
DEFAULT_MODES = {"report-length": "warn", "report-code": "warn"}
MODES = ("hold", "warn", "off")
_PREFIX = re.compile(r"^hubkit[^:]*:\s*")


def _opts(cfg: dict) -> dict:
    return cfg.get("gate") or {}


def _paths(cfg: dict) -> tuple:
    sd = store.state(cfg)
    return sd / "gate-modes.json", sd / "gate-events.jsonl", sd / "gate-warnings.jsonl", sd / ".gate.lock"


def _load(cfg: dict) -> dict:
    import json

    try:
        data = json.loads(_paths(cfg)[0].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(cfg: dict, data: dict) -> None:
    store.write_json_atomic(_paths(cfg)[0], data)


def mode_of(cfg: dict, cid: str) -> str:
    """hold, warn or off for a check: what was learned or set, else the check's default."""
    rec = _load(cfg).get(cid)
    return str(rec["mode"]) if rec else DEFAULT_MODES.get(cid, "hold")


def stats(cfg: dict, cid: str, now: float) -> tuple[int, int]:
    """(fires, false alarms) of a check in the window, since its last reset."""
    rec = _load(cfg).get(cid) or {}
    since = max(now - float(_opts(cfg).get("window_days", 14)) * 86400, float(rec.get("reset_at", 0)))
    rows = store.read_rows(_paths(cfg)[1])[0]
    mine = [r for r in rows if r.get("id") == cid and float(r.get("ts", 0)) >= since]
    return sum(r.get("op") == "fire" for r in mine), sum(r.get("op") == "false" for r in mine)


def apply(cfg: dict, pairs: list[tuple[str, str]], now: float) -> list[str]:
    """Log every fire. The reasons of checks in hold mode come back, tagged with how to call them wrong; a check in warn
    mode is queued as a warning for the next prompt; a check that is off is dropped."""
    holds: list[str] = []
    data: dict | None = None
    for cid, text in pairs:
        m = mode_of(cfg, cid)
        if m == "off":
            continue
        store.append_row(_paths(cfg)[1], {"ts": now, "op": "fire", "id": cid, "mode": m})
        tagged = f'{text} [check: {cid}; wrong? hubkit gate false-alarm {cid} --why "..."]'
        if m == "hold":
            holds.append(tagged)
            continue
        store.append_row(_paths(cfg)[2], {"ts": now, "id": cid, "text": tagged})
        data = _load(cfg) if data is None else data
        if cid in data:
            data[cid]["warned_at"] = now
    if data is not None:
        _save(cfg, data)
    return holds


def take_warnings(cfg: dict) -> list[str]:
    """The queued warn-mode reasons as `hubkit WARNING` lines, once each; the queue is emptied."""
    _, _, wp, _ = _paths(cfg)
    rows = store.read_rows(wp)[0]
    if not rows:
        return []
    wp.unlink(missing_ok=True)
    seen: dict[str, str] = {}
    for r in rows:
        seen[str(r["id"])] = _PREFIX.sub("", str(r["text"]))
    return [f"hubkit WARNING: {t}" for t in seen.values()]


def false_alarm(cfg: dict, cid: str, why: str, now: float) -> str:
    """Record that a check fired wrongly. Downgrades it to warn when enough of its recent fires were wrong."""
    if cid not in KNOWN:
        raise ValueError(f"unknown check {cid!r}; known: {', '.join(KNOWN)}")
    if not why.strip():
        raise ValueError('say why it was wrong: --why "..."')
    store.append_row(_paths(cfg)[1], {"ts": now, "op": "false", "id": cid, "why": store.safe(why, 200)})
    fires, bad = stats(cfg, cid, now)
    o = _opts(cfg)
    line = f"{cid}: {bad} false alarm(s) in {fires} fire(s)"
    with store.locked(_paths(cfg)[3]):
        data = _load(cfg)
        rec = data.get(cid) or {}
        if mode_of(cfg, cid) != "hold" or rec.get("by") == "hand":
            return line
        if bad < int(o.get("false_alarm_min", 3)) or bad < float(o.get("false_alarm_ratio", 0.3)) * max(fires, 1):
            return line
        if cid in o.get("never_downgrade", []):
            return line + "; kept at hold (safety floor)"
        data[cid] = {**rec, "mode": "warn", "by": "learned", "since": now}
        _save(cfg, data)
    return line + "; downgraded to warn (shown as a WARNING, no longer blocks)"


def _drop_warnings(cfg: dict, cid: str) -> None:
    """Forget queued warnings of a check whose mode just changed."""
    import json

    wp = _paths(cfg)[2]
    keep = [r for r in store.read_rows(wp)[0] if r.get("id") != cid]
    store.write_atomic(wp, "".join(json.dumps(r) + "\n" for r in keep))


def set_mode(cfg: dict, cid: str, mode: str, now: float) -> str:
    """Set a check's mode by hand; automatic learning leaves it alone."""
    if cid not in KNOWN:
        raise ValueError(f"unknown check {cid!r}; known: {', '.join(KNOWN)}")
    if mode not in MODES:
        raise ValueError(f"mode is one of {', '.join(MODES)}")
    with store.locked(_paths(cfg)[3]):
        data = _load(cfg)
        data[cid] = {"mode": mode, "by": "hand", "since": now, "reset_at": (data.get(cid) or {}).get("reset_at", 0)}
        _save(cfg, data)
        _drop_warnings(cfg, cid)
    return f"{cid}: {mode} (set by hand)"


def restore(cfg: dict, cid: str, now: float) -> str:
    """Put a check back to its default mode and forget its false alarms."""
    if cid not in KNOWN:
        raise ValueError(f"unknown check {cid!r}; known: {', '.join(KNOWN)}")
    with store.locked(_paths(cfg)[3]):
        data = _load(cfg)
        data[cid] = {"mode": DEFAULT_MODES.get(cid, "hold"), "by": "restored", "since": now, "reset_at": now}
        _save(cfg, data)
        _drop_warnings(cfg, cid)
    return f"{cid}: {DEFAULT_MODES.get(cid, 'hold')} (restored, false alarms reset)"


def on_correction(cfg: dict, now: float) -> list[str]:
    """A user correction soon after a learned-warn check warned: it was right to hold. Back to hold."""
    win = float(_opts(cfg).get("restore_window_min", 30)) * 60
    data = _load(cfg)
    back = [c for c, r in data.items() if r.get("by") == "learned" and r.get("mode") == "warn" and now - float(r.get("warned_at", 0)) <= win]
    return [f"hubkit: the user corrected you soon after check {c} warned, so it holds again. {restore(cfg, c, now)}" for c in back]


def downgraded(cfg: dict) -> list[str]:
    """Checks that have downgraded themselves."""
    return sorted(c for c, r in _load(cfg).items() if r.get("by") == "learned" and r.get("mode") == "warn")


def ls_lines(cfg: dict, now: float | None = None) -> list[str]:
    """One line per check: mode, fires, false alarms, why."""
    now = now or time.time()
    data = _load(cfg)
    out = []
    for cid in KNOWN:
        rec = data.get(cid) or {}
        fires, bad = stats(cfg, cid, now)
        why = {"learned": "learned (false alarms)", "hand": "set by hand", "restored": "restored"}.get(rec.get("by", ""), "default")
        out.append(f"{cid:<16} {mode_of(cfg, cid):<5} fires {fires:<4} false alarms {bad:<3} {why}")
    return out


def prompt_lines(cfg: dict, corrected: bool, now: float) -> list[str]:
    """For the prompt hook: a correction restores a check that warned a moment ago; queued warnings are shown once."""
    return [*(on_correction(cfg, now) if corrected else []), *take_warnings(cfg)]
