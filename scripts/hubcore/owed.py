"""Tracked requests (the user's plain asks, open until done with evidence) and the `mine` view (everything open, by whose court).

A request is a `work.json` item of kind `request`: the user's quote, the time it was opened, a chase count and the notes
the hub recorded. It closes only with `hubkit req done ID --evidence "<what shows it>"` (or `req drop` with the user's
words) and is archived like any work item, so the time-to-done of past requests becomes the usual time (see `limits`).
Words and limits come from `[requests]` in intents.toml and config.toml.

Intake also reads a question-form ask ("can you delete the old branch?") when its verb is a request verb and not an
information verb; a multi-part ask ("do three things: fix the log, tidy the docs, and reply") or a list of asks becomes one
request with `parts`; and it learns from false alarms (`req drop ID --not-a-request`): see `learned`."""

from __future__ import annotations

import re
import time

from . import lifecycle, status, store
from .orders import Orders, _word_re

KIND = "request"
STOP = {"the", "and", "for", "that", "this", "with", "you", "please", "all", "are", "was", "has", "have", "yet", "done", "from", "into", "not", "its"}


def opts(cfg: dict) -> dict:
    """The `[requests]` limits from config.toml."""
    return cfg["requests"]


def _words_cfg(orders: Orders) -> dict:
    return orders.intents.get("requests", {})


def stems(text: str) -> set[str]:
    """Key words of a text, cut to 5 characters so "branch" matches "branches"."""
    return {w[:5] for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 2 and w not in STOP}


def live(cfg: dict) -> list[dict]:
    """Open requests, oldest first."""
    return sorted((i for i in status.work_load(store.state(cfg)) if i.get("kind") == KIND), key=lambda i: i["added"])


def age_text(seconds: float) -> str:
    m = int(seconds / 60)
    return f"{m}m" if m < 120 else f"{m // 60}h" if m < 2880 else f"{m // 1440}d"


def parts_text(i: dict) -> str:
    """`2/3 parts done (open: 2 tidy the docs; 3 reply)` for a request with parts, else an empty string."""
    parts = i.get("parts") or []
    if not parts:
        return ""
    done = sum(p["state"] == "done" for p in parts)
    dropped = sum(p["state"] == "dropped" for p in parts)
    left = [f"{p['n']} {p['text'][:40]}" for p in parts if p["state"] == "open"]
    return f"{done}/{len(parts)} parts done" + (f", {dropped} dropped" if dropped else "") + (f" (open: {'; '.join(left[:3])}{'; ...' if len(left) > 3 else ''})" if left else "")


def line(i: dict, now: float) -> str:
    """`W3 2h, chased 1x, 2/3 parts done (open: ..): "delete the stale branches"` (the note count when the hub recorded any)."""
    notes = f", {len(i.get('notes', []))} note(s)" if i.get("notes") else ""
    parts = f", {parts_text(i)}" if i.get("parts") else ""
    return f"{i['id']} {age_text(now - i['added'])}, chased {int(i.get('chases', 0))}x{notes}{parts}: \"{i['title'][:100]}\""


# ---- the adaptive limit: the usual time-to-done of past requests, else the configured value ----

def done_minutes(cfg: dict) -> list[float]:
    """Minutes from open to done of every archived request that was finished (dropped ones say nothing about speed)."""
    out: list[float] = []
    for f in sorted((store.state(cfg) / "archive").glob("archive_*.jsonl"))[-90:]:
        for r in store.read_rows(f)[0]:
            if r.get("type") == "work" and r.get("kind") == KIND and r.get("outcome") == "done" and r.get("ts", 0) > r.get("added", 0):
                out.append((r["ts"] - r["added"]) / 60)
    return out


def limits(cfg: dict) -> tuple[float, float, str]:
    """(stall minutes, overdue minutes, basis). With `slow.min_samples` finished requests: the median time-to-done
    (stall) and `overdue_factor` times it (overdue), never under `floor_min`; fewer: the configured values."""
    o = opts(cfg)
    from . import slow

    usual, n = slow.usual(cfg, {KIND: done_minutes(cfg)}, KIND, -1.0)
    if usual < 0:
        return float(o["stall_min"]), float(o["overdue_min"]), f"configured, {n} samples"
    floor = float(o["floor_min"])
    return max(floor, usual), max(floor * 2, usual * float(o["overdue_factor"])), f"median of {n} done requests"


# ---- intake ----

