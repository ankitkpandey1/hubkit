"""Orders: urgent asks that get a record and a clock. Close needs evidence."""

from __future__ import annotations

import glob
import hashlib
import os
import re
import time
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

from . import config, store

PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
PR_REF = re.compile(r"(?i)https://github\.com/[\w.-]+/[\w.-]+/pull/\d+|\bpr\s*#?\d+|#\d{2,}")
LIVE_STATES = ("open", "unconfirmed", "held")


def load_intents(cfg: dict) -> dict:
    """Load the intents.toml in effect."""
    return tomllib.loads(config.intents_path(cfg).read_text(encoding="utf-8"))


def _word_re(words: list[str]) -> re.Pattern:
    alts = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![\w]){alts}(?![\w])", re.I)


def match_kind(intents: dict, text: str) -> str | None:
    """The first configured kind whose words appear in `text`, else None."""
    for kind, spec in intents.get("kinds", {}).items():
        if _word_re(spec["words"]).search(text):
            return kind
    return None


def parse_deadline(text: str, now: float, cfg: dict) -> float | None:
    """Epoch deadline from `in N min` or `by HH:MM` in the text, else None."""
    m = re.search(r"\b(?:in|within)\s+(\d{1,3})\s*min", text, re.I)
    if m:
        return now + int(m[1]) * 60
    m = re.search(r"\bby\s+(\d{1,2}):(\d{2})\b", text, re.I)
    if m:
        base = datetime.fromtimestamp(now, store.tzinfo(cfg))
        due = base.replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0)
        if due.timestamp() < now:
            due += timedelta(days=1)
        return due.timestamp()
    return None


def _fold_block(o: dict, r: dict) -> None:
    """A block row opens a blocker, or (same `on`) adds a fix to the open one. A new fix restarts the fix clock."""
    cur = o["blocker"]
    if cur is None or cur["on"] != r["on"]:
        cur = o["blocker"] = {"on": r["on"], "since": r["ts"], "fix": "", "fix_at": None, "holder": ""}
    if r.get("holder"):
        cur["holder"] = r["holder"]
    if r.get("fix"):
        cur["fix"], cur["fix_at"] = r["fix"], r["ts"]


def fold(rows: list[dict]) -> dict[str, dict]:
    """Fold order rows (file order) into one dict per order id. First close wins."""
    out: dict[str, dict] = {}
    for r in rows:
        oid = r.get("id")
        op = r.get("op")
        if not oid or not op:
            continue
        if op == "open":
            out.setdefault(
                oid,
                {
                    "id": oid, "kind": r["kind"], "state": r.get("state", "open"),
                    "quote": r.get("quote", ""), "refs": r.get("refs", []),
                    "opened": r["ts"], "deadline": r.get("deadline"), "moves": [],
                    "chases": 0, "chase_quotes": [], "evidence": {}, "held_quote": "",
                    "estimates": [], "estimate_min": None,
                    "blocker": None, "results": [], "stoploss": None,
                },
            )
            continue
        o = out.get(oid)
        if o is None or o["state"] in ("closed", "cancelled", "expired"):
            continue
        if op == "confirm" and o["state"] == "unconfirmed":
            o["state"] = "open"
        elif op == "move":
            o["moves"].append((r["ts"], r.get("note", "")))
        elif op == "block":
            _fold_block(o, r)
        elif op == "result":
            o["results"].append((r["ts"], r.get("note", "")))
            o["blocker"] = None  # only a recorded result clears a blocker
        elif op == "stoploss":
            o["stoploss"] = r["ts"]
        elif op == "estimate":
            o["estimates"].append({"t": r["ts"], "min": r["minutes"], "source": r.get("source", "hub"), "why": r.get("why", "")})
            o["estimate_min"] = r["minutes"]
        elif op == "ref":
            o["refs"] = list(o["refs"]) + list(r.get("refs", []))
        elif op == "chase":
            o["last_chase"] = r["ts"]
            o["chases"] += 1
            o["chase_quotes"].append(r.get("quote", ""))
        elif op == "hold":
            o["state"], o["held_quote"] = "held", r.get("quote", "")
        elif op == "resume" and o["state"] == "held":
            o["state"] = "open"
        elif op == "cancel":
            o["state"], o["closed_at"], o["close_quote"] = "cancelled", r["ts"], r.get("quote", "")
        elif op == "expire":
            o["state"], o["closed_at"] = "expired", r["ts"]
        elif op == "close":
            o["state"], o["closed_at"], o["evidence"] = "closed", r["ts"], r.get("evidence", {})
    return out


