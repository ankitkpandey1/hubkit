"""Peer roster: who is a reviewer or a worker, who is busy or free, and how each peer did on past jobs.

Learned, not configured: a peer's own message ("Reviewer (code-review only) ... I'm idle now", "treat me as a free
worker", "busy running a benchmark, not free for other work") sets its role and state, stamped with the time it was
learned. A state older than `roster.stale_min` is shown as stale and not trusted. A peer that holds a live routed
item is busy whatever it last said. `hubkit peers set` and `forget` edit it by hand.

The roster also keeps, per peer, each finished job (kind, estimate, minutes taken, late or not) so the free-peer
suggestion prefers the peer with the better record for that kind, and the user's corrections that arrive shortly
after a brief to a role (`hubkit peers ls` shows the count). Role-specific brief lines come from
`[roles.<role>] brief_must_include` in the config.
"""

from __future__ import annotations

import json
import re
import statistics
import time

from . import routed, store

BODY_CHARS = 400  # a state is read from the opening of a message only: a long report that says "free" in passing is no notice
ROLE_CHARS = 40  # a role is announced at the start ("Reviewer (code-review only)", "Role: research worker"), not mentioned later
BRIEFS_KEPT = 20


def _opts(cfg: dict) -> dict:
    return cfg.get("roster", {})


def _path(cfg: dict):  # noqa: ANN202 - a Path
    return store.state(cfg) / "roster.json"


def load(cfg: dict) -> dict:
    """The roster file as a dict with peers, stats, briefs and role_corrections."""
    try:
        data = json.loads(_path(cfg).read_text(encoding="utf-8"))
    except ValueError, OSError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    for key, empty in (("peers", {}), ("stats", {}), ("briefs", []), ("role_corrections", {})):
        data.setdefault(key, empty)
    return data


def _save(cfg: dict, data: dict) -> None:
    store.write_json_atomic(_path(cfg), data)


def _words_re(words: list[str]) -> re.Pattern[str] | None:
    parts = [r"(?<!\w)" + re.escape(w) + r"(?!\w)" for w in words if w]
    return re.compile("|".join(parts), re.I) if parts else None


def _has(cfg: dict, key: str, text: str) -> bool:
    rx = _words_re(list(_opts(cfg).get(key, [])))
    return bool(rx and rx.search(text))


NOT_BUSY = re.compile(r"(?i)\b(not busy|no longer busy|not currently busy)\b")


def classify(cfg: dict, text: str) -> tuple[str, str]:
    """(role, state) a peer's message announces: role 'reviewer' / 'worker' / '', state 'free' / 'busy' / ''."""
    m = routed.PEER_BODY.search(text)
    body = (m[1] if m else text).strip()
    role = ""
    head = body[:ROLE_CHARS]
    if _has(cfg, "reviewer_words", head):
        role = "reviewer"
    elif _has(cfg, "worker_words", head):
        role = "worker"
    top = body[:BODY_CHARS]
    state = ""
    if NOT_BUSY.search(top):
        state = "free"
    elif _has(cfg, "busy_words", top):
        state = "busy"
    elif _has(cfg, "free_words", top):
        state = "free"
    return role, state


def learn(cfg: dict, events: list[tuple[float, str, str]]) -> int:
    """Learn role and state from the peers' own messages. events: (epoch, from-name, text). Returns how many peers changed."""
    todo = [(ts, name, text) for ts, name, text in events if name and text]
    if not todo:
        return 0
    changed = 0
    with store.locked(store.state(cfg) / "roster.lock"):
        data = load(cfg)
        for ts, name, text in sorted(todo, key=lambda e: e[0]):
            role, state = classify(cfg, text)
            if not role and not state:
                continue
            key = name.lower()
            p = data["peers"].setdefault(key, {"name": name, "role": "", "free": None, "learned_at": 0.0, "by": "message"})
            if ts < float(p.get("learned_at") or 0):
                continue
            if role and p.get("by_role") != "hand":
                p["role"] = role
            if state:
                p["free"] = state == "free"
            p["learned_at"] = ts
            changed += 1
        if changed:
            _save(cfg, data)
    return changed


def set_by_hand(cfg: dict, name: str, role: str | None, note: str | None, free: bool | None, now: float) -> str:
    """`hubkit peers set`: role, note and state are what the user says; a role set by hand is not overwritten by messages."""
    if not name.strip():
        raise ValueError("peers set needs a NAME")
    with store.locked(store.state(cfg) / "roster.lock"):
        data = load(cfg)
        p = data["peers"].setdefault(name.lower(), {"name": name, "role": "", "free": None, "learned_at": now, "by": "hand"})
        if role is not None:
            p["role"], p["by_role"] = role.strip().lower(), "hand"
        if note is not None:
            p["note"] = note
        if free is not None:
            p["free"], p["learned_at"], p["by"] = free, now, "hand"
        _save(cfg, data)
    return f"{name}: {p['role'] or 'no role'}, {_state_word(free, p)}"