LEARN_FILE = "intake-learn.jsonl"
IMPERATIVE_KEY = "*imperative"
LEAD_FILLER = r"(?:(?:please|pls|also|and|then|now|just)\s+)*"
POLITE = (r"(?i)^\W*(?:(?:hey|ok|okay|also|and|then|please)\W+)*(?:(?:can|could|would|will)\s+(?:you|u)\s+(?:please\s+)?(?:also\s+)?"
          r"|(?:pls|please)\s+(?:also\s+)?)(?P<verb>[\w-]+)")
INFO_VERBS = ["tell", "explain", "show", "say", "describe", "clarify", "confirm", "remind", "think", "see", "check if"]
LIST_MARK = re.compile(r"^\s*(?:\d+[.)]|[-*\u2022])\s+(?=\S)")
CLAUSES = re.compile(r"\s*[,;]\s*(?:and\s+)?|\s+and\s+", re.I)


def _sentences(text: str) -> list[str]:
    """Sentences of a prompt, list markers ("1.", "-") cut off."""
    return [LIST_MARK.sub("", s).strip() for s in re.split(r"(?<=[.!?])\s+|\n", text) if s.strip()]


def _lead_re(verbs: list[str]) -> re.Pattern | None:
    return re.compile(r"(?i)^\W*" + LEAD_FILLER + r"(?P<verb>" + "|".join(re.escape(v) for v in verbs) + r")\b") if verbs else None


def _verbs(orders: Orders) -> list[str]:
    return list(_words_cfg(orders).get("verbs", []))


def _lead_of(orders: Orders, clause: str) -> str:
    """The request verb a clause starts with ('' when none)."""
    lead = _lead_re(_verbs(orders))
    m = lead.search(clause) if lead else None
    return m.group("verb").lower() if m else ""


def _polite(orders: Orders, s: str) -> str:
    """The verb of a question-form or `please X` ask ("can you delete the old branch?") when it is a request verb or an
    `[intake] imperative` word and not an information verb ("can you tell me why", "could you check if"); else ''."""
    cfg = _words_cfg(orders)
    m = re.search(cfg.get("polite_ask", POLITE), s)
    if not m:
        return ""
    verb = m.group("verb").lower()
    rest = s[m.start("verb"):].lower()
    info = cfg.get("info_verbs", INFO_VERBS)
    if any(re.match(re.escape(w.lower()) + r"\b", rest) for w in info):
        return ""
    known = {v.lower() for v in _verbs(orders)} | {w.lower() for w in orders.intents.get("intake", {}).get("imperative", []) if " " not in w}
    return verb if verb in known else ""


def detect_parts(cfg: dict, orders: Orders, text: str) -> list[dict]:
    """The parts of a multi-part ask, or [] when unsure. Conservative: a numbered or bulleted list of at least 2 lines
    that ALL start with a request verb, or after a colon at least 2 comma/semicolon/"and" separated clauses that ALL do;
    more than `[requests] max_parts` also means unsure (one item, no parts)."""
    cap = int(opts(cfg)["max_parts"])
    texts: list[str] = []
    lines = [x for x in text.splitlines() if x.strip()]
    marked = [LIST_MARK.sub("", x).strip() for x in lines if LIST_MARK.match(x)]
    if len(marked) >= 2:
        others = [x for x in lines if not LIST_MARK.match(x)]
        if all(_lead_of(orders, x) for x in marked) and not any(x.rstrip().endswith("?") for x in others):
            texts = marked
    elif ":" in text and "\n" not in text.strip():
        tail = text.split(":", 1)[1].strip().rstrip(".!")
        clauses = [c.strip() for c in CLAUSES.split(tail) if c.strip()]
        if len(clauses) >= 2 and all(_lead_of(orders, c) for c in clauses) and not text.rstrip().endswith("?"):
            texts = clauses
    if len(texts) < 2 or len(texts) > cap:
        return []
    return [{"n": n, "text": t[:120], "state": "open", "evidence": ""} for n, t in enumerate(texts, 1)]


