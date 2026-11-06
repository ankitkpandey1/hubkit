"""Per-class trust from what the user did with the hub's proposals: accepted, edited, rejected.

`hubkit trust record --class C --outcome accept|edit|reject` appends to trust.jsonl; `trust ls` shows each class as
`auto` (enough accepts and nothing else: do it and tell him) or `ask` (propose first). The session block names the classes
that earned `auto` and those he has rejected."""

from __future__ import annotations

import re
import time

from . import store

OUTCOMES = ("accept", "edit", "reject")
AUTO_AFTER = 5  # clean accepts in a row before a class is done without asking


def record(cfg: dict, cls: str, outcome: str) -> str:
    if outcome not in OUTCOMES:
        raise ValueError(f"--outcome must be one of {', '.join(OUTCOMES)}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", cls):
        raise ValueError("--class must be kebab-case (a-z, 0-9, -)")
    store.append_row(store.state(cfg) / "trust.jsonl", {"ts": time.time(), "cls": cls, "outcome": outcome})
    return f"{cls}: {outcome} recorded -> {mode(cfg).get(cls, 'ask')}"


def mode(cfg: dict) -> dict[str, str]:
    """`auto` for a class whose last AUTO_AFTER outcomes are all accepts; otherwise `ask`."""
    by: dict[str, list[str]] = {}
    for r in store.read_rows(store.state(cfg) / "trust.jsonl")[0]:
        by.setdefault(r["cls"], []).append(r["outcome"])
    return {c: ("auto" if len(o) >= AUTO_AFTER and all(x == "accept" for x in o[-AUTO_AFTER:]) else "ask") for c, o in by.items()}


def lines(cfg: dict) -> list[str]:
    """One status-block line naming auto and rejected classes, or nothing."""
    modes = mode(cfg)
    rejected = sorted({r["cls"] for r in store.read_rows(store.state(cfg) / "trust.jsonl")[0] if r["outcome"] == "reject"})
    auto = sorted(c for c, m in modes.items() if m == "auto")
    parts = ([f"auto (do it, then tell him): {', '.join(auto)}"] if auto else []) + ([f"he has rejected: {', '.join(rejected)}"] if rejected else [])
    return ["trust: " + "; ".join(parts)] if parts else []
