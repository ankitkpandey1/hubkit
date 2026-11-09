"""Checks that come from repeated corrections: re-reading the user's asks, unchecked claims, briefs that forbid the
fix, and an unasked merge. Every check is a warning or a Stop reason; none denies a tool call.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import store
from .orders import Orders

# ---- asks: the user's recent words, one line each ----

_FIN = r"(?:done|finished|complete[d]?|shipped)"
_WHOLE = r"(?:everything|all (?:of it|tasks|work|items|requirements|steps)|it all|the (?:task|work|request|job|order|release)|your (?:request|task|order|ask))"
# A claim that the user's work is finished: "I'm done", "everything is done", "all set", or a bare "Done." Not "tests done" or "#12: done".
DONE_CLAIM = re.compile(
    rf"(?i)^\W*all\s+{_FIN}\b|\b(?:i'?m|i am|we'?re|we are)\s+(?:all\s+)?{_FIN}\b"
    rf"|\b(?:i'?ve|i have|we'?ve|we have)\s+(?:finished|completed|shipped)\s+(?:{_WHOLE}|everything)\b"
    rf"|\b{_WHOLE}\s+(?:is|are|was|has been|have been)?\s*(?:all\s+)?{_FIN}\b"
    rf"|\ball set\b|^\W*(?:all\s+)?{_FIN}\W*$"
)


def claims_done(text: str) -> bool:
    """True when a sentence of the message says the user's work is finished."""
    return any(DONE_CLAIM.search(s) for s in re.split(r"(?<=[.!?])\s+|\n", text))


SPAN = re.compile(r"^(\d+)\s*([mhd])$")


def parse_span(text: str) -> float:
    """`30m`, `12h` or `2d` as seconds; raises ValueError otherwise."""
    m = SPAN.match(text.strip().lower())
    if not m:
        raise ValueError(
            f"--since takes a number and m, h or d (for example 12h), not {text!r}"
        )
    return int(m[1]) * {"m": 60, "h": 3600, "d": 86400}[m[2]]


def recent_prompts(cfg: dict, since_s: float, now: float | None = None) -> list[dict]:
    """The human prompts held in recent-prompts.jsonl within the span, oldest first."""
    now = now or time.time()
    rows = store.read_rows(store.state(cfg) / "recent-prompts.jsonl")[0]
    return [r for r in rows if now - r["ts"] <= since_s]


def asks_lines(cfg: dict, span: str) -> list[str]:
    """`HH:MM  words` per prompt, newest last. Reading them marks the asks as swept for the Stop gate."""
    rows = recent_prompts(cfg, parse_span(span))
    store.write_atomic(store.state(cfg) / "asks-swept", str(time.time()))
    return [f"{store.clock(cfg, r['ts'])}  {r['text']}" for r in rows] or [
        "(no prompts in that span)"
    ]


def _last_work_done(cfg: dict, now: float) -> float:
    last = 0.0
    for back in (0, 1):
        day = store.day_of(cfg, now - back * 86400)
        for r in store.read_rows(store.state(cfg) / "archive" / f"archive_{day}.jsonl")[
            0
        ]:
            if r.get("type") == "work" and r.get("outcome") == "done":
                last = max(last, float(r["ts"]))
    return last


def unswept_asks(cfg: dict, last: str, now: float | None = None) -> str | None:
    """A reason when the message says done while the user's newest prompt is newer than the last `work done` and
    the last `hubkit asks ls`: the requirements were not re-read."""
    now = now or time.time()
    if not claims_done(last):
        return None
    prompts = recent_prompts(cfg, 86400, now)
    if not prompts:
        return None
    try:
        swept = float((store.state(cfg) / "asks-swept").read_text(encoding="utf-8"))
    except OSError, ValueError:
        swept = 0.0
    if prompts[-1]["ts"] <= max(swept, _last_work_done(cfg, now)):
        return None
    return (
        "hubkit: you say it is done but have not re-read his words since his last prompt. NEXT: hubkit asks ls --since 12h; "
        "list every requirement in them and tick each with evidence before saying done"
    )


# ---- claims that need a lookup in the same turn ----

UNCHECKED_WORDS = [
    "gone",
    "lost",
    "absent",
    "vanished",
    "disappeared",
    "does not exist",
    "doesn't exist",
    "never written",
    "was never there",
    "a person's call",
    "needs your call",
    "needs his call",
    "needs her call",
    "needs a human decision",
]
PENDING_WORDS = [
    "pending on you",
    "pending on the user",
    "waiting on you",
    "waiting for you",
    "open on you",
    "still on you",
    "on your plate",
]
LOOKUP_TOOLS = ("Read", "Grep", "Glob", "Bash")