def _read(cfg: dict, orders: Orders, text: str, now: float) -> tuple[str | None, list[dict]]:
    """(the key that opens a request, its parts) or (None, []) for text that is not a request. The key is the lead
    verb, the polite-ask verb, or `*imperative`; a key `learned` has muted is skipped (see `learned`).
    A long discussion (over 300 characters, 900 with a detected list of parts) and an order phrase are not requests."""
    intake = orders.intents.get("intake", {})
    start = intake.get("question_start", r"(?i)^\W*(why|what|how|did|does|do you|is|are|was|were|can|could|would|should|when|where|who|which|has|have)\b")
    if not text.strip() or len(text) > 900:
        return None, []
    muted = set(learned(cfg, now)["muted"])
    parts = detect_parts(cfg, orders, text)
    if len(text) > 300 and not parts:
        return None, []
    wcfg = _words_cfg(orders)
    lead = _lead_re(_verbs(orders))
    imperative = intake.get("imperative", [])
    for s in _sentences(text):
        question = s.rstrip().endswith("?") or bool(re.match(start, s))
        if not question and lead and (m := lead.search(s)) and m.group("verb").lower() not in muted:
            return m.group("verb").lower(), parts
        if verb := _polite(orders, s):
            if verb not in muted:
                return verb, parts
        elif not question and imperative and IMPERATIVE_KEY not in muted and len(s.split()) >= int(wcfg.get("min_words", 4)) and _word_re(imperative).search(s):
            return IMPERATIVE_KEY, parts
    if parts and (key := _lead_of(orders, parts[0]["text"])) and key not in muted:
        return key, parts
    return None, []


def _is_request(cfg: dict, orders: Orders, text: str, now: float | None = None) -> bool:
    return _read(cfg, orders, text, now or time.time())[0] is not None


# ---- intake learning: false alarms mute a verb, a miss is logged ----

def _log(cfg: dict, row: dict, now: float) -> None:
    store.append_row(store.state(cfg) / LEARN_FILE, {**row, "ts": now})


def learned(cfg: dict, now: float) -> dict:
    """What intake learned from the user's corrections, in the last `requests.window_days`. A key (a lead verb, or
    `*imperative` for the imperative-word path) is MUTED when it has at least `false_min` false intakes (`req drop ID
    --not-a-request`) and they are at least `false_ratio` of its opens in the window; a muted verb no longer opens a request
    from its own lead, but a sentence that also holds an `[intake] imperative` word in `min_words` words still opens one (under
    `*imperative`, which can be muted too). `req relearn VERB` restores it: rows before it no longer count.
    Returns {"muted": {key: {...}}, "keys": {key: {"opens", "false"}}, "misses": n}."""
    o = opts(cfg)
    rows = store.read_rows(store.state(cfg) / LEARN_FILE)[0]
    since = now - float(o["window_days"]) * 86400
    reset: dict[str, float] = {}
    for r in rows:
        if r.get("type") == "relearn":
            reset[r["key"]] = max(reset.get(r["key"], 0.0), r["ts"])
    keys: dict[str, dict[str, int]] = {}
    for r in rows:
        k = str(r.get("key"))
        if r.get("type") in ("open", "false") and r["ts"] >= since and r["ts"] > reset.get(k, 0.0):
            keys.setdefault(k, {"opens": 0, "false": 0})["opens" if r["type"] == "open" else "false"] += 1
    muted = {}
    for k, c in keys.items():
        opens = max(c["opens"], c["false"])
        if c["false"] >= int(o["false_min"]) and c["false"] / opens >= float(o["false_ratio"]):
            muted[k] = {**c, "why": f"{c['false']} of {opens} opens in {o['window_days']} days were not requests"}
    return {"muted": muted, "keys": keys, "misses": sum(r.get("type") == "miss" and r["ts"] >= since for r in rows)}


def learned_lines(cfg: dict, now: float) -> list[str]:
    info = learned(cfg, now)
    out = [f"muted: {k} ({m['why']}). NEXT: hubkit req relearn {k}" for k, m in sorted(info["muted"].items())]
    return (out or ["no verb muted"]) + [f"misses logged (req add): {info['misses']}"]


def relearn(cfg: dict, verb: str, now: float | None = None) -> str:
    """Restore a muted verb: the false intakes so far no longer count against it."""
    key = verb.strip().lower() if verb != IMPERATIVE_KEY else verb
    if not key:
        raise ValueError("req relearn needs the verb")
    now = now or time.time()
    if key not in learned(cfg, now)["muted"]:
        raise KeyError(f"{key} is not muted")
    _log(cfg, {"type": "relearn", "key": key}, now)
    return f"{key} opens requests again"


