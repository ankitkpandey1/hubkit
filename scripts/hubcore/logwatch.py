"""Release logs and other order movement files: a line that needs a person is told to him at once, not found later.

A log line with `NEEDS A PERSON`, `YOUR DECISION` or `FAILED` is pending until a later step line (`[n/m] step: START|DONE`,
or a restart header `state and log in`) shows the run went on, or until the hub runs `hubkit log ack <key>`. Pending lines
raise a watcher alert and hold the Stop gate."""

from __future__ import annotations

import glob
import hashlib
import os
import re
import time
from pathlib import Path

from . import store
from .orders import Orders

MARK = re.compile(r"NEEDS A PERSON|YOUR DECISION|\bFAILED\b")
CLEAR = re.compile(r"\[\d+/\d+\]\s+\w+:\s+(?:START|DONE)\b|state and log in")
TAIL_BYTES = 200_000


def globs(cfg: dict, orders: Orders) -> list[str]:
    """`watcher.log_globs` plus the movement_files of every live order."""
    out = list(cfg["watcher"].get("log_globs", []))
    for o in orders.live():
        out += orders.intents["kinds"].get(o["kind"], {}).get("movement_files", [])
    return list(dict.fromkeys(out))


def _acked(cfg: dict) -> set[str]:
    return {r["key"] for r in store.read_rows(store.state(cfg) / "log-acks.jsonl")[0]}


def ack(cfg: dict, key: str) -> str:
    """Record that the line `key` was reported to him."""
    store.append_row(
        store.state(cfg) / "log-acks.jsonl", {"key": key, "ts": time.time()}
    )
    return f"acked {key}"


def pending(cfg: dict, orders: Orders, now: float | None = None) -> list[dict]:
    """The unreported marker lines of recently written logs: {key, path, line}."""
    now = now or time.time()
    max_age = float(cfg["watcher"].get("log_max_age_h", 24)) * 3600
    done = _acked(cfg)
    out = []
    for pat in globs(cfg, orders):
        for f in sorted(glob.glob(os.path.expanduser(pat))):
            try:
                if now - os.path.getmtime(f) > max_age:
                    continue
                with open(f, "rb") as fh:
                    fh.seek(max(0, os.path.getsize(f) - TAIL_BYTES))
                    lines = fh.read().decode("utf-8", errors="replace").splitlines()
            except OSError:
                continue
            hit = None
            for ln in lines:
                if MARK.search(ln):
                    hit = hit or ln.strip()
                elif CLEAR.search(ln):
                    hit = None
            if hit:
                key = f"log:{Path(f).parent.name}:{hashlib.sha1(hit.encode()).hexdigest()[:8]}"
                if key not in done:
                    out.append({"key": key, "path": f, "line": store.safe(hit, 160)})
    return out


def checks(cfg: dict, orders: Orders) -> list[dict]:
    """Watcher alerts, one per unreported line."""
    return [
        {
            "key": p["key"],
            "msg": (
                f"{p['path']}: {p['line']}. NEXT: tell him now in one line with the log path, then: hubkit log ack {p['key']}"
            ),
        }
        for p in pending(cfg, orders)
    ]


def gate_lines(cfg: dict, orders: Orders) -> list[str]:
    """Stop-gate reasons: the hub may not end its turn while such a line is unreported."""
    return [
        f"hubkit LOG: {p['path']} says {p['line']!r} and he has not been told. A log line that needs a person is reported at once, not found later. "
        f"NEXT: tell him now (what failed, the exact line, what you do next), then: hubkit log ack {p['key']}"
        for p in pending(cfg, orders)
    ]