def tools_this_turn(transcript: str | Path | None) -> list[str] | None:
    """Names of the tools called after the last typed prompt in the transcript, or None when unreadable."""
    if not transcript:
        return None
    try:
        lines = (
            Path(transcript).read_text(encoding="utf-8", errors="replace").splitlines()
        )
    except OSError:
        return None
    names: list[str] = []
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
            names += [str(b.get("name")) for b in blocks if b.get("type") == "tool_use"]
    return names


def lookups_this_turn(transcript: str | Path | None) -> int | None:
    """Count of Read/Grep/Glob/Bash calls after the last typed prompt in the transcript, or None when unreadable."""
    names = tools_this_turn(transcript)
    return None if names is None else sum(1 for n in names if n in LOOKUP_TOOLS)


def prose_ask(last: str, transcript: str | Path | None, is_ask: bool) -> str | None:
    """A reason when a real question to the user ends the turn as prose: decisions go through the card tool."""
    names = tools_this_turn(transcript)
    if not is_ask or names is None or "AskUserQuestion" in names:
        return None
    return "hubkit: you asked the user in prose. A decision goes through the card tool (AskUserQuestion) with the recommended option first, not in the message. NEXT: send it as a card"


def unchecked_claim(cfg: dict, last: str, transcript: str | Path | None) -> str | None:
    """A reason when the message calls something gone/lost/absent, or a person's call, and this turn made no lookup."""
    from .gate import _asserted

    words = (
        Orders(cfg).intents.get("stop_gate", {}).get("unchecked_words", UNCHECKED_WORDS)
    )
    hit = next(
        (
            s.strip()[:100]
            for s in re.split(r"(?<=[.!?])\s+|\n", last)
            if any(_asserted(s.lower(), w.lower()) for w in words)
        ),
        None,
    )
    if not hit or lookups_this_turn(transcript) != 0:
        return None
    return (
        f"hubkit: your message claims {hit!r} but this turn read nothing. A report that something is gone, or is a person's call, "
        "is checked, not relayed. NEXT: read the code or record that writes it (or what he already decided), then say it"
    )


def pending_claim(cfg: dict, last: str, transcript: str | Path | None) -> str | None:
    """A reason when the message lists items pending on the user and this turn made no lookup: that list is read now, not remembered."""
    from .gate import _asserted

    words = Orders(cfg).intents.get("stop_gate", {}).get("pending_words", PENDING_WORDS)
    hit = next((s.strip()[:100] for s in re.split(r"(?<=[.!?])\s+|\n", last) if any(_asserted(s.lower(), w.lower()) for w in words)), None)
    if not hit or lookups_this_turn(transcript) != 0:
        return None
    return (
        f"hubkit: your message lists what is pending on him ({hit!r}) but this turn read nothing. That list goes stale in minutes. "
        "NEXT: read the open items now (mep ls, or the record that holds them), then say it"
    )


VERIFIED_WORDS = [
    "tests pass",
    "tests passed",
    "all tests pass",
    "all green",
    "ci is green",
    "it works",
    "now works",
    "is deployed",
    "was deployed",
    "is live now",
    "fixed and verified",
]


def unrun_claim(cfg: dict, last: str, transcript: str | Path | None) -> str | None:
    """A reason when the message says tests pass, it works or it is deployed and this turn ran no command (no Bash call)."""
    from .gate import _asserted

    words = (
        Orders(cfg).intents.get("stop_gate", {}).get("verified_words", VERIFIED_WORDS)
    )
    hit = next(
        (
            s.strip()[:100]
            for s in re.split(r"(?<=[.!?])\s+|\n", last)
            if any(_asserted(s.lower(), w.lower()) for w in words)
        ),
        None,
    )
    names = tools_this_turn(transcript)
    if not hit or names is None or "Bash" in names:
        return None
    return (
        f"hubkit: your message says {hit!r} but this turn ran no command to show it. A pass, a fix or a deploy is shown by a run, not remembered. "
        "NEXT: run the check now and quote its result, or drop the claim"
    )


LINK_REF = re.compile(r"(?<![\w/])(?:[\w.-]+/[\w.-]+)?#\d{4,6}\b")


