"""One table for the four things hubkit tracks: orders, routed work, requests and replies.

For each kind: its states (live or terminal, with the clocks that run in it), and the allowed transitions
`(from, verb, to, requires)`. The table only names things; the modules keep computing the minutes and doing the writes.
`requires` is a tuple of proof names (`evidence`, `quote`, `note`, `sha`, `github`); a transition with several names
needs any one of them, an empty tuple needs nothing. A value counts as given when it is not None and, for text, not
blank (an evidence dict counts even when empty: the order's own `done_when` check judges what is in it).

Clock names say where the minutes come from: `cfg:a.b` is a key of config.toml, `intents:a.b` a key of intents.toml
(`*` stands for any order kind, each with a built-in default), `fn:module.name` a function that learns the limit.
A live state with no clock is `clockless`, on purpose. The state of a work item is not stored: `state_of` derives it
from the item's fields (the strings on disk for orders are the state names below)."""

from __future__ import annotations

import importlib
from typing import Any

NEW = "new"  # the state before an item exists

# state -> (live, clocks); an empty clocks tuple on a live state means it is clockless on purpose
_Spec = dict[str, tuple[bool, tuple[str, ...]]]

STATES: dict[str, _Spec] = {
    "order": {
        "unconfirmed": (True, ("intents:intake.unconfirmed_ttl_min",)),
        "open": (True, ("intents:kinds.*.first_action_min", "intents:kinds.*.stall_min")),
        "held": (True, ("intents:intake.held_review_min",)),
        "closed": (False, ()),
        "cancelled": (False, ()),
        "expired": (False, ()),
    },
    "routed": {
        "routed": (True, ("cfg:routed.stall_min", "cfg:routed.review_stall_min")),
        "chased": (True, ("cfg:routed.stall_min", "cfg:routed.review_stall_min")),
        "reported": (True, ("cfg:routed.stall_min", "cfg:routed.review_overdue_min")),
        "answered": (True, ()),  # closed by the next tick
        "closed": (False, ()),
    },
    "request": {
        "open": (True, ("fn:owed.limits",)),
        "noted": (True, ("fn:owed.limits",)),
        "chased": (True, ("fn:owed.limits",)),
        "done": (False, ()),
        "dropped": (False, ()),
    },
    "reply": {
        "open": (True, ("cfg:watcher.reply_owed_min", "fn:replies.limit")),
        "due": (True, ()),  # the answer is owed now: the alert fires at once, there is no limit to wait for
        "replied": (False, ()),
    },
}

CLOCKLESS: dict[str, tuple[str, ...]] = {"routed": ("answered",), "reply": ("due",)}  # live on purpose without a limit to wait for

# (from, verb, to, requires)
_T = tuple[str, str, str, tuple[str, ...]]
_ORDER_LIVE = ("unconfirmed", "open", "held")
_ROUTED_LIVE = ("routed", "chased", "reported")
_REQ_LIVE = ("open", "noted", "chased")

TRANSITIONS: dict[str, tuple[_T, ...]] = {
    "order": (
        (NEW, "open", "open", ()),
        (NEW, "open", "unconfirmed", ()),
        ("unconfirmed", "confirm", "open", ()),
        *((s, "hold", "held", ("quote",)) for s in _ORDER_LIVE),
        ("held", "resume", "open", ()),
        *((s, "cancel", "cancelled", ("quote",)) for s in _ORDER_LIVE),
        *((s, "expire", "expired", ("note",)) for s in _ORDER_LIVE),
        ("open", "close", "closed", ("evidence",)),
        ("held", "close", "closed", ("evidence",)),
    ),
    "routed": (
        (NEW, "route", "routed", ()),
        ("routed", "chase", "chased", ()),
        ("chased", "chase", "chased", ()),
        ("reported", "chase", "reported", ()),
        *((s, "report", "reported", ()) for s in _ROUTED_LIVE),
        *((s, "answer", "answered", ()) for s in _ROUTED_LIVE),
        *((s, "close", "closed", ("github", "evidence", "note")) for s in (*_ROUTED_LIVE, "answered")),
    ),
    "request": (
        (NEW, "add", "open", ()),
        ("open", "note", "noted", ("note",)),
        ("noted", "note", "noted", ("note",)),
        ("chased", "note", "chased", ("note",)),
        *((s, "chase", "chased", ()) for s in _REQ_LIVE),
        *((s, "done", "done", ("evidence",)) for s in _REQ_LIVE),
        *((s, "drop", "dropped", ("quote",)) for s in _REQ_LIVE),
    ),
    "reply": (
        (NEW, "ingest", "open", ()),
        ("open", "settle", "due", ()),
        ("due", "unsettle", "open", ()),
        ("open", "replied", "replied", ("note",)),
        ("due", "replied", "replied", ("note",)),
    ),
}


class LifecycleError(ValueError):
    """A transition the table does not allow, or one whose proof is missing. The message ends with `NEXT: ...`."""


def kinds() -> list[str]:
    return list(STATES)


def states(kind: str) -> list[str]:
    return list(STATES[kind])


def live_states(kind: str) -> tuple[str, ...]:
    """States that count as open."""
    return tuple(s for s, (live, _) in STATES[kind].items() if live)


def terminal_states(kind: str) -> tuple[str, ...]:
    return tuple(s for s, (live, _) in STATES[kind].items() if not live)