def _match(text: str, items: list[dict], similarity: float, chase: bool = False) -> dict | None:
    """The open request a short chase or a repeated ask is about: the best overlap of key words (stems)."""
    mine = stems(text)
    if not mine:
        return None
    best, score = None, 0.0
    for i in items:
        theirs = stems(i["title"])
        shared = len(mine & theirs)
        if shared >= min(2, len(mine)) and shared / len(mine | theirs) >= similarity and shared / len(mine | theirs) > score:
            best, score = i, shared / len(mine | theirs)
    if best is None and chase:  # a chase has fewer words than the ask: count the share of the chase's own words
        for i in items:
            shared = len(mine & stems(i["title"]))
            if shared >= min(2, len(mine)) and shared / len(mine) >= 0.5 and shared / len(mine) > score:
                best, score = i, shared / len(mine)
    return best


def _is_chase(orders: Orders, text: str) -> bool:
    intake = orders.intents.get("intake", {})
    pat = _words_cfg(orders).get("chase") or intake.get("chase")
    return bool(pat) and len(text) <= int(intake.get("verb_max_chars", 120)) and bool(re.search(pat, text))


def _part_of(item: dict, text: str) -> dict | None:
    """The open part of a request whose words a chase repeats (at least half of the chase's key words), else None."""
    mine = stems(text)
    best, score = None, 0.0
    for p in item.get("parts") or []:
        shared = len(mine & stems(p["text"]))
        if p["state"] == "open" and mine and shared >= min(2, len(mine)) and shared / len(mine) >= 0.5 and shared / len(mine) > score:
            best, score = p, shared / len(mine)
    return best


def intake(cfg: dict, orders: Orders, text: str, opened_order: bool, now: float) -> list[str]:
    """One typed prompt: a chase on an open request counts and prints its line (and the part it names); a new ask (a
    statement, a polite question such as "can you delete the old branch?", a list of asks) opens a request, not when it
    repeats an open one, which counts as a chase. Returns lines for the model."""
    items = live(cfg)
    sim = float(opts(cfg)["similarity"])
    if _is_chase(orders, text) and items and (hit := _match(text, items, sim, chase=True)):
        return [chase(cfg, hit["id"], now, _part_of(hit, text))]
    if opened_order:
        return []
    key, parts = _read(cfg, orders, text, now)
    if key is None:
        return []
    if hit := _match(text, items, sim) if len(stems(text)) >= 2 else None:
        return [chase(cfg, hit["id"], now, _part_of(hit, text))]
    if len(items) >= int(opts(cfg)["max_open"]):
        return []
    item = add(cfg, text, now, parts=parts, lead=key)
    _log(cfg, {"type": "open", "key": key, "id": item["id"]}, now)
    stall, overdue, basis = limits(cfg)
    pl = f" in {len(parts)} parts (close each: hubkit req done {item['id']} --part N --evidence \"..\")" if parts else ""
    return [f"hubkit: tracking request {item['id']}: \"{item['title'][:80]}\"{pl}. It stays open until: hubkit req done {item['id']} --evidence \"<what shows it>\" "
            f"(overdue after {overdue:g} min, {basis}); progress: hubkit req note {item['id']} --note \"..\"; not a request: hubkit req drop {item['id']} --not-a-request"]


def add(cfg: dict, text: str, now: float | None = None, parts: list[dict] | None = None, lead: str = "") -> dict:
    """Open one request with the user's words: the 120-character title, the quote, the full text (`text_chars`) and its parts."""
    from .redact import redact_obj

    sd = store.state(cfg)
    quote = " ".join(text.split())
    with store.locked(sd / "work.lock"):
        items = status.work_load(sd)
        item = redact_obj({"id": status.next_work_id(sd, items), "kind": KIND, "title": quote[:int(opts(cfg)["title_chars"])],
                           "quote": quote[:300], "text": quote[:int(opts(cfg)["text_chars"])], "owner": "", "note": "", "added": now or time.time(),
                           "chases": 0, "notes": [], "lead": lead, **({"parts": parts} if parts else {})})
        items.append(item)
        status.work_save(sd, items)
    return item


def add_by_hand(cfg: dict, orders: Orders, text: str, now: float | None = None) -> str:
    """`req add`: open a request the intake missed and log the miss (shown in eod)."""
    if not text.strip():
        raise ValueError("req add needs the user's words")
    now = now or time.time()
    item = add(cfg, text, now, parts=detect_parts(cfg, orders, text))
    _log(cfg, {"type": "miss", "text": " ".join(text.split())[:100], "id": item["id"]}, now)
    return f"{item['id']} opened by hand (logged as an intake miss): \"{item['title'][:80]}\""