class Orders:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.sd = store.state(cfg)
        self.path = self.sd / "orders.jsonl"
        self.intents = load_intents(cfg)

    def all(self) -> dict[str, dict]:
        """Every order, folded from the ledger rows."""
        return fold(store.read_rows(self.path)[0])

    def live(self) -> list[dict]:
        """Orders that are open, unconfirmed or held."""
        return [o for o in self.all().values() if o["state"] in LIVE_STATES]

    def _append(self, oid: str, op: str, **kw) -> None:
        store.append_row(self.path, {"id": oid, "op": op, **kw})

    def open(self, kind: str, quote: str, state: str = "open", now: float | None = None) -> dict:
        """Open a new order of `kind` quoting the user's words; returns the folded order."""
        now = now or time.time()
        quote = store.safe(quote, 300)  # redact first, then cut
        with store.locked(self.sd / "lock"):  # blocking: two hooks cannot both open the same order
            for o in self.all().values():
                if o["kind"] == kind and o["state"] in LIVE_STATES and o["quote"] == quote:
                    return o
            qhash = hashlib.sha1(f"{kind}|{quote}|{now}".encode()).hexdigest()
            oid = f"O-{store.day_of(self.cfg, now).replace('-', '')[2:]}-{qhash[:4]}"
            self._append(
                oid, "open", kind=kind, quote=quote, state=state, ts=now,
                refs=PR_URL.findall(quote), deadline=parse_deadline(quote, now, self.cfg),
            )
        return self.all()[oid]

    def get(self, oid: str) -> dict:
        """One order by id; raises KeyError when unknown."""
        found = self.all().get(oid)
        if found is None:
            raise KeyError(f"no order {oid}")
        return found

    def confirm(self, oid: str) -> None:
        """Turn an unconfirmed order into an open one."""
        self._append(oid, "confirm")

    def move(self, oid: str, note: str, estimate: int | None = None) -> str:
        """Record real movement on an order. The first action also records its estimate (the usual time unless
        given); returns a line saying so, or an empty string."""
        first = self.get(oid)["estimate_min"] is None
        self._append(oid, "move", note=note)
        if estimate:
            self.estimate(oid, estimate, "brief")
        elif first:
            from . import slow

            mins, basis = slow.order_default(self.cfg, self, self.get(oid))
            self.estimate(oid, mins, "default")
            return f"{oid} estimate defaulted to {mins:g}m ({basis}); set it: hubkit work estimate {oid} --minutes N --why \"..\""
        return ""

    def block(self, oid: str, on: str, fix: str = "", holder: str = "") -> str:
        """Record what blocks an order and the fix that is running for it. A blocker without a fix, or whose fix has
        run past `blocker_fix_min` with no result, alerts and holds the Stop gate."""
        if len(on.strip()) < 3:
            raise ValueError("block needs --on <what is blocked>")
        self.get(oid)
        self._append(oid, "block", on=store.safe(on, 200), fix=store.safe(fix, 200), holder=store.safe(holder, 80))
        return self.blocker_line(self.get(oid)) or ""

    def result(self, oid: str, note: str) -> None:
        """Record the result of an action on an order: movement, clears its blocker and any stop-loss."""
        if len(note.strip()) < 3:
            raise ValueError("result needs --note <what was done and what came of it>")
        self.get(oid)
        self._append(oid, "result", note=store.safe(note, 300))

    def stoploss(self, oid: str) -> None:
        """Record that a STOP-LOSS line was printed for this order."""
        self._append(oid, "stoploss")

    def fix_limit_min(self) -> float:
        return float(self.intents.get("intake", {}).get("blocker_fix_min", 3))

    def blocker_problem(self, o: dict, now: float) -> str | None:
        """Why the open blocker of an order is not being solved, or None when a fresh fix is running."""
        b = o.get("blocker")
        if not b or o["state"] != "open":
            return None
        if not b["fix"]:
            return f"{o['id']} is blocked on {b['on']} with no fix running"
        age = (now - b["fix_at"]) / 60
        if age > self.fix_limit_min():
            return f"{o['id']} is blocked on {b['on']}; its fix has run {int(age)} min with no result"
        return None

    def blocker_line(self, o: dict, now: float | None = None) -> str | None:
        """`BLOCKED on X for 4m; fix: Y (2m, holder H)` for the status line, or None."""
        b = o.get("blocker")
        if not b:
            return None
        now = now or time.time()
        fix = f"fix: {b['fix']} ({int((now - b['fix_at']) / 60)}m{', ' + b['holder'] if b['holder'] else ''})" if b["fix"] else "NO FIX RUNNING"
        return f"BLOCKED on {b['on']} for {int((now - b['since']) / 60)}m; {fix}"

    def stoploss_pending(self, o: dict) -> bool:
        """A STOP-LOSS line was printed and no result has been recorded since."""
        return bool(o.get("stoploss")) and not any(t > o["stoploss"] for t, _ in o["results"])

    def estimate(self, oid: str, minutes: float, source: str, why: str = "") -> None:
        """Record an estimate beside any earlier one; never replaces it."""
        self._append(oid, "estimate", minutes=minutes, source=source, why=store.safe(why, 200))

    def add_ref(self, oid: str, refs: list[str]) -> None:
        """Attach PR or ticket references to an order."""
        self._append(oid, "ref", refs=refs)

    def recent_chase(self, o: dict, now: float, window: float = 60.0) -> bool:
        """True when the order was chased within `window` seconds."""
        return bool(o.get("last_chase")) and now - o["last_chase"] < window

    def chase(self, oid: str, quote: str) -> None:
        """Record that the user asked for status on an order."""
        self._append(oid, "chase", quote=store.safe(quote, 300))

    def hold(self, oid: str, quote: str) -> None:
        """Hold an order, quoting the user's explicit words."""
        self._append(oid, "hold", quote=quote)

    def resume(self, oid: str) -> None:
        """Resume a held order and restart its clock."""
        self._append(oid, "resume")

    def cancel(self, oid: str, quote: str) -> None:
        """Cancel an order, quoting the user's words, and archive it."""
        if not quote.strip():
            raise ValueError("cancel needs the user's words (--quote)")
        with store.locked(self.sd / "lock"):
            o = self.get(oid)
            if o["state"] == "closed":
                raise ValueError(f"{oid} is already closed")
            if o["state"] in ("cancelled", "expired"):
                return
            self._append(oid, "cancel", quote=quote)  # the order row first; the archive row is repairable
            self._die_after("cancel")
            self._archive(self.get(oid), "cancelled", {"quote": store.safe(quote, 300)})

    def expire(self, oid: str, why: str) -> None:
        """Expire an order (for example an unconfirmed one past its TTL) and archive it."""
        with store.locked(self.sd / "lock"):
            o = self.get(oid)
            if o["state"] in LIVE_STATES:
                self._append(oid, "expire", note=why)
                self._archive(self.get(oid), "expired", {"note": why})

    @staticmethod
    def _die_after(point: str) -> None:
        if os.environ.get("HUBKIT_DIE_AFTER") == point:  # test-only fault point
            os._exit(9)

    def _archive(self, o: dict, outcome: str, extra: dict) -> None:
        now = time.time()
        row = {
            "type": "order", "id": o["id"], "kind": o["kind"], "outcome": outcome,
            "quote": o["quote"], "opened": o["opened"], "chases": o["chases"],
            "moves": len(o["moves"]), "estimates": o.get("estimates", []), "ts": now, **extra,
        }
        day = store.day_of(self.cfg, now)
        store.append_row(self.sd / "archive" / f"archive_{day}.jsonl", row)

    def check_evidence(self, o: dict, evidence: dict[str, str]) -> list[str]:
        """Return a list of problems; empty means the evidence is acceptable."""
        spec = self.intents["kinds"].get(o["kind"], {})
        problems = []
        for need in spec.get("done_when", []):
            val = evidence.get(need, "").strip()
            if len(val) < 3:
                problems.append(f"missing evidence '{need}'")
            elif val.startswith("file:"):
                p = Path(val[5:]).expanduser()
                if not p.exists():
                    problems.append(f"'{need}': {p} does not exist")
                elif p.stat().st_mtime < o["opened"]:
                    problems.append(f"'{need}': {p} is older than the order")
        for need in spec.get("sha_evidence", []):  # a merge is proven by the merge commit, not by a word
            if need in evidence and not re.search(r"\b[0-9a-f]{7,40}\b", evidence[need]):
                problems.append(f"'{need}' must name the merge commit sha (7+ hex characters)")
        problems += self._uncovered_prs(o, evidence)
        return problems

    @staticmethod
    def _uncovered_prs(o: dict, evidence: dict[str, str]) -> list[str]:
        """Every PR the order names (its quote and refs) must appear in the evidence, by number."""
        named = dict.fromkeys(re.findall(r"\d+", " ".join(PR_REF.findall(" ".join([o["quote"], *o["refs"]])))))
        given = set(re.findall(r"\b\d{2,}\b", " ".join(evidence.values())))
        missing = [n for n in named if n not in given]
        return [f"evidence does not cover {', '.join('#' + n for n in missing)} (every PR named in the order needs evidence)"] if missing else []

    def close(self, oid: str, evidence: dict[str, str]) -> str:
        """Close an order once every `done_when` evidence name is given; returns a one-line result."""
        with store.locked(self.sd / "lock") as _got:
            o = self.get(oid)
            if o["state"] == "closed":
                return f"{oid} already closed"
            if o["state"] in ("cancelled", "expired"):
                raise ValueError(f"{oid} was {o['state']}")
            if o["state"] == "unconfirmed":
                raise ValueError(f"{oid} is unconfirmed; run 'order confirm {oid}' first")
            problems = self.check_evidence(o, evidence)
            if problems:
                raise ValueError("close refused: " + "; ".join(problems))
            self._append(oid, "close", evidence=evidence)  # source of truth first
            self._die_after("close")
            self._archive(self.get(oid), "closed", {"evidence": evidence})
        return f"{oid} closed"

    def repair_archive(self) -> int:
        """Archive closed or cancelled orders whose archive row is missing (crash repair)."""
        archived = set()
        for f in (self.sd / "archive").glob("archive_*.jsonl"):
            archived |= {r.get("id") for r in store.read_rows(f)[0] if r.get("type") == "order"}
        fixed = 0
        for o in self.all().values():
            if o["state"] in ("closed", "cancelled", "expired") and o["id"] not in archived:
                self._archive(o, o["state"], {"evidence": o["evidence"], "repaired": True})
                fixed += 1
        return fixed

    def last_movement(self, o: dict) -> float:
        """Epoch of the latest movement: a `move`, the open time, or a touched movement file."""
        times = [o["opened"], *[t for t, _ in o["results"]]]
        if o.get("blocker"):  # notes and touched files are not progress on a blocked order; only a result is
            return max(times + [t for t, _ in o["moves"] if t <= o["blocker"]["since"]])
        times += [t for t, _ in o["moves"]]
        spec = self.intents["kinds"].get(o["kind"], {})
        for pat in spec.get("movement_files", []):
            for f in glob.glob(os.path.expanduser(pat)):
                m = os.path.getmtime(f)
                if m >= o["opened"]:
                    times.append(m)
        return max(times)

    def status_line(self, o: dict, now: float | None = None) -> str:
        """One line: id, kind, state, ages, chases and what done still needs."""
        now = now or time.time()
        spec = self.intents["kinds"].get(o["kind"], {})
        age = int((now - o["opened"]) / 60)
        idle = int((now - self.last_movement(o)) / 60)
        ddl = f" deadline {store.clock(self.cfg, o['deadline'])}" if o.get("deadline") else ""
        need = ",".join(spec.get("done_when", [])) or "-"
        blocked = f" | {self.blocker_line(o, now)}" if o.get("blocker") else ""
        return (
            f"{o['id']} {o['kind']} [{o['state']}] open {age}m, idle {idle}m, "
            f"chases {o['chases']}{ddl}; done needs: {need}{blocked}"
        )

    def metrics(self, day: str) -> dict:
        """Chases per order for orders opened on `day` (local day)."""
        mine = [o for o in self.all().values() if store.day_of(self.cfg, o["opened"]) == day]
        chases = sum(o["chases"] for o in mine)
        unordered = sum(1 for r in store.read_rows(self.sd / "unordered-chases.jsonl")[0] if store.day_of(self.cfg, r["ts"]) == day)
        return {
            "orders": len(mine), "chases": chases,
            "chases_per_order": round(chases / len(mine), 2) if mine else 0.0,
            "unordered_chases": unordered,
        }