def _state_word(free: bool | None, p: dict) -> str:
    cur = p.get("free") if free is None else free
    return "unknown" if cur is None else "free" if cur else "busy"


def forget(cfg: dict, name: str) -> str:
    """`hubkit peers forget NAME`: drop the peer and its job record."""
    with store.locked(store.state(cfg) / "roster.lock"):
        data = load(cfg)
        had = data["peers"].pop(name.lower(), None)
        data["stats"].pop(name.lower(), None)
        _save(cfg, data)
    return f"{name} forgotten" if had else f"{name} was not in the roster"


def holds(cfg: dict, name: str, skip: str = "") -> list[str]:
    """Ids of the live routed items this peer holds (other than `skip`)."""
    return [i["id"] for i in routed.items(cfg) if i["owner"].lower() == name.lower() and i["id"] != skip]


def view(cfg: dict, name: str, now: float, skip: str = "") -> dict:
    """role, state ('busy' / 'free' / 'stale' / 'unknown'), age in minutes. Holding a live job means busy."""
    p = load(cfg)["peers"].get(name.lower(), {})
    age = (now - float(p["learned_at"])) / 60 if p.get("learned_at") else None
    live = holds(cfg, name, skip)
    if live:
        state = "busy"
    elif p.get("free") is None or age is None:
        state = "unknown"
    elif age > float(_opts(cfg).get("stale_min", 120)):
        state = "stale"
    else:
        state = "free" if p["free"] else "busy"
    return {"name": p.get("name", name), "role": p.get("role", ""), "state": state, "age": age, "live": live, "note": p.get("note", "")}


# ---- the record: which peer did which kind of job, and was it within its estimate ----


def record_job(cfg: dict, item: dict, note: str, now: float) -> None:
    """A routed item finished (archived): keep kind, estimate, minutes taken and late/not for its owner."""
    try:
        if item.get("kind") != "routed" or item.get("state_ask") or note.startswith(("re-briefed", "answered", "state question")):
            return
        start = item.get("routed_at") or item.get("added")
        if not start or now <= start:
            return
        from . import slow

        took = round((now - float(start)) / 60, 1)
        est = item.get("estimate_min")
        row = {"kind": slow.step_of(item), "est": est, "took": took, "late": est is not None and took > float(est), "ts": now}
        with store.locked(store.state(cfg) / "roster.lock"):
            data = load(cfg)
            data["stats"].setdefault(str(item["owner"]).lower(), []).append(row)
            data["stats"][str(item["owner"]).lower()] = data["stats"][str(item["owner"]).lower()][-200:]
            _save(cfg, data)
    except OSError, ValueError, KeyError, TypeError:
        return  # a roster failure never blocks closing the item


def record_of(data: dict, name: str, kind: str | None = None) -> dict:
    """{'done', 'median', 'late'} for a peer, optionally for one job kind."""
    rows = [r for r in data["stats"].get(name.lower(), []) if kind is None or r["kind"] == kind]
    return {"done": len(rows), "median": round(statistics.median(r["took"] for r in rows), 1) if rows else None, "late": sum(bool(r["late"]) for r in rows)}


def _rank(data: dict, name: str, kind: str) -> tuple[float, float, str]:
    r = record_of(data, name, kind)
    if not r["done"]:
        return (0.5, 1e9, name)
    return (r["late"] / r["done"], float(r["median"]), name)


def free_peers(cfg: dict, kind: str, now: float, skip: str = "") -> list[dict]:
    """Fresh-free peers that fit the job kind ('review': reviewers; otherwise anyone who is not review-only), best record first."""
    data = load(cfg)
    out = []
    for key in data["peers"]:
        v = view(cfg, key, now)
        if key == skip.lower() or v["state"] != "free":
            continue
        if (v["role"] == "reviewer") != (kind == "review"):
            continue
        out.append(v)
    return sorted(out, key=lambda v: _rank(data, v["name"], kind))


def _pick(cfg: dict, v: dict) -> str:
    data = load(cfg)
    r = record_of(data, v["name"])
    rec = f", {r['done']} done, median {r['median']:g}m, {r['late']} late" if r["done"] else ""
    return f"{v['name']} ({v['role'] or 'no role'}, free{rec})"


# ---- at routing time ----


def role_lines(cfg: dict, role: str, text: str) -> list[str]:
    """Phrases the role's brief must include ([roles.<role>] brief_must_include) that this brief lacks."""
    need = (cfg.get("roles") or {}).get(role, {}).get("brief_must_include") or []
    out = []
    for phrase in need:
        try:
            found = bool(re.search(phrase, text, re.I))
        except re.error:
            found = phrase.lower() in text.lower()
        if not found:
            out.append(f'hubkit WARNING: this brief to a {role} lacks "{phrase}" (roles.{role}.brief_must_include). NEXT: add it and re-send')
    return out