def bare_refs(cfg: dict, last: str) -> list[str]:
    """PR/issue numbers like `#7457` named in the message while it holds no link at all: the user must be able to click."""
    if (
        not Orders(cfg).intents.get("stop_gate", {}).get("full_links", True)
        or "http" in last
    ):
        return []
    return sorted(set(LINK_REF.findall(re.sub(r"`[^`]*`", "", last))))


# ---- a brief must not forbid the step that clears a blocker ----

FORBID = re.compile(
    r"(?i)\b(do not|don't|dont|never|must not|mustn't|not allowed|forbidden|banned)\b"
)
STOP_WORDS = {
    "with",
    "that",
    "this",
    "from",
    "have",
    "been",
    "when",
    "then",
    "than",
    "into",
    "does",
    "host",
    "need",
    "needs",
    "start",
    "running",
    "after",
    "before",
    "about",
    "their",
    "there",
    "which",
    "would",
    "could",
    "should",
}


def _keys(text: str) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-z][a-z0-9_-]{3,}", text.lower())
        if w not in STOP_WORDS
    }


def brief_forbids_fix(cfg: dict, brief: str) -> str | None:
    """The line of a brief that forbids a step an open order is blocked on (shares a key word with its blocker or fix)."""
    blockers = [
        (o["id"], b)
        for o in Orders(cfg).live()
        if o["state"] == "open" and (b := o.get("blocker"))
    ]
    for line in brief.splitlines():
        if not FORBID.search(line):
            continue
        for oid, b in blockers:
            shared = _keys(line) & _keys(b["on"] + " " + b["fix"])
            if shared:
                return (
                    f"hubkit WARNING: this brief says {line.strip()[:100]!r}, which touches what {oid} is blocked on ({b['on'][:60]}; "
                    f"shared: {', '.join(sorted(shared)[:3])}). A brief must not forbid the one action that clears the blocker. "
                    "NEXT: reword it, or allow that step explicitly"
                )
    return None


RELEASE_COMMANDS = ["--admin"]  # flags release.py itself runs: a brief must not ban them while a release order is open


def release_ban_warning(cfg: dict, brief: str) -> str | None:
    """The line of a brief that bans a command release.py runs itself, while a release order is open."""
    orders = Orders(cfg)
    words = orders.intents.get("stop_gate", {}).get("release_commands", RELEASE_COMMANDS)
    oid = next((o["id"] for o in orders.live() if o["state"] == "open" and o["kind"] == "release"), None)
    line = next((ln for ln in brief.splitlines() if oid and FORBID.search(ln) and any(w in ln for w in words)), None)
    if not line:
        return None
    return (
        f"hubkit WARNING: this brief says {line.strip()[:100]!r} while release order {oid} is open, but release.py runs that itself "
        "(master needs --admin because a required check never reports). NEXT: remove the ban; the release runner does exactly what the script does"
    )


# ---- merge guard (warn only) ----

MERGE_CMD = re.compile(r"\bgh\s+pr\s+merge\b([^\n;&|]*)")


def merge_target(command: str) -> str | None:
    """The PR number a `gh pr merge` command names ("" when it names none), or None when it is not a merge."""
    m = MERGE_CMD.search(command)
    if not m:
        return None
    n = re.search(r"(?:/pull/|\s)(\d{1,7})\b", " " + m[1])
    return n[1] if n else ""


def merge_warning(cfg: dict, command: str, now: float | None = None) -> str | None:
    """A warning when a merge has neither a live order nor a typed word of his naming that PR. Never denies."""
    num = merge_target(command)
    if num is None:
        return None
    now = now or time.time()
    for o in Orders(cfg).live():
        if o["state"] == "open" and (
            re.search(rf"(?<!\d){num}(?!\d)", " ".join([o["quote"], *o["refs"]]))
            if num
            else o["kind"] == "merge"
        ):
            return None
    for r in recent_prompts(cfg, 7200, now):
        if re.search(r"(?i)\bmerge", r["text"]) and (
            not num or re.search(rf"(?<!\d){num}(?!\d)", r["text"])
        ):
            return None
    what = f"#{num}" if num else "a PR"
    return (
        f"hubkit WARNING: merging {what} with no live order and none of his typed words naming it. A merge is done on his order only. "
        'NEXT: stop unless he ordered it; if he did, open the order: hubkit order open --kind merge --quote "<his words>"'
    )


# ---- warn-only checks before a tool call: posts, banned commands, stopping an agent ----