def show_lines(i: dict, now: float) -> list[str]:
    """The full record of one request, for `req show`."""
    out = [f"{i['id']} request, open {age_text(now - i['added'])}, chased {int(i.get('chases', 0))}x" + (f", opened by: {i['lead']}" if i.get("lead") else ""),
           f"title: {i['title']}", f"text: {i.get('text') or i.get('quote', '')}"]
    out += [f"part {p['n']} [{p['state']}]: {p['text']}" + (f" -- {p['evidence']}" if p.get("evidence") else "") for p in i.get("parts", [])]
    out += [f"note {n + 1} ({age_text(now - x['t'])} ago): {x['text']}" for n, x in enumerate(i.get("notes", []))]
    return out


def show(cfg: dict, rid: str) -> dict:
    return _find(status.work_load(store.state(cfg)), rid)


def _find(items: list[dict], rid: str) -> dict:
    hit = next((i for i in items if i["id"] == rid and i.get("kind") == KIND), None)
    if hit is None:
        raise KeyError(f"no open request {rid}")
    return hit


def chase(cfg: dict, rid: str, now: float, part: dict | None = None) -> str:
    """Count a chase on a request and return its line (the hub answers from the record)."""
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        items = status.work_load(sd)
        hit = _find(items, rid)
        lifecycle.check("request", lifecycle.state_of("request", hit), "chase")
        hit["chases"] = int(hit.get("chases", 0)) + 1
        hit["last_chase"] = now
        status.work_save(sd, items)
    last = hit["notes"][-1]["text"][:100] if hit.get("notes") else "no note recorded yet"
    on = f" It is about part {part['n']}: {part['text'][:80]}." if part else ""
    return f"hubkit: the user is chasing request {line(hit, now)}.{on} Last note: {last}. NEXT: answer from this record, then do it or: hubkit req done {rid} --evidence \"..\""


def note(cfg: dict, rid: str, text: str, now: float | None = None) -> str:
    """Record progress on a request."""
    if not text.strip():
        raise ValueError("req note needs --note")
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        items = status.work_load(sd)
        hit = _find(items, rid)
        lifecycle.check("request", lifecycle.state_of("request", hit), "note", note=text)
        hit["notes"] = [*hit.get("notes", []), {"t": now or time.time(), "text": store.safe(text, 300)}][-20:]
        status.work_save(sd, items)
    return f"{rid} noted"


def _archive(cfg: dict, rid: str, outcome: str, detail: str, now: float, extra: dict | None = None) -> str:
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        items = status.work_load(sd)
        hit = _find(items, rid)
        lifecycle.check("request", lifecycle.state_of("request", hit), "done" if outcome == "done" else "drop", evidence=detail, quote=detail)
        day = store.day_of(cfg, now)
        store.append_row(sd / "archive" / f"archive_{day}.jsonl", {"type": "work", **hit, "outcome": outcome, "done_note": detail, "ts": now, **(extra or {})})
        status.work_save(sd, [i for i in items if i["id"] != rid])
    return f"{rid} {outcome}, archived to archive_{day}"


def _open_parts(hit: dict) -> list[dict]:
    return [p for p in hit.get("parts") or [] if p["state"] == "open"]


def _close_part(cfg: dict, rid: str, part: int, state: str, proof: str, now: float) -> str:
    """Close one part (done with evidence, dropped with the user's words) and note it; the last open part closes the request:
    done (the parts' evidence joined) when any part was done, else dropped (the quotes joined)."""
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        items = status.work_load(sd)
        hit = _find(items, rid)
        p = next((x for x in hit.get("parts") or [] if x["n"] == part), None)
        if p is None:
            raise KeyError(f"request {rid} has no part {part}")
        if p["state"] != "open":
            raise store.Refused(f"part {part} of {rid} is already {p['state']}. NEXT: hubkit req show {rid}")
        text = f"part {part} {'done' if state == 'done' else 'dropped'}: {store.safe(proof, 200)}"
        lifecycle.check("request", lifecycle.state_of("request", hit), "note", note=text)
        p["state"], p["evidence"] = state, store.safe(proof, 200)
        hit["notes"] = [*hit.get("notes", []), {"t": now, "text": text}][-20:]
        status.work_save(sd, items)
    if _open_parts(hit):
        return f"{rid} part {part} {state}; {parts_text(hit)}"
    ps = hit["parts"]
    if any(x["state"] == "done" for x in ps):
        return _archive(cfg, rid, "done", store.safe("; ".join(f"part {x['n']}: {x['evidence']}" for x in ps if x["state"] == "done"), 300), now)
    return _archive(cfg, rid, "dropped", store.safe("; ".join(f"part {x['n']}: {x['evidence']}" for x in ps), 300), now)