def on_route(cfg: dict, item: dict, text: str, now: float | None = None) -> list[str]:
    """Warnings for one routed item (a new ask only), and the brief is noted against its role(s)."""
    now = now or time.time()
    try:
        owner = str(item["owner"])
        review = bool(item.get("review"))
        kind = "review" if review else "work"
        mine = view(cfg, owner, now, skip=item["id"])
        roles = [r for r in dict.fromkeys([mine["role"], "reviewer" if review else ""]) if r]
        out: list[str] = []
        for r in roles:
            out += role_lines(cfg, r, text)
        if len(item.get("events", [])) == 1:
            out += _fit_lines(cfg, item, mine, kind, now)
            _note_brief(cfg, roles, owner, now)
        return out
    except OSError, ValueError, KeyError, TypeError:
        return []


def _fit_lines(cfg: dict, item: dict, mine: dict, kind: str, now: float) -> list[str]:
    owner, out = str(item["owner"]), []
    alts = free_peers(cfg, kind, now, skip=owner)
    best = f" NEXT: send it to {_pick(cfg, alts[0])}" if alts else ""
    if kind == "review" and mine["role"] != "reviewer" and alts:
        out.append(f"hubkit WARNING: a review ask went to {owner} ({mine['role'] or 'no role'}) while a reviewer is free.{best}")
    elif kind == "work" and mine["role"] == "reviewer":
        out.append(f"hubkit WARNING: {owner} is review-only and this is not a review ask.{best or ' NEXT: send it to a free worker'}")
    elif alts and mine["state"] == "busy":
        why = f"holds {', '.join(mine['live'])}" if mine["live"] else f"said it was busy {int(mine['age'] or 0)}m ago"
        out.append(f"hubkit WARNING: {owner} is busy ({why}) and a fitting peer is free.{best}")
    return out


def _note_brief(cfg: dict, roles: list[str], owner: str, now: float) -> None:
    if not roles:
        return
    with store.locked(store.state(cfg) / "roster.lock"):
        data = load(cfg)
        data["briefs"] = [*data["briefs"], {"roles": roles, "owner": owner, "ts": now}][-BRIEFS_KEPT:]
        _save(cfg, data)


def note_correction(cfg: dict, now: float) -> None:
    """The user's correction arrived: count it against the role(s) briefed within `roster.correction_window_min`."""
    try:
        window = float(_opts(cfg).get("correction_window_min", 10)) * 60
        with store.locked(store.state(cfg) / "roster.lock"):
            data = load(cfg)
            hit = {r for b in data["briefs"] if 0 <= now - b["ts"] <= window for r in b["roles"]}
            if not hit:
                return
            for r in hit:
                data["role_corrections"][r] = int(data["role_corrections"].get(r, 0)) + 1
            data["briefs"] = [b for b in data["briefs"] if now - b["ts"] > window]
            _save(cfg, data)
    except OSError, ValueError, KeyError, TypeError:
        return


# ---- shown ----


def _age(v: dict) -> str:
    return "never" if v["age"] is None else f"{int(v['age'])}m"


def ls_lines(cfg: dict, now: float | None = None) -> list[str]:
    """`hubkit peers ls`: per peer role, state, age and record; per role the corrections after briefs."""
    now = now or time.time()
    data = load(cfg)
    out = []
    for key in sorted(data["peers"]):
        v = view(cfg, key, now)
        r = record_of(data, key)
        rec = f"jobs done {r['done']}, median {r['median']:g}m, late {r['late']}" if r["done"] else "jobs done 0"
        state = f"STALE (said {'free' if data['peers'][key].get('free') else 'busy'} {_age(v)} ago)" if v["state"] == "stale" else v["state"]
        held = f", holds {', '.join(v['live'])}" if v["live"] else ""
        out.append(f"{v['name']}: {v['role'] or 'no role'}, {state}{held} (learned {_age(v)} ago); {rec}" + (f"; {v['note']}" if v["note"] else ""))
    roles = sorted({*data["role_corrections"], *(cfg.get("roles") or {})})
    out += [f"role {r}: {int(data['role_corrections'].get(r, 0))} corrections after briefs to this role" for r in roles if data["role_corrections"].get(r)]
    from . import guardwatch

    if line := guardwatch.hosts_in_use_line(cfg):
        out.append(line)
    return out or ["no peers in the roster (it is learned from the peers' own messages, or `hubkit peers set NAME --role ROLE`)"]


def block_lines(cfg: dict, now: float) -> list[str]:
    """One line for the status block: name: role, state, age."""
    try:
        data = load(cfg)
        parts = []
        for key in sorted(data["peers"]):
            v = view(cfg, key, now)
            parts.append(f"{v['name']}: {v['role'] or '?'}, {v['state']}, {_age(v)}")
        return ["roster: " + "; ".join(parts[:6]) + (f" (+{len(parts) - 6})" if len(parts) > 6 else "")] if parts else []
    except OSError, ValueError, KeyError, TypeError:
        return []