PLACE = re.compile(r"(?i)\b\w+\s+(?:server|box|host|machine|env|environment)s?\b")


def _without_places(text: str) -> str:
    """"release server" names a machine; an order word next to a machine noun is a mention, not an order."""
    return PLACE.sub(" ", text)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9#]{5,}", text.lower())}


def chase_relates(o: dict, text: str, chase_re: str, kind: str | None) -> bool:
    """A chase counts on an order when it is bare ("eta?", "what is the hold up?") or names that order: its kind, a PR it holds, or words of its quote."""
    if kind == o["kind"]:
        return True
    rest = _words(re.sub(chase_re, " ", text))
    if not rest:
        return True
    return bool(rest & _words(" ".join([o["quote"], *o["refs"]])))


def hook_intake(orders: Orders, text: str, now: float | None = None) -> list[str]:
    """Apply one human prompt to the order book. Returns lines for the model."""
    now = now or time.time()
    intake = orders.intents.get("intake", {})
    chase_re = intake.get("chase")
    # "what is the hold up?" is a chase, not a hold: drop chase phrases before looking for verbs
    verbs_text = re.sub(chase_re, " ", text) if chase_re else text

    def has(key: str, in_text: str | None = None) -> bool:
        words = intake.get(key, [])
        return bool(words) and bool(_word_re(words).search(in_text if in_text is not None else verbs_text))

    lines: list[str] = []
    short = len(text) <= int(intake.get("verb_max_chars", 120))
    kind_text = _without_places(text)
    for phrase in intake.get("not_order_phrases", []):  # "release notes" is not a release order
        kind_text = re.sub(re.escape(phrase), " ", kind_text, flags=re.I)
    kind = match_kind(orders.intents, kind_text)
    live = orders.live()
    start = intake.get("question_start", r"(?i)^\W*(why|what|how|did|does|do you|is|are|was|were|can|could|would|should|when|where|who|which|has|have)\b")
    statements = [s for s in re.split(r"(?<=[.!?])\s+|\n", text) if s.strip() and not (s.rstrip().endswith("?") or re.match(start, s))]
    act = _without_places(" ".join(statements))  # only a statement can order; a question never does, urgent word or not
    for phrase in intake.get("not_order_phrases", []):
        act = re.sub(re.escape(phrase), " ", act, flags=re.I)
    act_kind = match_kind(orders.intents, act)
    urgent = has("urgent_words", text)  # "don't wait, merge it now" keeps or restarts the clock
    hold = has("hold_words") and not urgent
    cancel, resume = has("cancel_words"), has("resume_words")
    held = [o for o in live if o["state"] == "held" and kind in (None, o["kind"])]
    if (hold or cancel or resume) and short:
        targets = _verb_targets(orders, text, kind, live, intake)
        for o in targets:
            if cancel:
                orders.cancel(o["id"], text)
                lines.append(f"hubkit: {o['id']} cancelled on the user's words")
            elif hold and o["state"] != "held":
                orders.hold(o["id"], text)
                lines.append(f"hubkit: {o['id']} held; clock stopped")
            elif resume and o["state"] == "held":
                orders.resume(o["id"])
                lines.append(f"hubkit: {o['id']} resumed")
        if targets:
            return lines
    if urgent and kind and held:  # an urgent order word on a held order restarts its clock
        for o in held:
            orders.resume(o["id"])
            lines.append(f"hubkit: {o['id']} resumed (urgent words); clock running")
        live = orders.live()
    opened_id = None
    same = [o for o in live if o["kind"] == kind]
    if act_kind and not any(o["kind"] == act_kind for o in live) and not (hold or cancel) and has("imperative", act):
        kind = act_kind
        o = orders.open(kind, text, state="open", now=now)
        opened_id = o["id"]
        spec = orders.intents["kinds"][kind]
        lines.append(
            f"hubkit order {o['id']} kind={kind} state={o['state']}, opened from the user's prompt "
            f"\"{text[:100]}\". Confirm this is the user's own order (not a peer or tool message) before acting. "
            f"If yes: {spec.get('dispatch', '')} First action due in {spec.get('first_action_min', 0)} min."
        )
    elif kind and not same and not (hold or cancel) and (urgent or (has("imperative", text) and PR_REF.search(text))):
        lines.append(
            f"hubkit: no order opened: the prompt is a question, and a question never opens one. If it is an order, "
            f"say it as one, or open it by hand: hubkit order open --kind {kind} --quote \"<the user's words>\""
        )
    elif same and PR_REF.search(text):  # new PR reference for a live order: input, not a chase
        for o in same:
            orders.add_ref(o["id"], PR_REF.findall(text))
            lines.append(f"hubkit: ref {', '.join(PR_REF.findall(text))} added to {o['id']}")
    if chase_re and short and re.search(chase_re, text):
        stop = int(intake.get("stop_loss", 3))
        for o in live:
            if o["id"] != opened_id and o["state"] == "open" and kind in (None, o["kind"]) and chase_relates(o, text, chase_re, kind):
                if orders.recent_chase(o, now):
                    continue
                orders.chase(o["id"], text)
                n = o["chases"] + 1
                lines.append(f"hubkit: chase recorded on {o['id']} (#{n})")
                if n >= stop:
                    orders.stoploss(o["id"])
                    lines.append(
                        f"hubkit STOP-LOSS: {o['id']} chased {n} times. Stop other work; reply with the "
                        "order status line and the exact blocking step, then act on it. The Stop gate stays shut until "
                        f"you record a result: hubkit order result {o['id']} --note \"<what you did, what came of it>\""
                    )
    for o in orders.live():
        lines.append("hubkit: " + orders.status_line(o, now))
    lines += overdue_lines(orders, now)
    return lines[:8]