def done(cfg: dict, rid: str, evidence: str, now: float | None = None, part: int | None = None) -> str:
    """Close a request (or one part with `part`); the evidence (what shows it is done) is required. A request with open
    parts is refused (exit 3) with the open parts named: close them one by one, the last one closes the request."""
    if len(evidence.strip()) < 8:
        raise ValueError("req done needs --evidence: what shows it is done (a sha, a URL, a command and its result)")
    now = now or time.time()
    if part is not None:
        return _close_part(cfg, rid, part, "done", evidence, now)
    left = _open_parts(show(cfg, rid))
    if left:
        raise store.Refused(f"{rid} has {len(left)} open part(s): " + "; ".join(f"{p['n']} {p['text'][:50]}" for p in left[:5])
                            + f". NEXT: hubkit req done {rid} --part {left[0]['n']} --evidence \"<what shows it>\"")
    return _archive(cfg, rid, "done", store.safe(evidence, 300), now)


def drop(cfg: dict, rid: str, quote: str, now: float | None = None, part: int | None = None, not_a_request: bool = False) -> str:
    """Drop a request (or one part); only the user's own words may (a cancel by the hub is not a drop). `not_a_request`
    closes it as a false intake without a quote and teaches intake (see `learned`)."""
    now = now or time.time()
    if not_a_request:
        hit = show(cfg, rid)
        msg = _archive(cfg, rid, "dropped", "not a request (false intake)", now, {"false_intake": True})
        if hit.get("lead"):
            _log(cfg, {"type": "false", "key": hit["lead"], "id": rid, "text": hit["title"][:100]}, now)
        return msg
    if not quote.strip():
        raise ValueError("req drop needs --quote: the user's words that cancel it")
    if part is not None:
        return _close_part(cfg, rid, part, "dropped", quote, now)
    return _archive(cfg, rid, "dropped", store.safe(quote, 300), now)


# ---- block, gate, watchdog, metrics ----

def block_lines(items: list[dict], now: float, cap: int) -> list[str]:
    """Open requests oldest first for the status block, `cap` at most."""
    mine = sorted((i for i in items if i.get("kind") == KIND), key=lambda i: i["added"])
    if not mine:
        return []
    out = [f"open requests: {len(mine)} (hubkit req ls)"] + ["  " + line(i, now) for i in mine[:cap]]
    return out + ([f"  ... {len(mine) - cap} more"] if len(mine) > cap else [])


def _quiet_since(i: dict) -> float:
    return max([i["added"]] + [n["t"] for n in i.get("notes", [])])


def gate_lines(cfg: dict, last: str, now: float) -> list[str]:
    """A final message that claims done while a request older than the stall limit has no note within it: one reason."""
    from . import lessons

    if not lessons.claims_done(last):
        return []
    stall = limits(cfg)[0]
    old = [i for i in live(cfg) if (now - i["added"]) / 60 > stall and (now - _quiet_since(i)) / 60 > stall]
    if not old:
        return []
    return [f"hubkit: you say it is done but {len(old)} request(s) of the user are open past {stall:g} min with no note: "
            + "; ".join(line(i, now) for i in old[:3])
            + f". NEXT: for each, finish it and run hubkit req done {old[0]['id']} --evidence \"<what shows it>\", or record real progress: hubkit req note {old[0]['id']} --note \"..\""]


def alerts(cfg: dict, now: float) -> list[dict]:
    """One alert per request past its overdue limit (tick applies the back-off)."""
    overdue = limits(cfg)[1]
    return [{"key": f"{i['id']}:request_overdue",
             "msg": f"{i['id']} request \"{i['title'][:50]}\" open {age_text(now - i['added'])}, past {overdue:g} min. NEXT: do it and hubkit req done {i['id']} --evidence \"..\", or say what blocks it: hubkit req note {i['id']} --note \"..\""}
            for i in live(cfg) if (now - i["added"]) / 60 > overdue]


