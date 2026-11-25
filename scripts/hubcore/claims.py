"""Checks on what the hub did or said in a turn, read from the transcript and his recorded words. All warn or hold; none deny."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import lessons, routed, store

# ---- his words: typed prompts and card answers ----


def record_answer(cfg: dict, resp: object, now: float | None = None) -> None:
    """Keep each card answer with its question so a later check can find the words he chose."""
    ans = resp.get("answers") if isinstance(resp, dict) else None
    pairs = list(ans.items()) if isinstance(ans, dict) else [("", resp)] if isinstance(resp, str) else []
    for q, a in pairs:
        if str(a).strip() and store.is_human(str(a)):
            store.append_row(store.state(cfg) / "answers.jsonl", {"ts": now or time.time(), "q": store.safe(str(q), 300), "a": store.safe(str(a), 300)})


def his_words(cfg: dict, since_s: float = 7200, now: float | None = None) -> list[dict]:
    """Typed prompts and card answers within the span, as {say, ctx}: `say` is what he wrote or chose, `ctx` adds a card's question."""
    now = now or time.time()
    typed = [{"say": r["text"], "ctx": r["text"]} for r in lessons.recent_prompts(cfg, since_s, now)]
    cards = [{"say": r["a"], "ctx": f"{r['q']} {r['a']}"} for r in store.read_rows(store.state(cfg) / "answers.jsonl")[0] if now - r["ts"] <= since_s]
    return [*typed, *cards]


NEG_BEFORE = re.compile(r"(?i)\b(never|don'?t|do not|not|no|without)\b[^.\n]{0,25}$")
REFUSAL = re.compile(r"(?i)^\W*(don'?t|do not|no\b|hold|leave|skip|not now|later|keep)")


def orders_act(w: dict, nums: list[str]) -> bool:
    """True when what he wrote or chose orders an act: an act word in a sentence that is not a question, not negated, and
    (when the act names PRs) his words or the card's question name one of them."""
    if REFUSAL.search(w["say"]):
        return False
    for sentence in re.findall(r"[^.!?\n]+[.!?]?", w["say"]):
        if sentence.rstrip().endswith("?"):
            continue
        if any(not NEG_BEFORE.search(sentence[: m.start()]) for m in ACT_WORD.finditer(sentence)):
            return not nums or any(n in w["ctx"] for n in nums)
    return False


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


# ---- the turn's tool calls ----