POST_CMD = re.compile(
    r"(?i)\b(?:mep\s+(?:send|reply|post)|chat\S*\s+send|gh\s+(?:pr|issue)\s+(?:comment|review)|gh\s+api\b[^\n]*\b(?:comments|reviews)\b|slack\S*\s+(?:post|send))"
)
BANNED_CMD = re.compile(
    r"(?i)(?:\brm\s+-\w*r\w*f|\brm\s+-\w*f\w*r|\bpkill\b|\bgit\s+reset\s+--hard|\bgit\s+stash\b|\bgit\s+clean\b"
    r"|\bgit\s+push\b[^\n;&|]*\s(?:--force\b|--force-with-lease\b|-f\b)|\bgit\s+checkout\s+\S+\s+--\s)"
)
YES_WORD = re.compile(
    r"(?i)\b(yes|yep|go|go ahead|send it|post it|approved?|ok|okay|do it|ship it)\b"
)
STOP_WORD = re.compile(r"(?i)\b(kill|stop|cancel|abort|terminate)\b")


def _typed_recently(
    cfg: dict, pattern: re.Pattern[str], seconds: float, now: float
) -> bool:
    return any(pattern.search(r["text"]) for r in recent_prompts(cfg, seconds, now))


def tool_warning(
    cfg: dict, tool: str, tool_input: dict, now: float | None = None
) -> str | None:
    """A warning for one tool call, else None. Never denies. Merges, posts, banned commands and TaskStop."""
    now = now or time.time()
    if tool == "TaskStop":
        if _typed_recently(cfg, STOP_WORD, 1800, now):
            return None
        return (
            "hubkit WARNING: stopping a running agent that he did not ask to stop. Its half-done work is lost. "
            "NEXT: let it finish and harvest the result, or SendMessage it to redirect; stop it only on his word"
        )
    if tool == "AskUserQuestion":
        return card_warning(tool_input)
    command = str(tool_input.get("command") or "")
    if tool != "Bash" or not command:
        return None
    if FULL_PYTEST.search(command):
        return "hubkit WARNING: pytest with no path, -k or -m runs the whole suite. NEXT: run only the tests that cover the change"
    if again := repeat_warning(cfg, command, now):
        return again
    if m := BANNED_CMD.search(command):
        return f"hubkit WARNING: {m[0].strip()!r} is a banned command. NEXT: use the safe form (revert with a new commit, remove one named file, send a normal push) or ask him"
    if POST_CMD.search(command) and not _typed_recently(cfg, YES_WORD, 600, now):
        return (
            "hubkit WARNING: posting to a person or a thread with no typed yes of his in the last 10 minutes. A message goes out on his word, "
            "from the right account. NEXT: show him the draft and send on his yes"
        )
    return merge_warning(cfg, command, now)


# ---- delivery: a report on built work with the work still only on this machine ----

BUILT_WORDS = re.compile(
    r"(?i)\b(built|implemented|fixed|committed|ready|shipped|finished|completed|done)\b"
)
LOCAL_OK = re.compile(
    r"(?i)\b(not (yet )?pushed|unpushed|local only|uncommitted|not committed|before (the )?push)\b"
)


def turn_start(transcript: str | Path | None) -> float | None:
    """Epoch time of the last typed prompt in the transcript (when this turn began), or None when unknown."""
    if not transcript:
        return None
    try:
        lines = (
            Path(transcript).read_text(encoding="utf-8", errors="replace").splitlines()
        )
    except OSError:
        return None
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
            try:
                return datetime.fromisoformat(
                    str(o["timestamp"]).replace("Z", "+00:00")
                ).timestamp()
            except KeyError, ValueError:
                return None
    return None


def _touched_since(cwd: str, porcelain: list[str], since: float) -> list[str]:
    """The changed files of `git status --porcelain` lines whose mtime is at or after `since` (a deleted file counts as old)."""
    out = []
    for ln in porcelain:
        path = ln[3:].split(" -> ")[-1].strip().strip('"')
        try:
            if os.stat(Path(cwd) / path).st_mtime >= since:
                out.append(path)
        except OSError:
            continue
    return out


def _unpushed_since(cwd: str, since: float) -> int:
    """Unpushed commits made at or after `since`."""
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "log", "@{u}..HEAD", "--format=%ct"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return 0
    return sum(1 for x in r.stdout.split() if x.isdigit() and float(x) >= since)