def metrics(cfg: dict, day: str) -> dict:
    """Requests opened on `day` (live and archived), how many finished, and the chases on them."""
    sd = store.state(cfg)
    rows = [r for r in status.work_load(sd) if r.get("kind") == KIND]
    for f in sorted((sd / "archive").glob("archive_*.jsonl"))[-30:]:
        rows += [r for r in store.read_rows(f)[0] if r.get("type") == "work" and r.get("kind") == KIND]
    mine = [r for r in rows if store.day_of(cfg, r["added"]) == day]
    chases = sum(int(r.get("chases", 0)) for r in mine)
    return {"requests": len(mine), "done": sum(r.get("outcome") == "done" for r in mine), "open": sum("outcome" not in r for r in mine),
            "chases": chases, "chases_per_request": round(chases / len(mine), 2) if mine else 0.0,
            "false_intakes": sum(bool(r.get("false_intake")) for r in mine), "misses": _misses(cfg, day)}


def _misses(cfg: dict, day: str) -> int:
    """Requests opened by hand (`req add`) on `day`: asks the intake missed."""
    rows = store.read_rows(store.state(cfg) / LEARN_FILE)[0]
    return sum(r.get("type") == "miss" and store.day_of(cfg, r["ts"]) == day for r in rows)


def glance_line(cfg: dict, day: str) -> str:
    m = metrics(cfg, day)
    return (f"requests opened {m['requests']}, done {m['done']}, still open {m['open']}; "
            f"chases {m['chases']} ({m['chases_per_request']} per request); intake misses {m['misses']}, false alarms {m['false_intakes']}")


# ---- `hubkit mine`: everything open, by whose court it is in ----

def _row(i: dict, now: float, started: float, title: str, link: str = "") -> str:
    return f"  {i['id']} {age_text(now - started)}: {title[:90]}" + (f" {link}" if link else "")


def mine(cfg: dict, now: float | None = None) -> list[str]:
    """Read-only list of every open thing, grouped by who must move next."""
    now = now or time.time()
    items = status.work_load(store.state(cfg))
    orders = Orders(cfg)
    first_pr = lambda i: (i.get("prs") or [""])[0]  # noqa: E731
    user = [_row(i, now, i["added"], i["title"] + (f" (blocks: {i['note']})" if i.get("note") else "")) for i in items if i.get("kind") == "ask"]
    user += [f"  {o['id']} {age_text(now - o['opened'])}: confirm order ({o['kind']}): {o['quote'][:80]}" for o in orders.live() if o["state"] == "unconfirmed"]
    st = status.read_status(cfg) or {}
    if (st.get("mep") or {}).get("state") in ("ok", "stale"):
        user += [f"  (mep) {t[:90]}" for t in st["mep"].get("titles", [])]
    hub = [_row(i, now, i["added"], i["title"], "") for i in sorted(items, key=lambda i: i["added"]) if i.get("kind") == KIND]
    hub += [f"  {o['id']} {age_text(now - o['opened'])}: order {o['kind']} ({o['state']}): {o['quote'][:80]}" for o in orders.live() if o["state"] in ("open", "held")]
    hub += [_row(i, now, i["added"], f"[{i['kind']}] {i['title']}") for i in items if i.get("kind") in ("active", "priority")]
    peers = [_row(i, now, i.get("routed_at") or i["added"], f"{i.get('owner', '?')}: {i['title']}", first_pr(i)) for i in items if i.get("kind") == "routed"]
    others = [_row(i, now, i["added"], f"{i.get('owner', '?')} asked: {i['title']}", first_pr(i)) for i in items if i.get("kind") == "reply"]
    others += [_row(i, now, i["added"], f"waiting: {i['title']}", first_pr(i)) for i in items if i.get("kind") == "tracked"]
    from . import sweeps, threads

    user, hub, peers, others = threads.fold_rows(cfg, now, user, hub, peers, others)  # one entry per thread
    user, hub, peers, others = sweeps.mine_rows(cfg, items, now, user, hub, peers, others)
    out: list[str] = []
    for head, rows in (("ON THE USER", user), ("ON THE HUB", hub), ("ON PEERS AND AGENTS", peers), ("ON OTHER PEOPLE", others)):
        out += [f"{head} ({sum(not r.startswith('    ') for r in rows)})", *(rows or ["  nothing"])]  # member lines of a thread are indented
    return out