def turn_calls(transcript: str | Path | None) -> list[dict] | None:
    """Tool calls ({id, name, input}) made after the last typed prompt, oldest first, or None when unreadable."""
    if not transcript:
        return None
    try:
        lines = (
            Path(transcript).read_text(encoding="utf-8", errors="replace").splitlines()
        )
    except OSError:
        return None
    calls: list[dict] = []
    for line in reversed(lines):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        c = (o.get("message") or {}).get("content")
        blocks = [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []
        if (
            o.get("type") == "user"
            and (isinstance(c, str) or any(b.get("type") == "text" for b in blocks))
            and (o.get("origin") or {}).get("kind", "human") == "human"
        ):
            break
        if o.get("type") == "assistant":
            calls = [
                {
                    "id": b.get("id", ""),
                    "name": str(b.get("name")),
                    "input": b.get("input") or {},
                }
                for b in blocks
                if b.get("type") == "tool_use"
            ] + calls
    return calls


# ---- 1. an act (merge, release, prod, workflow dispatch) that none of his words ordered ----

ACT_SEG = re.compile(
    r"^(?:\w+=\S+\s+)*(?:gh\s+pr\s+merge\b|gh\s+workflow\s+run\b|(?:python3?\s+)?\S*\b(?:release|prod)\.py\b)"
)
ACT_IN_BRIEF = re.compile(
    r"\bgh\s+pr\s+merge\b|\bgh\s+workflow\s+run\b|\b(?:release|prod)\.py\b"
)
NEGATED = re.compile(
    r"(?i)\b(never|do not|don't|not|no|banned|without|must not)\b[^\n]{0,40}$"
)
ACT_WORD = re.compile(
    r"(?i)\b(merge[ds]?|release[ds]?|deploy(?:ed|ing)?|ship(?:ped)?|app update|go live|push to prod)\b"
)
NUMS = re.compile(r"(?:/pull/|#|\bmerge\s+|\bpr\s+)(\d{3,6})\b", re.I)


def _acts(call: dict) -> list[str]:
    """The act commands one tool call runs, or (an Agent or SendMessage brief) tells another session to run."""
    inp = call["input"]
    if call["name"] == "Bash":
        segs = [s.strip() for s in re.split(r"[;&|\n]+", str(inp.get("command") or ""))]
        return [s[:120] for s in segs if ACT_SEG.search(s)]
    if call["name"] in ("Agent", "SendMessage"):
        out = []
        for ln in str(inp.get("prompt") or inp.get("message") or "").splitlines():
            m = ACT_IN_BRIEF.search(ln)
            if m and not NEGATED.search(ln[: m.start()]):
                out.append(ln.strip()[:120])
        return out
    return []


def unordered_acts(
    cfg: dict, transcript: str | Path | None, now: float | None = None
) -> list[dict]:
    """Acts of this turn, not yet flagged, with no typed prompt or card answer of his in the last 2 hours that names
    an act word (and the PR number, when the act names one). Each result is {id, act}.
    """
    calls = turn_calls(transcript)
    if not calls:
        return []
    words = his_words(cfg, now=now)
    flagged = {
        r["id"] for r in store.read_rows(store.state(cfg) / "acts-flagged.jsonl")[0]
    }
    out = []
    for c in calls:
        for act in _acts(c):
            nums = NUMS.findall(act)
            if c["id"] in flagged or any(orders_act(w, nums) for w in words):
                continue
            out.append({"id": c["id"], "act": act})
    return out


def flag_acts(cfg: dict, acts: list[dict]) -> None:
    for a in acts:
        store.append_row(
            store.state(cfg) / "acts-flagged.jsonl", {"id": a["id"], "ts": time.time()}
        )


def act_reason(acts: list[dict]) -> str | None:
    if not acts:
        return None
    return (
        f"hubkit: this turn ran {acts[0]['act']!r} but none of his typed words or card answers in the last 2 hours order that act"
        f"{' (and ' + str(len(acts) - 1) + ' more)' if len(acts) > 1 else ''}. A word like 'hotfix' or 'prioritize' is not an order to merge or deploy. "
        "NEXT: tell him now, in one line, what ran and on whose words; stop the act if it is still running; ask with a card before any further step"
    )


# ---- 2. a brief that claims his authority with no quote of his ----

AUTHORITY = re.compile(
    r"(?i)\b(?:(?:this|the) (?:brief|message|prompt) is (?:his|the|NAME's) go|his go\b|is the go\b|"
    r"(?:NAME|he|the user) (?:ordered|approved|authori[sz]ed|told me to|said to)\b|on his (?:order|word|say)\b|ordered it as)"
)
QUOTED = re.compile(r"[\"“]([^\"”]{4,})[\"”]")


def authority_warning(cfg: dict, brief: str, now: float | None = None) -> str | None:
    """A warning when a brief says he ordered or approved something and quotes no words of his that appear in what he typed
    or chose in the last 24 hours (`hubkit asks ls` shows them)."""
    m = re.search(AUTHORITY.pattern.replace("NAME", re.escape(cfg["user"]["name"])), brief)
    if not m:
        return None
    mine = [_norm(w["ctx"]) for w in his_words(cfg, 86400, now)]
    if any(_norm(q) in w for q in QUOTED.findall(brief) for w in mine):
        return None
    return (
        f"hubkit WARNING: this brief claims his authority ({m[0]!r}) but quotes no words of his found in his last 24 hours of asks. "
        "A brief is not his go. The agent may act on it and will not know better. "
        "NEXT: SendMessage the agent now to stop any merge, release or deploy until his exact words are quoted (hubkit asks ls shows them); if he has not named the act, say so"
    )


# ---- 3. a claim about what a PR does or its state, with no read of that PR this turn ----

DOES = r"(?:makes?|changes?|fix(?:es)?|adds?|removes?|covers?|supersedes?|replaces?|lets?|allows?|stops?|prevents?|registers?|moves?|handles?|already|does(?:n't| not)?|only)"
PR_NUM = r"(?:[\w.-]+/)?[\w.-]*#(\d{3,6})\b"
PR_CLAIM = [
    re.compile(rf"{PR_NUM}\)?\s+(?:\w+\s+){{0,2}}?{DOES}\b", re.I),
    re.compile(
        rf"(?:supersed(?:ed|es)|covered|fixed|handled|replaced|obsoleted|made redundant) by {PR_NUM}",
        re.I,
    ),
    re.compile(
        rf"{PR_NUM}[^.\n]{{0,60}}\b(?:un(?:handled|reviewed)|not (?:been )?(?:handled|reviewed|replied|answered))\b",
        re.I,
    ),
]


def unread_pr_claim(last: str, transcript: str | Path | None) -> str | None:
    """A reason when the message says what a PR does or what state it is in and no gh or git command in this turn names that PR."""
    calls = turn_calls(transcript)
    if calls is None:
        return None
    cmds = [str(c["input"].get("command") or "") for c in calls if c["name"] == "Bash"]
    for sentence in re.split(r"(?<=[.!?])\s+|\n", re.sub(r"`", "", last)):
        for rx in PR_CLAIM:
            for n in rx.findall(sentence):
                if not any(
                    re.search(r"\b(?:gh|git)\b", c)
                    and re.search(rf"(?<!\d){n}(?!\d)", c)
                    for c in cmds
                ):
                    return (
                        f"hubkit: your message says what #{n} does or where it stands ({sentence.strip()[:90]!r}) but no gh or git command of this turn read it. "
                        f"A peer's summary or your memory is not the PR. NEXT: gh pr view {n} --json title,body,files,reviews and gh pr diff {n}, then restate it from what they show"
                    )
    return None


# ---- 4. blaming his config or rules without reading them ----

BLAME = re.compile(
    r"(?i)\byour (?:hub )?(?:config|configuration|rules?|settings?|instructions?|standing orders?)\b|\byou (?:configured|set up|wrote) (?:a |the )?rule"
)
SOURCES = re.compile(r"(?i)learnings|overlay|intents|memory|playbook|MEMORY\.md")


def unchecked_blame(last: str, transcript: str | Path | None) -> str | None:
    """A reason when the message says a rule is his and no command or read of this turn touched the files that hold the hub's rules."""
    calls = turn_calls(transcript)
    m = BLAME.search(last)
    if calls is None or not m:
        return None
    if any(SOURCES.search(json.dumps(c["input"])) for c in calls):
        return None
    return (
        f"hubkit: your message says {m[0]!r} but this turn read none of the hub's rule files. The hub writes its own rules; a rule is his only when his dated words are behind it. "
        "NEXT: grep learnings.md, the overlay and the memory files for the rule, quote the words and date, and say plainly if the hub wrote it"
    )


# ---- 5. a time in the past tense that has not come yet ----

PAST_AT = re.compile(
    r"(?i)\b(?:went out|was sent|sent|posted|merged|started|finished|ran|landed|replied|done|failed|read)\b[^.\n]{0,40}\bat (?:about |around |~)?(\d{1,2}):(\d{2})"
)


def future_time(cfg: dict, last: str, now: float | None = None) -> str | None:
    """A reason when the message puts a past event at a clock time later than now (his timezone)."""
    cur = store.clock(cfg, now or time.time())
    cur_min = int(cur[:2]) * 60 + int(cur[3:5])
    for m in PAST_AT.finditer(last):
        t = int(m[1]) * 60 + int(m[2])
        if 5 < t - cur_min < 12 * 60:
            return (
                f"hubkit: your message puts a past event at {m[1]}:{m[2]} but it is {cur[:5]} now. A time is read from the record, not remembered. "
                "NEXT: read the timestamp (log, transcript or gh) and correct the message"
            )
    return None


# ---- 6. asking him what the hub does itself ----


def never_ask(cfg: dict, text: str) -> str | None:
    """The question line that asks him whether to start or stop a box or server, or None. `ask_guard.patterns` and `ask_guard.names` configure it."""
    g = cfg.get("ask_guard", {})
    pats = [re.compile(p) for p in g.get("patterns", [])]
    names = "|".join(re.escape(n) for n in g.get("names", []))
    if names:
        pats.append(
            re.compile(
                rf"(?i)\b(?:{names})\b[^?\n]{{0,60}}\b(?:start|stop|boot|turn on|power on|keep it off)\b|\b(?:start|stop|boot|turn on|power on)\b[^?\n]{{0,40}}\b(?:{names})\b"
            )
        )
    for ln in text.splitlines():
        if "?" in ln and any(p.search(ln) for p in pats):
            return ln.strip()[:100]
    return None


# ---- 7. one PR to a person who is sent batches ----

DM = re.compile(r"\bmep\s+(?:draft|send)\b[^\n]*?--dm\s+(\w+)")
REDIRECT = re.compile(r"<\s*(\S+)")


def batch_warning(cfg: dict, command: str) -> str | None:
    """A warning when a DM to a batch recipient (`routed.batch_to`) names one PR while other PRs are live in the work list."""
    m = DM.search(command)
    if not m or m[1].lower() not in [
        b.lower() for b in routed.opts(cfg).get("batch_to", [])
    ]:
        return None
    text = command
    env = dict(re.findall(r"\b(\w+)=([^\s;]+)", command))
    for r in REDIRECT.findall(command):
        for k, v in env.items():
            r = r.replace(f"${k}", v).replace(f"${{{k}}}", v)
        try:
            text += (
                "\n"
                + Path(r.strip("\"'"))
                .expanduser()
                .read_text(encoding="utf-8")[:4000]
            )
        except OSError:
            pass
    named = {routed.pr_key(u) for u in routed.refs(cfg, text)}
    others = sorted(
        {
            routed.pr_num(p)
            for i in routed.items(cfg)
            for p in i["prs"]
            if routed.pr_key(p) not in named
        }
    )
    if len(named) != 1 or not others:
        return None
    return (
        f"hubkit WARNING: this DM to {m[1]} names one PR while #{', #'.join(others[:6])} are also live. He said: batch, and send a single PR only when it is urgent. "
        "NEXT: hold it and send one DM with every PR that is ready, or say in the DM why this one cannot wait"
    )