def clocks(kind: str, state: str) -> tuple[str, ...]:
    """The names of the clocks that run in `state` (empty: none, or it is clockless on purpose)."""
    return STATES[kind][state][1]


def verbs(kind: str) -> list[str]:
    return sorted({v for _, v, _, _ in TRANSITIONS[kind]})


def _step(kind: str, state: str, verb: str) -> list[_T]:
    return [t for t in TRANSITIONS[kind] if t[0] == state and t[1] == verb]


def allowed(kind: str, state: str, verb: str) -> bool:
    return bool(_step(kind, state, verb))


def next_states(kind: str, state: str, verb: str) -> list[str]:
    return [t[2] for t in _step(kind, state, verb)]


def _given(v: Any) -> bool:
    return v is not None and not (isinstance(v, str) and not v.strip())


def check(kind: str, state: str, verb: str, **given: Any) -> None:
    """Refuse a transition the table does not allow, or one without its proof. `given` holds the proof arguments by name."""
    if kind not in STATES:
        raise LifecycleError(f"unknown tracker kind {kind!r}. NEXT: use one of {', '.join(STATES)}")
    steps = _step(kind, state, verb)
    if not steps:
        ok = sorted({t[1] for t in TRANSITIONS[kind] if t[0] == state})
        raise LifecycleError(
            f"{kind} cannot {verb} from {state}. NEXT: "
            + (f"from {state} a {kind} can: {', '.join(ok)}" if ok else f"{state} is final; open a new {kind} if the work is not done"))
    need = steps[0][3]
    if need and not any(_given(given.get(n)) for n in need):
        raise LifecycleError(f"{kind} {verb} needs {' or '.join(need)}. NEXT: give {' or '.join(need)} for the {verb}")


def state_of(kind: str, item: dict) -> str:
    """The state of a live work item (kind `routed`, `request` or `reply`), derived from its fields."""
    if kind == "routed":
        return "answered" if item.get("answered") else "reported" if item.get("reported") else "chased" if int(item.get("chases", 0)) > 0 else "routed"
    if kind == "request":
        return "chased" if int(item.get("chases", 0)) > 0 else "noted" if item.get("notes") else "open"
    if kind == "reply":
        return "due" if item.get("due") else "open"
    return str(item.get("state", ""))


def describe() -> list[str]:
    """The whole table as lines: per kind its states with clocks, then each transition with its proof."""
    out: list[str] = []
    for kind in STATES:
        out.append(f"{kind}")
        for s, (live, cl) in STATES[kind].items():
            tag = "live" if live else "final"
            out.append(f"  state {s} ({tag}): clocks " + (", ".join(cl) if cl else "none"))
        for a, v, b, req in TRANSITIONS[kind]:
            out.append(f"  {a} --{v}--> {b}" + (f" [needs {' or '.join(req)}]" if req else ""))
    return out


def _resolves(name: str, cfg: dict, intents: dict) -> bool:
    scheme, _, path = name.partition(":")
    if scheme == "fn":
        mod, _, fn = path.partition(".")
        try:
            return callable(getattr(importlib.import_module(f"{__package__}.{mod}"), fn, None))
        except ImportError:
            return False
    if "*" in path:
        return True  # per-kind keys have built-in defaults
    node: Any = cfg if scheme == "cfg" else intents
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def audit(cfg: dict) -> list[str]:
    """Problems found in the state files against the table: a state the table lacks, a live state whose clock names
    resolve to nothing, or a table whose live state has no clock and is not marked clockless (an empty list is a pass)."""
    from . import status, store
    from .orders import Orders

    problems: list[str] = []
    orders = Orders(cfg)
    for oid, o in orders.all().items():
        if o["state"] not in STATES["order"]:
            problems.append(f"order {oid} has state {o['state']!r}, which is not in the lifecycle table")
    for i in status.work_load(store.state(cfg)):
        kind = str(i.get("kind", ""))
        if kind not in STATES:
            continue
        s = state_of(kind, i)
        if s not in STATES[kind]:
            problems.append(f"{kind} {i.get('id', '?')} has state {s!r}, which is not in the lifecycle table")
        elif STATES[kind][s][0]:
            for c in clocks(kind, s):
                if not _resolves(c, cfg, orders.intents):
                    problems.append(f"{kind} {i.get('id', '?')} is live in {s} but its clock {c} has no source")
    live_clockless = {k: [s for s in live_states(k) if not clocks(k, s)] for k in STATES}
    for k, ss in live_clockless.items():
        for s in ss:
            if s not in CLOCKLESS.get(k, ()):
                problems.append(f"{k} {s} is live with no clock and is not marked clockless")
    return problems


def audit_line(cfg: dict) -> str:
    """The `hubkit doctor` line: `lifecycle: ok` or the problems."""
    try:
        found = audit(cfg)
    except (OSError, ValueError, KeyError, TypeError) as e:
        return f"lifecycle: could not audit ({type(e).__name__}: {e})"
    return "lifecycle: ok" if not found else "lifecycle: " + "; ".join(found[:5]) + (f" (+{len(found) - 5} more)" if len(found) > 5 else "")


__all__ = ["NEW", "STATES", "TRANSITIONS", "LifecycleError", "allowed", "audit", "audit_line", "check", "clocks", "describe",
           "kinds", "live_states", "next_states", "state_of", "states", "terminal_states", "verbs"]