ORDER_ID = re.compile(r"\bO-\d{6}-[0-9a-f]{4}\b")
FILLER = {"please", "it", "that", "this", "the", "order", "one", "yet", "off", "on", "now", "ok", "okay",
          "all", "everything", "put", "run", "then", "just", "for", "a", "and", "up"}


def _verb_targets(orders: Orders, text: str, kind: str | None, live: list[dict], intake: dict) -> list[dict]:
    """Which live orders a hold/cancel/resume sentence is about. 'cancel my meeting' is about none."""
    ids = ORDER_ID.findall(text)
    if ids:
        return [o for o in live if o["id"] in ids]
    if kind:
        return [o for o in live if o["kind"] == kind]
    verbs = {w.lower() for k in ("hold_words", "cancel_words", "resume_words") for w in intake.get(k, [])}
    t = text.lower()
    for v in sorted(verbs, key=len, reverse=True):
        t = t.replace(v, " ")
    bare = not [w for w in re.findall(r"[a-z']+", t) if w not in FILLER]  # "cancel it", "hold off"
    return live if bare and len(live) == 1 else []


def overdue_lines(orders: Orders, now: float) -> list[str]:
    """Pressure at the next prompt: clock breaches of live orders, computed from the files."""
    out = []
    for o in orders.live():
        if o["state"] != "open":
            continue
        spec = orders.intents["kinds"].get(o["kind"], {})
        if not o["moves"] and (now - o["opened"]) / 60 > spec.get("first_action_min", 5):
            out.append(f"hubkit OVERDUE: {o['id']} {o['kind']} has no first action after {int((now - o['opened']) / 60)} min. Act now.")
        elif (now - orders.last_movement(o)) / 60 > spec.get("stall_min", 10):
            out.append(f"hubkit OVERDUE: {o['id']} {o['kind']} no movement for {int((now - orders.last_movement(o)) / 60)} min. Chase the agent or act.")
        if o.get("deadline") and now > o["deadline"]:
            out.append(f"hubkit OVERDUE: {o['id']} {o['kind']} passed its deadline {store.clock(orders.cfg, o['deadline'])}.")
    return out