def unpushed_work(
    last: str, cwd: str | None, transcript: str | Path | None = None
) -> str | None:
    """A reason when the message reports built work while the repo at `cwd` has changes or commits from this turn that
    no remote has. With the turn's start known (the transcript), older dirt is not this turn's work and does not hold.
    """
    if not cwd or not BUILT_WORDS.search(last) or LOCAL_OK.search(last):
        return None
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "status", "--porcelain", "-b"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    lines = r.stdout.splitlines()
    if r.returncode != 0 or not lines:
        return None
    dirty = [ln for ln in lines[1:] if not ln.startswith("??")]
    ahead = re.search(r"\[ahead (\d+)", lines[0])
    n_ahead = int(ahead[1]) if ahead else 0
    since = turn_start(transcript)
    if since is not None:
        dirty = _touched_since(cwd, dirty, since)
        n_ahead = _unpushed_since(cwd, since) if n_ahead else 0
    if not dirty and not n_ahead:
        return None
    what = ", ".join(
        [
            *([f"{len(dirty)} changed file(s) not committed"] if dirty else []),
            *([f"{n_ahead} commit(s) not pushed"] if n_ahead else []),
        ]
    )
    return f"hubkit: you report the work as built but this checkout has {what}. A report on work only on this machine is not delivery. NEXT: commit and push it (or say plainly it is local only)"


# ---- briefs: required lines and the agent cap ----

BRIEF_LINES = {
    "Done-when": re.compile(r"(?i)done[- ]when"),
    "Output": re.compile(r"(?i)\boutput\b"),
    "Estimate": re.compile(r"(?i)\bestimate\b"),
}


def brief_missing(cfg: dict, text: str) -> str | None:
    """A warning when a long (real) brief lacks a termination clause, an output shape or an estimate."""
    from . import routed

    if len(text) < int(routed.opts(cfg)["min_chars"]) or routed.ACK.match(text):
        return None
    missing = [k for k, rx in BRIEF_LINES.items() if not rx.search(text)]
    if len(missing) == len(BRIEF_LINES) and not (
        routed.DELIVERABLE.search(text)
        or routed.DIRECTIVE.search(text)
        or routed.REQUEST.search(text)
    ):
        return None  # not an ask
    return (
        (
            f"hubkit WARNING: this brief has no {', '.join(missing)} line. An agent without a done-when never stops, one without an output shape returns prose, "
            "one without an estimate cannot be judged slow. NEXT: add the missing line(s) and re-send"
        )
        if missing
        else None
    )


def agent_cap_warning(cfg: dict) -> str | None:
    """A warning when more distinct agents hold live routed work than `routed.max_agents`."""
    from . import routed

    owners = {i["owner"] for i in routed.items(cfg)}
    cap = int(routed.opts(cfg).get("max_agents", 4))
    if len(owners) <= cap:
        return None
    return f"hubkit WARNING: {len(owners)} agents hold live work (cap {cap}). Each extra agent is more to chase and merge. NEXT: wait for one to finish before routing another"


# ---- repeating the same command, full test runs, cards without a recommendation ----


def repeat_warning(
    cfg: dict, command: str, now: float, limit: int = 3, window_s: float = 600.0
) -> str | None:
    """A warning when the same command has run `limit` times within the window (the ledger is the state file `commands.jsonl`)."""
    p = store.state(cfg) / "commands.jsonl"
    rows = [r for r in store.read_rows(p)[0] if now - r["ts"] <= window_s][-50:]
    store.append_row(p, {"ts": now, "cmd": command[:200]})
    if sum(1 for r in rows if r["cmd"] == command[:200]) + 1 < limit:
        return None
    return f"hubkit WARNING: this exact command has now run {limit} times in {int(window_s / 60)} minutes. Repeating it will not change the result. NEXT: read why it fails, or change the approach"


FULL_PYTEST = re.compile(r"\bpytest\b(?![^\n;&|]*(?:\s-k\b|\s-m\b|\.py\b|tests?/|::))")


def card_warning(tool_input: dict) -> str | None:
    """A decision card whose first option of any question is not marked as the recommended one."""
    for q in tool_input.get("questions") or []:
        opts = q.get("options") or []
        if opts and "recommend" not in str(opts[0].get("label", "")).lower():
            return "hubkit WARNING: the first option of this card is not marked (Recommended). Put your recommendation first so he can accept it in seconds. NEXT: reorder and label it"
    return None
