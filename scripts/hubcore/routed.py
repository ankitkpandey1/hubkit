"""Routed work: an outcome ledger for everything the hub hands to a peer session or background agent.

An item is an OUTCOME (owner + PR number), not a message. The PostToolUse hook on SendMessage and
Agent opens it; a second ask about the same PR to the same owner refreshes it; a chase ("status on
#7") is only an event on it and never restarts its clock. Time is read from files: nothing from the
owner for the limit is a stall, the stall climbs a ladder, and every item has one NEXT action.
`tick` reads each PR with `gh` once per tick; a review that lands at the PR's current head closes a
review item by itself, a head that moved on is an alert.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import store
from .orders import PR_REF, PR_URL, load_intents
from .status import next_work_id, work_load, work_save

ACK = re.compile(
    r"(?i)^\W*(ack(nowledged)?|ok(ay)?|thanks?|thank you|got it|noted|will do|roger|done)\b"
)
CHASE = re.compile(
    r"(?i)\b(status|eta|updates?|any progress|where are|how long|still (not|waiting|running)|done\?|report back|one[- ]line (state|status)|current state|what state|state of)\b"
)
BARE = re.compile(r"(?<![\w/])(?:#|PR\s*#?\s*)(\d{2,6})\b", re.IGNORECASE)
FROM_NAME = re.compile(r'<cross-session-message[^>]*\bfrom-name="([^"]+)"')
MAX_PRS = 3
MAX_QUEUE = 50
MAX_EVENTS = 20
SCAN_TAIL = 2_000_000  # first scan of a transcript reads only its last 2 MB


def opts(cfg: dict) -> dict:
    """cfg [routed] overridden by intents.toml [routed]."""
    return {**cfg["routed"], **load_intents(cfg).get("routed", {})}


# ---- what counts as routed work ----


PUSH = re.compile(
    r"(?i)\b(fix(?:es|ed)?|address|rebase|implement|resolve|amend|patch|push|rework|refactor)\b"
)
EXAMPLE = re.compile(
    r"(?i)\b(examples?|e\.g\.|for instance|such as|sample|placeholder|hypothetical)\b"
)
QUOTED = re.compile(r"```.*?```|`[^`\n]*`|\"[^\"\n]*\"|\u201c[^\u201d\n]*\u201d", re.S)
NOUN_BEFORE = re.compile(
    r"(?i)\b(the|his|her|their|its|a|an|my|our|your|that|this|in|from|with)\s+$"
)
NOUN_AFTER = re.compile(
    r"(?i)^s?\s+(comments?|findings?|feedback|notes?|threads?|rounds?)\b"
)


def strip_examples(text: str) -> str:
    """The text without what only illustrates: code fences, backtick and quoted spans, and the part of a sentence from
    "example", "e.g." or "such as" on (the line after a header that ends in a colon goes too). What comes before the marker
    stays: "Review #7, e.g. the loader part" still asks for #7. A PR number inside an example is not an ask.
    """
    text = QUOTED.sub(" ", text)
    kept: list[str] = []
    skip_next = False
    for s in re.split(r"(?<=[.!?])\s+|\n", text.replace("e.g.", "example")):
        m = EXAMPLE.search(s)
        if skip_next or m:
            skip_next = bool(m) and s.rstrip().endswith(":")
            if m and not skip_next and not m.start() == 0:
                kept.append(s[: m.start()])
            continue
        kept.append(s)
    return "\n".join(kept)


LEADS_CHANGE = re.compile(
    r"(?i)^\W*(?:(?:please|then|and|also)\s+)*(?:fix(?:es|ed)?|address|resolve|apply|rebase|implement|amend|patch|push|rework|refactor)\b"
)


REVIEW_LEAD = re.compile(
    r"(?i)^\W*(?:(?:please|then|and|also)\s+)*(?:full\s+|gated\s+|re-?)*review\b"
)


def _kinds(cfg: dict, sentence: str) -> set[str]:
    """What the owner may be asked to do in one sentence. A review word beside a PR is a review ask by default
    ("review #7", "needs a review", "can I get a review on #7"). The one exception: the sentence opens with a change
    verb (fix, address, resolve, apply, ...), every review word in it names an existing review (his review, the
    review comments) and no later clause ("then give #7 a review") asks for one, so the owner is asked to change the
    PR, not to review it. "push" marks a change verb anywhere."""
    low = sentence.lower()
    out = {"push"} if PUSH.search(low) else set()
    hits = [
        m
        for w in (x.lower() for x in opts(cfg)["review_words"])
        for m in re.finditer(re.escape(w), low)
    ]
    existing = all(
        NOUN_BEFORE.search(low[: m.start()]) or NOUN_AFTER.match(low[m.end() :])
        for m in hits
    )
    words = [x.lower() for x in opts(cfg)["review_words"]]
    later = [c.strip() for c in re.split(r",|\bthen\b|\band\b", low)[1:]]
    asks_later = any(
        any(w in c for w in words) and not LEADS_CHANGE.match(c) for c in later
    )
    if hits and not (LEADS_CHANGE.match(low) and existing and not asks_later):
        out.add("review")
    return out


def _asks(cfg: dict, text: str, summary: str = "") -> set[str]:
    """The kinds of ask (see _kinds) in the sentences that name a PR, plus the summary when the text names one."""
    sentences = [
        x for x in re.split(r"(?<=[.!?])\s+|\n", text) if x.strip() and PR_REF.search(x)
    ]
    if summary.strip() and PR_REF.search(text + " " + summary):
        sentences.append(summary)
    return set().union(*(_kinds(cfg, x) for x in sentences)) if sentences else set()


def is_review(cfg: dict, text: str, summary: str = "") -> bool:
    """A review ask: the owner is asked to review a PR, i.e. a sentence naming a PR leads with a review word (or the
    summary does). A review word elsewhere (a handover, a status) or as a noun after another action ("fix the
    findings in his review of #7") does not make it one."""
    body = strip_examples(text)
    if "review" not in _asks(cfg, body, summary):
        return False
    # a job titled as a change ("Fix #7 review findings") asks for changes, not a verdict, unless a sentence leads with a review
    leads_review = any(
        REVIEW_LEAD.match(x)
        for x in re.split(r"(?<=[.!?])\s+|\n", body)
        if PR_REF.search(x)
    )
    return not (LEADS_CHANGE.match(summary.strip().lower()) and not leads_review)


def pushes_to_pr(cfg: dict, text: str, summary: str = "") -> bool:
    """The owner is asked to change the PR (fix, rebase, address findings): its own pushes move the head."""
    body = strip_examples(text)
    if "push" in _asks(cfg, body, summary):
        return True
    # a fix job whose change verb sits in another sentence than the PR number ("Fix the gaps. ... PR #7"): still a fix job
    return (
        bool(PR_REF.search(text + " " + summary))
        and bool(PUSH.search(body + " " + summary))
        and not is_review(cfg, text, summary)
    )


def owner_is_author(cfg: dict, owner: str, author: str) -> bool:
    """Is the owner the PR's author? A peer or agent has no GitHub login, so this uses the data on hand: an
    explicit `routed.owner_logins` map (owner name -> login), else the owner's name equals the login or contains
    it (a login of 4+ characters). An agent id never matches, so it is treated as a non-author.
    """
    login = author.lower()
    if not login:
        return False
    mapped = opts(cfg)["owner_logins"].get(owner)
    return (
        (mapped or "").lower() == login
        or owner.lower() == login
        or (len(login) >= 4 and login in owner.lower())
    )


def is_chase(cfg: dict, text: str) -> bool:
    """A short status question about a PR: an event on the existing item, not a new ask. A message with a brief marker
    (Done-when, Output) or a sentence that opens with an action on a PR (Review #N ...) is an ask whatever else it
    says; a status question that merely mentions a review ("state of the review on #N") is not.
    """
    sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n", text)]
    if DELIVERABLE.search(text) or any(DIRECTIVE.match(x) for x in sentences):
        return False
    return len(text) < int(opts(cfg)["min_chars"]) and bool(CHASE.search(text))


QUEUED = re.compile(
    r"(?i)\b(?:after|behind|once)\b[^.\n]{0,40}?(?:'s\b|\bqueue\b|\bbatch\b|\bfinish(?:es|ed)?\b|\bdone\b|\bcomplete[sd]?\b|\bahead\b)"
)


def queued_behind(text: str, summary: str = "") -> bool:
    """The ask says it waits behind other work ("after X's queue", "once Y finishes"): the owner has not started it."""
    return bool(QUEUED.search(f"{summary}\n{text}"))


DELIVERABLE = re.compile(r"(?i)done[- ]when|\boutput\b|\bdeliverable")


def is_deliverable_brief(cfg: dict, description: str, prompt: str) -> bool:
    """A background agent is routed work only when its brief asks for a result (a PR, an Output, a Done-when) and is
    not a standing watcher (`routed.skip_briefs`); a long-running agent with nothing to deliver cannot stall.
    """
    low = f"{description} {prompt}".lower()
    if any(w.lower() in low for w in opts(cfg)["skip_briefs"]):
        return False
    return bool(DELIVERABLE.search(prompt) or PR_REF.search(prompt))


def _is_reply(
    cfg: dict, owner: str, text: str, asked: list[str], now: float | None
) -> bool:
    """The hub answering a peer who wrote to it recently, with no PR to work on and no brief, is not an ask."""
    if asked or DELIVERABLE.search(text):
        return False
    wrote = peers(cfg).get(owner.lower(), 0.0)
    return (now or time.time()) - wrote < float(opts(cfg)["reply_window_min"]) * 60


def peers(cfg: dict) -> dict[str, float]:
    """Last time each peer wrote to the hub (from the transcript scans)."""
    try:
        return json.loads((store.state(cfg) / "peers.json").read_text(encoding="utf-8"))
    except ValueError, OSError:
        return {}


def _note_peers(cfg: dict, events: list[tuple[float, str, str]]) -> None:
    seen = peers(cfg)
    for ts, name, _ in events:
        if name and ts > seen.get(name.lower(), 0.0):
            seen[name.lower()] = ts
    store.write_atomic(store.state(cfg) / "peers.json", json.dumps(seen))


def _is_follow_up(cfg: dict, owner: str, text: str) -> bool:
    """To an owner with open items: not an acknowledgement, names no PR, carries no new brief."""
    return (
        not ACK.match(text)
        and not PR_REF.search(text)
        and not DELIVERABLE.search(text)
        and not any(s.lower() in owner.lower() for s in opts(cfg)["skip_owners"])
        and any(i["owner"] == owner for i in items(cfg))
    )


def is_work(cfg: dict, owner: str, text: str, asked: list[str] | None = None) -> bool:
    """A message is routed work when it names a PR or is long; acknowledgements and the inbox watcher are not."""
    o = opts(cfg)
    if any(s.lower() in owner.lower() for s in o["skip_owners"]):
        return False
    if ACK.match(text) and not (
        DELIVERABLE.search(text) or DIRECTIVE.search(text) or REQUEST.search(text)
    ):
        return False  # "Ack #399 hold": a bare acknowledgement that names a PR is not an ask; any request cue makes it one
    if asked if asked is not None else PR_REF.search(text):
        return True
    if len(text) >= int(o["min_chars"]):
        return not ACK.match(text)
    return is_short_ask(text)


SHORT_ASK = re.compile(
    r"(?im)(?:^\W*|[.!?]\s+)(?:please|pls|can you|could you|would you|will you|check|look|fix|send|run|merge|deploy|rebase|take|handle|tell|report|reply|confirm|verify|investigate|find|start|stop|update|write|add|open|close|make)\b|\?\s*$"
)


def is_short_ask(text: str) -> bool:
    """A short message that asks the peer to do or answer something: a request verb opening a sentence, or a question.
    Not an acknowledgement, not a status question (that is a chase), and long enough to carry a request.
    """
    return (
        len(text.strip()) >= 15
        and not ACK.match(text)
        and not CHASE.search(text)
        and bool(SHORT_ASK.search(text))
    )


REQUEST = re.compile(
    r"(?i)\b(please|pls|needs?|needed|next|your|you|eyes|look|review|check|fix|merge|deploy|rebase|send|take|handle|findings?|can|could|would|should|must|do)\b|\?"
)
DIRECTIVE = re.compile(
    r"(?i)\b(re-?review|review|re-?check|check|merge|approve|test|rebase|deploy|fix|land|verify|finish|start|continue)\b"
    r"(?:[^.\n]|\.(?=\S)){0,60}?(?:https://github\.com/[\w.-]+/[\w.-]+/pull/\d+|#\d{2,}|\bpr\s*#?\d+)(?:[^.\n]|\.(?=\S)){0,300}"
)


def asked_refs(
    cfg: dict, text: str, summary: str = "", cap: int | None = MAX_QUEUE
) -> list[str]:
    """The PRs the owner is asked to work on. A short message asks about every PR it names. In a long one only
    the summary and sentences that direct an action at a PR count: a PR named in passing (a bug
    list, a status) is not an ask."""
    text = strip_examples(text)
    if len(text) < int(opts(cfg)["min_chars"]):
        return refs(cfg, f"{summary}\n{text}", cap)
    spans = [summary, *(m.group(0) for m in DIRECTIVE.finditer(text))]
    return refs(cfg, "\n".join(spans), cap)


def pr_num(ref: str) -> str:
    """The PR number at the end of a URL or `#N` reference."""
    return ref.rstrip("/").rsplit("/", 1)[-1].lstrip("#")


def pr_key(ref: str) -> str:
    """Repo and number of a PR ref: `owner/repo#N` for a URL, `#N` for a bare number (repo not known)."""
    parts = ref.rstrip("/").split("/")
    return (
        f"{parts[3]}/{parts[4]}#{parts[6]}".lower()
        if ref.startswith("http") and len(parts) >= 7
        else "#" + pr_num(ref)
    )


def same_pr(a: str, b: str) -> bool:
    """The same PR: equal repo and number, or one side is a bare number (repo unknown) with the same number."""
    ka, kb = pr_key(a), pr_key(b)
    return ka == kb or (("/" not in ka or "/" not in kb) and pr_num(a) == pr_num(b))


def overlaps(xs: list[str], ys: list[str]) -> bool:
    """Any PR in common."""
    return any(same_pr(x, y) for x in xs for y in ys)


def covers(xs: list[str], ys: list[str]) -> bool:
    """Every PR of `xs` is in `ys`."""
    return all(any(same_pr(x, y) for y in ys) for x in xs)


REPO_REF = re.compile(r"(?<![\w/.-])([\w.-]+/[\w.-]+)#(\d{2,6})\b")


def _repo_refs(cfg: dict, text: str) -> tuple[list[str], str]:
    """PRs named with their repo: `owner/repo#N`, or `<alias> N` / `<alias>#N` / `<alias> PR N` for a configured
    `routed.repo_aliases` entry (alias -> owner/repo). Returns the URLs and the text with those spans removed, so
    the number is not read again as a bare one in the default repo."""
    out: list[str] = []

    def take(repo: str, n: str) -> str:
        out.append(f"https://github.com/{repo}/pull/{n}")
        return " "

    def repl(repo: str | None = None):  # noqa: ANN202 - a re.sub callback
        def inner(m: re.Match[str]) -> str:
            return take(repo or m[1], m[2] if repo is None else m[1])

        return inner

    text = REPO_REF.sub(repl(), text)
    for alias, repo in dict(opts(cfg).get("repo_aliases", {})).items():
        text = re.sub(
            rf"(?i)(?<![\w/.-]){re.escape(alias)}(?:\s*#|\s+pr\s*#?\s*|\s+)(\d{{2,6}})\b",
            repl(repo),
            text,
        )
    return list(dict.fromkeys(out)), text


def refs(cfg: dict, text: str, cap: int | None = MAX_PRS) -> list[str]:
    """PR refs in a text: full URLs, repo-qualified refs, and bare #N / PR N resolved against `routed.repo`
    (else left as #N)."""
    qualified, text = _repo_refs(cfg, text)
    urls = list(dict.fromkeys([*PR_URL.findall(text), *qualified]))
    seen = {pr_key(u) for u in urls}
    repo = str(opts(cfg).get("repo", ""))
    out = list(urls)
    for n in dict.fromkeys(BARE.findall(text)):
        ref = f"https://github.com/{repo}/pull/{n}" if repo else f"#{n}"
        if pr_key(ref) not in seen:
            seen.add(pr_key(ref))
            out.append(ref)
    return out[:cap]


# ---- gh ----


def _gh(cfg: dict) -> str | None:
    want = os.path.expanduser(cfg["tools"]["gh"])
    if os.sep in want:
        return want if os.access(want, os.X_OK) else None
    found = shutil.which(want)
    if found:
        return found
    for d in ("~/.local/bin", "/usr/local/bin", "/usr/bin"):
        cand = os.path.join(os.path.expanduser(d), want)
        if os.access(cand, os.X_OK):
            return cand
    return None


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def pr_info(cfg: dict, url: str, timeout: float) -> dict | None:
    """Head sha, PR author and every review, from gh; None (unknown) on any failure."""
    exe = _gh(cfg)
    if exe is None or timeout <= 0 or not url.startswith("http"):
        return None
    try:
        r = subprocess.run(
            [
                exe,
                "pr",
                "view",
                url,
                "--json",
                "headRefOid,reviews,author,commits,state,statusCheckRollup",
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        data = json.loads(r.stdout)
        revs: list[dict] = [
            {
                "at": _epoch(x["submittedAt"]),
                "state": str(x.get("state", "")),
                "commit": str((x.get("commit") or {}).get("oid", "")),
                "login": str((x.get("author") or {}).get("login", "")),
                "body": str(x.get("body") or ""),
            }
            for x in data["reviews"]
            if x.get("submittedAt")
        ]
        return {
            "head": str(data["headRefOid"]),
            "reviews": revs,
            "author": str((data.get("author") or {}).get("login", "")),
            "review_at": max((x["at"] for x in revs), default=None),
            "head_author": _last_author(data.get("commits")),
            "state": str(data.get("state") or "").upper(),
            "ci": ci_state(data.get("statusCheckRollup")),
        }
    except OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError:
        return None


FAILED_CHECK = {
    "FAILURE",
    "TIMED_OUT",
    "CANCELLED",
    "STARTUP_FAILURE",
    "ACTION_REQUIRED",
    "ERROR",
}
OK_CHECK = {"SUCCESS", "NEUTRAL", "SKIPPED"}


def ci_state(rollup: object) -> str:
    """CI at the PR's head from gh's statusCheckRollup: 'failing', 'pending', 'green', or 'none' (no checks reported)."""
    if not isinstance(rollup, list) or not rollup:
        return "none"
    seen = {
        str(c.get("conclusion") or c.get("state") or "").upper() or "PENDING"
        for c in rollup
        if isinstance(c, dict)
    }
    if seen & FAILED_CHECK:
        return "failing"
    return "green" if seen <= OK_CHECK else "pending"


def _last_author(commits: object) -> str:
    """Login of the author of the PR's newest commit, else an empty string (unknown)."""
    try:
        return str(commits[-1]["authors"][0]["login"]) if commits else ""  # type: ignore[index]
    except KeyError, IndexError, TypeError:
        return ""


def counts_as_review(cfg: dict, rev: dict, info: dict) -> bool:
    """Does this review count as the review that was asked for? A non-author review does. On a PR written by the
    hub's user (`user.github_login`) the reviewer peer posts from that same account, and GitHub allows only a
    COMMENT there, so a COMMENT review with a body counts on such a PR. A COMMENT with an empty body is what a reply in
    an inline thread leaves behind (gh gives each review's `body`), so it does not. `reviewer_logins`, when set,
    limits who counts."""
    need = opts(cfg)["reviewer_logins"]
    if need and rev["login"] not in need:
        return False
    if rev["login"] != info["author"]:
        return True
    me = str(cfg["user"].get("github_login", "")).lower()
    return (
        bool(me)
        and rev["login"].lower() == me
        and rev["state"].upper().startswith("COMMENT")
        and bool(rev["body"].strip())
    )


def same_head(a: str, b: str) -> bool:
    """True when two shas agree on their common prefix."""
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


# ---- the items ----


def items(cfg: dict) -> list[dict]:
    """All routed work items."""
    return [i for i in work_load(store.state(cfg)) if i.get("kind") == "routed"]


def _event(item: dict, kind: str, text: str, now: float) -> None:
    item["events"] = [
        *item.get("events", []),
        {"t": now, "kind": kind, "text": text[:160]},
    ][-MAX_EVENTS:]


def _merge_prs(old: list[str], new: list[str]) -> list[str]:
    """One ref per PR number; a full URL replaces a bare #N."""
    merged: list[str] = []
    for ref in [*old, *new]:
        hit = next((k for k, m in enumerate(merged) if same_pr(m, ref)), None)
        if hit is None:
            merged.append(ref)
        elif ref.startswith("http"):
            merged[hit] = ref
    return merged[:MAX_PRS]


def _same_job(i: dict, owner: str, prs: list[str], summary: str) -> bool:
    if i.get("kind") != "routed" or i["owner"] != owner:
        return False
    if prs:
        return overlaps([*i["prs"], *i.get("queue", [])], prs)
    return not i[
        "prs"
    ]  # no PR named: a follow-up to the same owner is the same outcome


def _chase(cfg: dict, item: dict, text: str, heads: dict[str, str], now: float) -> None:
    """A follow-up to the owner about work already on the item: counted, never a new clock. It answers a moved head
    or an old-head verdict (the hub acted), so those flags clear and the new head becomes the baseline.
    """
    _event(item, "chase", text, now)
    item["chases"] = int(item.get("chases", 0)) + 1
    item["addressed"] = [*item.get("addressed", []), now]
    item["chased_level"] = eff_level(cfg, item, now)
    v = item.get("verdict_old_head")
    if v:
        item["verdict_acked"] = [v["commit"], v["head"]]
    item["head_moved"] = item["verdict_old_head"] = None
    item["heads"] = {**item["heads"], **heads}


def chase_owner(
    cfg: dict, owner: str, text: str, now: float | None = None
) -> dict | None:
    """A message to an owner who has open items, naming no PR and carrying no new brief, is a follow-up on all of
    them. Returns the first item, or None when the owner has none."""
    now = now or time.time()
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        all_ = work_load(sd)
        mine = [i for i in all_ if i.get("kind") == "routed" and i["owner"] == owner]
        for i in mine:
            _chase(cfg, i, text[:120], {}, now)
        if mine:
            work_save(sd, all_)
    return mine[0] if mine else None


def record(
    cfg: dict,
    owner: str,
    summary: str,
    prs: list[str],
    heads: dict[str, str],
    tool_use_id: str = "",
    now: float | None = None,
    review: bool = False,
    chase: bool = False,
    brief: bool = False,
    queue: list[str] | None = None,
    waiting: bool = False,
    pushes: bool = False,
    estimate: int | None = None,
) -> dict:
    """Open the outcome, or refresh it (same owner and PR number). A chase only adds an event."""
    now = now or time.time()
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        all_ = work_load(sd)
        named = [*prs, *(queue or [])]
        hit = next((i for i in all_ if _same_job(i, owner, named, summary)), None)
        follow_up = (
            hit is not None
            and not brief
            and covers(named, [*hit["prs"], *hit.get("queue", [])])
        )
        if hit is not None and (chase or follow_up):
            _chase(cfg, hit, summary, heads, now)
            if not hit.get("queue"):
                hit["prs"] = _merge_prs(
                    hit["prs"], prs
                )  # a full URL replaces a bare #N
            work_save(sd, all_)
            return hit
        is_new = hit is None
        if hit is None:
            hit = {
                "id": next_work_id(sd, all_),
                "kind": "routed",
                "owner": owner,
                "note": "",
                "added": now,
                "last_heard": None,
                "heads": {},
                "prs": [],
                "events": [],
                "chases": 0,
            }
            all_.append(hit)
        moved: list[dict] = []
        if is_new:
            hit["behind"] = [
                i["id"]
                for i in all_
                if i is not hit and i.get("kind") == "routed" and i["owner"] == owner
            ]
            for (
                other
            ) in (
                all_
            ):  # a brief on the same PRs to another owner is a re-brief: it addresses the old item
                if (
                    other is not hit
                    and other.get("kind") == "routed"
                    and other["owner"] != owner
                    and overlaps(named, [*other["prs"], *other.get("queue", [])])
                ):
                    other["addressed"] = [*other.get("addressed", []), now]
                    if (
                        brief
                    ):  # a real re-brief moves the PRs: the old owner no longer holds them
                        other["prs"] = [
                            u for u in other["prs"] if not overlaps([u], named)
                        ]
                        other["queue"] = [
                            u
                            for u in other.get("queue", [])
                            if not overlaps([u], named)
                        ]
                        other["heads"] = {
                            u: h for u, h in other["heads"].items() if u in other["prs"]
                        }
                        if not other["prs"]:
                            _archive(cfg, other, f"re-briefed to {owner}", now)
                            moved.append(other)
        all_ = [i for i in all_ if not any(i is m for m in moved)]
        _event(hit, "ask", summary, now)
        hit.update(
            title=summary,
            routed_at=now,
            prs=prs if queue else _merge_prs(hit["prs"], prs),
            queue=queue or [],
            waiting=waiting,
            verdict_acked=None,
            review=review,
            pushes=pushes,
            state_ask=chase and not brief,
            tool_use_id=tool_use_id or hit.get("tool_use_id", ""),
            head_moved=None,
            verdict_old_head=None,
            chased_level=None,
        )
        _set_estimate(cfg, hit, estimate, now)
        hit.setdefault(
            "heads0", dict(heads)
        )  # the head when first asked: a later head is the owner's artifact
        hit["heads"] = {
            **hit["heads"],
            **heads,
        }  # re-asking resets the baseline: the hub has acted on the new head
        work_save(sd, all_)
    return hit


def from_estimate(text: str) -> int | None:
    """Minutes of the brief's `Estimate: <n>m` line, else None."""
    from . import slow

    return slow.parse_estimate(text)


def _set_estimate(cfg: dict, item: dict, given: int | None, now: float) -> None:
    """A new ask carries an estimate: the brief's `Estimate:` line, else the usual time for its step (defaulted)."""
    from . import slow

    mins, basis = (float(given), "brief") if given else slow.item_default(cfg, item)
    item["estimates"] = [
        *item.get("estimates", []),
        {
            "t": now,
            "min": mins,
            "source": "brief" if given else "default",
            "basis": basis,
        },
    ]
    item.update(
        estimate_min=mins,
        estimate_defaulted=not given,
        estimate_basis=basis,
        addressed=[],
    )


def from_tool(
    cfg: dict,
    tool: str,
    ti: dict,
    resp: object,
    tool_use_id: str,
    now: float | None = None,
) -> dict | None:
    """The item for one SendMessage / Agent call, or None when the call is not routed work."""
    chase = False
    if tool == "SendMessage":
        owner, text = str(ti.get("to") or ti.get("recipient") or ""), ti.get("message")
        if not owner or not isinstance(text, str):
            return None
        if _is_follow_up(cfg, owner, text):
            return chase_owner(cfg, owner, str(ti.get("summary") or text), now)
        asked = asked_refs(cfg, text, str(ti.get("summary") or ""))
        if _is_reply(cfg, owner, text, asked, now) or not is_work(
            cfg, owner, text, asked
        ):
            return None
        summary = str(ti.get("summary") or text.splitlines()[0])[:120]
        chase = is_chase(cfg, text)
    elif tool == "Agent" and ti.get(
        "run_in_background"
    ):  # a foreground agent hands its result back in-turn
        text = str(ti.get("prompt") or "")
        if not is_deliverable_brief(cfg, str(ti.get("description") or ""), text):
            return None
        found = re.search(r"agentId: (\w+)", resp) if isinstance(resp, str) else None
        ident = (
            resp.get("agentId")
            if isinstance(resp, dict)
            else (found.group(1) if found else None)
        )
        owner = str(ident or "agent:" + str(ti.get("description", "")))
        summary = str(ti.get("description") or text.splitlines()[0])[:120]
    else:
        return None
    every = asked_refs(cfg, text, summary)
    queue = (
        every if len(every) > 1 else []
    )  # one PR per clock: several PRs are a queue, the first is current
    prs = every[:1] if queue else every
    heads: dict[str, str] = {}
    authored = False
    deadline = time.time() + 3.0  # the hook is synchronous: 3 s in total for baselines
    for url in prs:
        info = pr_info(cfg, url, deadline - time.time())
        if info:
            heads[url] = info["head"]
            authored = authored or owner_is_author(cfg, owner, info["author"])
    return record(
        cfg,
        owner,
        summary,
        prs,
        heads,
        tool_use_id,
        now,
        bool(prs) and not authored and not chase and is_review(cfg, text, summary),
        chase,
        bool(DELIVERABLE.search(text)),
        queue,
        queued_behind(text, summary),
        pushes_to_pr(cfg, text, summary) and not chase,
        from_estimate(text),
    )


# ---- clocks and the ladder ----


def started_at(cfg: dict, item: dict) -> float:
    """When the owner is expected to start: the ask, or for work queued behind other work, `queued_wait_min` later
    (until a review shows on the PR)."""
    t = item.get("routed_at") or item["added"]
    return t + float(opts(cfg)["queued_wait_min"]) * 60 if item.get("waiting") else t


def base(cfg: dict, item: dict) -> float:
    """Last word from the owner: the later of the start and last heard."""
    return max(started_at(cfg, item), item.get("last_heard") or 0.0)


def limit(cfg: dict, item: dict) -> float:
    """Stall limit in minutes: reviews are held to the shorter review clock."""
    o = opts(cfg)
    return float(o["review_stall_min"] if item.get("review") else o["stall_min"])


def overdue(cfg: dict, item: dict, now: float) -> int | None:
    """Minutes since the ask when a review passes its hard limit (hearing from the owner does not stop it)."""
    age = int((now - started_at(cfg, item)) / 60)
    hard = float(opts(cfg)["review_overdue_min"])
    if item.get("estimate_min") is not None and not item.get("estimate_defaulted"):
        hard = max(
            hard, float(item["estimate_min"])
        )  # the job's own stated estimate sets its limit
    return age if item.get("review") and age > hard else None


def level(cfg: dict, item: dict, now: float) -> int | None:
    """Ladder step by silence: 0 past 1x the limit, 1 past 2x, 2 past 3x; None while within the limit."""
    lim = limit(cfg, item)
    idle = (now - base(cfg, item)) / 60
    return None if idle <= lim else min(int(idle // lim) - 1, 2)


def eff_level(cfg: dict, item: dict, now: float) -> int | None:
    """level(), but a review past its hard limit counts as at least step 1."""
    lvl = level(cfg, item, now)
    return (
        max(lvl if lvl is not None else 0, 1)
        if overdue(cfg, item, now) is not None
        else lvl
    )


def needs_action(cfg: dict, item: dict, now: float) -> bool:
    """Something the hub must do now: a moved head, a verdict on an old head, or a silence not yet chased at this step."""
    if item.get("head_moved") or item.get("verdict_old_head"):
        return True
    from . import watcher

    lvl = eff_level(cfg, item, now)
    return (
        lvl is not None
        and (item.get("chased_level") is None or item["chased_level"] < lvl)
        and watcher.excused(cfg, item, now) is None
    )


def _pr_no(ref: str) -> str:
    return "#" + pr_num(ref)


def _label(item: dict) -> str:
    prs = " ".join(_pr_no(u) for u in item["prs"])
    if item.get("queue"):
        prs += f" (+{len(item['queue']) - 1} queued)"
    return f"{item['id']} ({item['title'][:50]}{' ' + prs if prs else ''})"


def next_action(cfg: dict, item: dict, now: float) -> str:
    """The single next action for one item."""
    o = item["owner"]
    m, v = item.get("head_moved"), item.get("verdict_old_head")
    if m:
        return f"SendMessage {o}: {_pr_no(m['url'])} head moved {m['old'][:7]} -> {m['new'][:7]}; ask for a re-check at the new head"
    if v:
        return f"SendMessage {o}: the {v['state']} review landed at {v['commit'][:7]} but the head is {v['head'][:7]}; ask for a re-review at the head"
    lvl = eff_level(cfg, item, now)
    if item.get("reported") and not item.get("review"):
        prs = " ".join(_pr_no(u) for u in item["prs"])
        return (
            f"{o} reported it done {int((now - item['reported']) / 60)} min ago; that is a claim. Check the artifact"
            f"{' (' + prs + ')' if prs else ''}, then: hubkit work done {item['id']} --note \"<what you checked>\""
        )
    if lvl is not None:
        if item.get("chased_level") is not None and item["chased_level"] >= lvl:
            nxt = base(cfg, item) + limit(cfg, item) * 60 * (lvl + 2)
            return f"chased {item.get('chases', 0)}x, waiting; escalate to step {lvl + 1} at {store.clock(cfg, nxt)}"
        heard = item.get("last_heard")
        if lvl >= 1 and heard and now - heard < limit(cfg, item) * 60:
            return f"SendMessage {o} for an ETA on {' '.join(_pr_no(u) for u in item['prs']) or 'the work'}; it reported {int((now - heard) / 60)} min ago, so do not re-brief it"
        steps = (
            f"SendMessage {o} for a one-line state",
            "re-brief this work to another idle peer or a fresh agent (ListAgents first); the new brief opens its own item",
            "tell the user in the next report in one line (no card), then decide the executor",
        )
        return steps[lvl]
    if item.get("pr_unknown"):
        if any(not u.startswith("http") for u in item["pr_unknown"]):
            return "a PR is a bare #N and routed.repo is not set: set [routed] repo in config.toml (or work add --pr with the full URL)"
        return (
            "gh could not read the PR: have an agent read it; nothing is known about it"
        )
    if item.get("waiting") and now < started_at(cfg, item):
        return f"queued behind other work: no clock until {store.clock(cfg, started_at(cfg, item))} or until a review shows on the PR; then chase {o}"
    return f"wait for {o}; next check {store.clock(cfg, base(cfg, item) + limit(cfg, item) * 60)} (limit {int(limit(cfg, item))} min)"


def _why(cfg: dict, item: dict, now: float) -> str:
    if item.get("head_moved"):
        m = item["head_moved"]
        return f"{_pr_no(m['url'])} head moved {m['old'][:7]} -> {m['new'][:7]} after the ask"
    if item.get("verdict_old_head"):
        return "the review landed on an old head"
    late = overdue(cfg, item, now)
    idle = int((now - base(cfg, item)) / 60)
    if late is not None:
        return f"review asked {late} min ago, past the hard limit of {opts(cfg)['review_overdue_min']} min"
    return f"nothing for {idle} min (limit {int(limit(cfg, item))})"


def prompt_lines(cfg: dict, now: float) -> list[str]:
    """One line per owner that needs the hub, most urgent first. The action is always one NEXT."""
    by_owner: dict[str, list[dict]] = {}
    for i in items(cfg):
        if needs_action(cfg, i, now):
            by_owner.setdefault(i["owner"], []).append(i)
    out = []
    for owner, its in by_owner.items():
        tag = (
            "OVERDUE"
            if any(overdue(cfg, i, now) is not None for i in its)
            else "STALLED"
        )
        facts = "; ".join(f"{_label(i)}: {_why(cfg, i, now)}" for i in its)
        out.append(
            f"hubkit {tag}: {owner}: {facts}. NEXT: {next_action(cfg, its[0], now)}"
        )
    return out


def slow_why(cfg: dict, item: dict, now: float) -> str:
    """What the holder is doing, from real signals (see slow.why)."""
    from . import slow

    return slow.why(cfg, item["owner"], item, now, False)[0]


def alerts(cfg: dict, now: float) -> list[dict]:
    """Tick alerts, one per item and ladder step; back-off is applied by the tick."""
    out = []
    for i in items(cfg):
        m = i.get("head_moved")
        if m:
            out.append(
                {
                    "key": f"{i['id']}:head_moved:{m['new'][:8]}",
                    "msg": (
                        f"{_label(i)} to {i['owner']}: {_why(cfg, i, now)}. NEXT: {next_action(cfg, i, now)}"
                    ),
                }
            )
        lvl = eff_level(cfg, i, now)
        if lvl is not None and needs_action(
            cfg, i, now
        ):  # a chased item waits: no alert, no wake-up
            out.append(
                {
                    "key": f"{i['id']}:routed_stalled:L{lvl}",
                    "msg": (
                        f"{_label(i)} to {i['owner']}: {_why(cfg, i, now)}; holder: {slow_why(cfg, i, now)}. NEXT: {next_action(cfg, i, now)}"
                    ),
                }
            )
    return out


def block_lines(cfg: dict, work: list[dict], now: float) -> list[str]:
    """Status-block lines for routed work: owner, age, last heard and the NEXT action."""
    mine = [i for i in work if i.get("kind") == "routed"]
    if not mine:
        return []
    out = ["routed work (owner, age, last heard, NEXT):"]
    for i in mine:
        heard = int((now - i["last_heard"]) / 60) if i.get("last_heard") else None
        pr = ", ".join(
            f"{_pr_no(u)}@{i['heads'].get(u, '?')[:7]}" for u in i.get("prs", [])
        )
        flags = (
            (f" +{len(i['queue']) - 1} queued" if i.get("queue") else "")
            + (" REVIEW" if i.get("review") else "")
            + (" PR unknown" if i.get("pr_unknown") else "")
        )
        out.append(
            f"  {i['id']} -> {i['owner']}: {i['title'][:50]}{' [' + pr + ']' if pr else ''}{flags}; "
            f"age {int((now - i.get('routed_at', i['added'])) / 60)}m, last heard "
            f"{'never' if heard is None else str(heard) + 'm ago'}, chased {i.get('chases', 0)}x"
        )
        out.append(f"      NEXT: {next_action(cfg, i, now)}")
    return out


# ---- heard-from ----


def heard(cfg: dict, events: list[tuple[float, str, str]]) -> int:
    """events: (epoch, from-name or "", free text). Marks items whose owner or tool use id is named."""
    if not events:
        return 0
    _note_peers(cfg, events)
    sd = store.state(cfg)
    n = 0
    with store.locked(sd / "work.lock"):
        all_ = work_load(sd)
        for i in all_:
            if i.get("kind") != "routed":
                continue
            for ts, name, text in events:
                by_text = not name and (
                    i["owner"] in text
                    or bool(i.get("tool_use_id") and i["tool_use_id"] in text)
                )
                if (
                    by_text
                    and COMPLETED.search(text)
                    and ts > (i.get("routed_at") or i["added"])
                ):
                    i["reported"] = ts  # the agent finished after this ask and reported
                    if not i.get(
                        "review"
                    ):  # a review item stays: the review check on the PR decides
                        i["answered"] = True
                        i["done_note"] = "agent completed (task notification)"
                from_owner = bool(name) and name.lower() == i["owner"].lower()
                if (
                    from_owner
                    and not i.get("review")
                    and ts > (i.get("routed_at") or i["added"])
                    and is_report(text)
                    and _report_is_about(all_, i, text)
                ):
                    i["reported"] = (
                        ts  # a claim, not evidence: the item closes on the artifact (refresh_prs) or on `work done`
                    )
                    i["claimed"] = text[:160]
                    n += 1  # a change to save even when no last_heard moved
                if (name.lower() == i["owner"].lower() or by_text) and ts > (
                    i.get("last_heard") or 0
                ):
                    i["last_heard"] = ts
                    n += 1
                    if i.get("state_ask") and ts > (i.get("routed_at") or 0):
                        i["answered"] = (
                            True  # a state question is answered by one reply
                        )
        closing = [x for x in all_ if x.get("answered")]
        for i in closing:
            _archive(
                cfg, i, i.get("done_note") or "state question answered", time.time()
            )
            all_.remove(i)
        if n or closing:
            work_save(sd, all_)
    return n


COMPLETED = re.compile(r"<status>\s*completed\s*</status>")
PEER_BODY = re.compile(
    r"<cross-session-message[^>]*>(.*?)(?:</cross-session-message>|\Z)", re.S
)
PEER_RESULT = re.compile(
    r"(?i)\b(done|finished|complete[d]?|merged|pushed|passed|passing|green|fixed|shipped|result|ready for)\b"
)
PEER_NOT_RESULT = re.compile(
    r"(?i)\b(blocked|failed|failing|cannot|can't|couldn't|unable|error|stuck|waiting)\b"
)


def _report_is_about(all_: list[dict], item: dict, text: str) -> bool:
    """A report closes the owner's only item; when the owner holds several, it must name this item's PR or id."""
    mine = [
        x for x in all_ if x.get("kind") == "routed" and x["owner"] == item["owner"]
    ]
    return (
        len(mine) == 1
        or any(pr_num(u) in re.findall(r"\d+", text) for u in item["prs"])
        or item["id"] in text
    )


def is_report(text: str) -> bool:
    """A peer's message reports a result: a result word, no blocker word, and it does not end on a question."""
    m = PEER_BODY.search(text)
    body = (m[1] if m else text).strip()
    return (
        bool(body)
        and not body.endswith("?")
        and bool(PEER_RESULT.search(body))
        and not PEER_NOT_RESULT.search(body)
    )


def peer_name(text: str) -> str:
    """Name of the peer a message came from, or an empty string."""
    m = FROM_NAME.search(text)
    return m.group(1) if m else ""


def _entry_event(o: dict) -> tuple[str, str] | None:
    """(from-name, text) of a peer message or task notification entry in a transcript, else None.

    Two shapes carry them. A queue-operation entry holds the raw text the moment the message is queued
    (while the hub is busy, this is the only entry for a long time). A user entry with origin.kind
    peer / task-notification is written when the hub takes the message up.
    """
    if o.get("type") == "queue-operation":
        text = o.get("content") if o.get("operation") == "enqueue" else None
        text = text.lstrip() if isinstance(text, str) else ""
        if text.startswith("<cross-session-message"):
            return peer_name(text), text
        return ("", text) if text.startswith("<task-notification") else None
    kind = (o.get("origin") or {}).get("kind")
    if o.get("type") != "user" or kind not in ("peer", "task-notification"):
        return None
    c = o.get("message", {}).get("content")
    text = c if isinstance(c, str) else json.dumps(c)
    return (peer_name(text), text) if kind == "peer" else ("", text)


def scan_transcript(cfg: dict, path: str | Path | None) -> int:
    """Incremental scan of the hub transcript: peer messages and task notifications are 'heard from'."""
    if not path:
        return 0
    p = Path(path)
    sp = store.state(cfg) / "routed-scan.json"
    try:
        size = p.stat().st_size
    except OSError:
        return 0
    try:
        seen = json.loads(sp.read_text(encoding="utf-8"))
    except ValueError, OSError:
        seen = {}
    prev = seen.get(str(p))
    first = not isinstance(prev, int) or prev > size
    off: int = max(0, size - SCAN_TAIL) if first or not isinstance(prev, int) else prev
    with open(p, "rb") as fh:
        fh.seek(off)
        data = fh.read()
    cut = data.rfind(b"\n") + 1  # only whole lines
    lines = data[:cut].decode("utf-8", "replace").splitlines()
    if first and off > 0 and lines:
        lines = lines[1:]  # the first line may start mid-record
    events = []
    for line in lines:
        if "cross-session-message" not in line and "task-notification" not in line:
            continue
        try:
            o = json.loads(line)
            hit = _entry_event(o)
            if hit:
                events.append((_epoch(o["timestamp"]), *hit))
        except ValueError, KeyError:
            continue
    seen[str(p)] = off + cut
    store.write_atomic(sp, json.dumps(seen))
    return heard(cfg, events)


def transcript_of(cfg: dict, session_id: str) -> str | None:
    """Path of the transcript file for a session id, or None."""
    for f in glob.glob(os.path.expanduser(cfg["paths"]["transcripts_glob"])):
        if os.path.basename(f) == f"{session_id}.jsonl":
            return f
    return None


# ---- PR check, once per PR per tick ----


def _archive(cfg: dict, item: dict, note: str, now: float) -> None:
    day = store.day_of(cfg, now)
    store.append_row(
        store.state(cfg) / "archive" / f"archive_{day}.jsonl",
        {"type": "work", **item, "outcome": "done", "done_note": note, "ts": now},
    )


def _landed(
    cfg: dict, item: dict, got: dict[str, dict | None]
) -> dict[str, tuple[str, dict | None]]:
    """Per PR of a review item: ('closed'|'old'|'', detail). The newest review since the ask, from someone but the
    author, judged against that PR's current head."""
    out: dict[str, tuple[str, dict | None]] = {}
    for u in item["prs"]:
        info = got.get(u)
        revs = (
            [
                r
                for r in (info["reviews"] if info else [])
                if r["at"] >= (item.get("routed_at") or item["added"])
                and counts_as_review(cfg, r, info)
            ]
            if info
            else []
        )
        if not info or not revs:
            out[u] = ("", None)
            continue
        last = max(revs, key=lambda r: r["at"])
        if same_head(last["commit"], info["head"]):
            out[u] = ("closed", {**last, "url": u, "ci": info.get("ci", "none")})
        else:
            out[u] = ("old", {**last, "head": info["head"], "url": u})
    return out


HEX_HEAD = re.compile(r"[0-9a-f]{7,40}")
AGENT_ID = re.compile(r"^[0-9a-f]{12,}$")


def agent_activity(cfg: dict, now: float) -> int:
    """A background agent whose transcript or output file is still growing is not silent: count the file's
    modification time as heard-from. Returns the number of items refreshed."""
    sd = store.state(cfg)
    pats = opts(cfg)["activity_globs"]
    n = 0
    with store.locked(sd / "work.lock"):
        all_ = work_load(sd)
        for i in all_:
            if i.get("kind") != "routed" or not AGENT_ID.match(i["owner"]):
                continue
            stamps = [
                m
                for pat in pats
                for f in glob.glob(os.path.expanduser(pat.format(id=i["owner"])))
                if (m := _mtime(f)) is not None
            ]
            seen = min(max(stamps, default=0.0), now)
            if seen > base(cfg, i):
                i["last_heard"] = seen
                n += 1
        if n:
            work_save(sd, all_)
    return n


def _mtime(path: str) -> float | None:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _advance(item: dict, det: dict, now: float) -> dict:
    """A queue item's current PR has its review: move to the next PR with a fresh clock."""
    done, rest = item["queue"][0], item["queue"][1:]
    item["queue"] = rest if len(rest) > 1 else []
    item["prs"] = rest[:1]
    item["heads"] = {}
    item.update(
        routed_at=now,
        head_moved=None,
        verdict_old_head=None,
        verdict_acked=None,
        chased_level=None,
    )
    _event(
        item,
        "advance",
        f"{_pr_no(done)} reviewed; now {_pr_no(rest[0]) if rest else 'none'}",
        now,
    )
    return {
        "key": f"{item['id']}:queue_advanced:{pr_num(done)}",
        "msg": (
            f"{item['id']} to {item['owner']}: {_pr_no(done)} {det['state']} at {det['commit'][:7]}; the clock restarts on the next PR. NEXT: read the review on GitHub and act on it"
        ),
    }


def artifact_exists(item: dict, url: str, info: dict) -> bool:
    """The PR shows what the owner claims: merged or closed, or a head that moved since the first ask and CI not failing."""
    if info.get("state") in ("MERGED", "CLOSED"):
        return True
    first = str((item.get("heads0") or {}).get(url) or "")
    return (
        bool(first)
        and not same_head(first, info["head"])
        and info.get("ci", "none") != "failing"
    )


def next_step(cfg: dict, d: dict, owner: str) -> str:
    """The concrete step after a review lands, by verdict and CI at that head."""
    state, ci = str(d.get("state", "")).upper(), d.get("ci", "none")
    sha = str(d.get("commit", ""))[:7]
    if state in ("MERGED", "CLOSED"):
        return f"the PR is {state.lower()}; tell {owner} nothing more is owed and report it"
    if state == "REPORTED":
        return f"{owner}'s claim is backed by the PR (merged, or the head moved with CI {ci}); report it to the user"
    if state == "APPROVED":
        if ci in ("failing", "pending"):
            return f"approved at {sha} but CI is {ci}: have {owner} fix or wait for CI before any merge; do not merge on red"
        return f"approved at {sha}, CI {ci}: if the user ordered a merge, merge with --match-head-commit {sha}; otherwise report the approval and wait for his word"
    if state == "CHANGES_REQUESTED":
        return f"changes requested: send {owner} the findings with the PR's comment links and ask for a push; chase it as a new item"
    return f"read the review at {sha}, then send {owner} the next step or report the verdict"


def _reply_owed(cfg: dict, item: dict, done: dict[str, dict]) -> str:
    """Names the people who asked the hub for this review (inbound reply items on the same PRs)."""
    who = sorted(
        {
            x["owner"]
            for x in work_load(store.state(cfg))
            if x.get("kind") == "reply" and overlaps(list(done), x.get("prs", []))
        }
    )
    return f" Reply owed to: {', '.join(who)}." if who else ""


def _close_landed(cfg: dict, item: dict, done: dict[str, dict], now: float) -> dict:
    """Reviews landed at the head for some PRs of a (non-queue) item: drop them from the item and report the gaps.
    The item is archived when no PR is left."""
    item["prs"] = [u for u in item["prs"] if u not in done]
    for u in done:
        item["heads"].pop(u, None)
    note = "; ".join(
        f"{_pr_no(u)} {d['state']} by {d['login']} at {d['commit'][:7]} (the head), CI {d.get('ci', 'none')}"
        for u, d in done.items()
    )
    gap = (
        f" Still open: {' '.join(_pr_no(u) for u in item['prs'])} (no review at its head yet)."
        if item["prs"]
        else ""
    )
    if not item["prs"]:
        _archive(cfg, item, "review " + note, now)
    first = next(iter(done.values()))
    return {
        "key": f"{item['id']}:review_landed:{pr_num(next(iter(done)))}",
        "msg": (
            f"{item['id']} to {item['owner']}: {note}.{gap}{_reply_owed(cfg, item, done)} NEXT: {next_step(cfg, first, item['owner'])}"
        ),
    }


def resolve_bare_refs(cfg: dict) -> None:
    """Items recorded before `routed.repo` was set (or by hand) hold bare #N refs gh cannot read: resolve them."""
    repo = str(opts(cfg).get("repo", ""))
    if not repo:
        return

    def fix(u: str) -> str:
        return (
            u if u.startswith("http") else f"https://github.com/{repo}/pull/{pr_num(u)}"
        )

    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        all_ = work_load(sd)
        if not any(
            not u.startswith("http")
            for i in all_
            if i.get("kind") == "routed"
            for u in [*i["prs"], *i.get("queue", [])]
        ):
            return
        for i in all_:
            if i.get("kind") == "routed":
                i["prs"] = [fix(u) for u in i["prs"]]
                i["queue"] = [fix(u) for u in i.get("queue", [])]
                i["heads"] = {fix(u): h for u, h in i["heads"].items()}
        work_save(sd, all_)


def refresh_prs(cfg: dict, now: float) -> list[dict]:
    """Read head and reviews with gh once per PR. A failure marks the PRs unknown. A moved head is recorded for
    the alert (it does not reset the stall clock: the pusher is not known). A review after the ask counts as
    movement. A review item closes when a review lands at the PR's current head; a review on an older head never
    closes it. Returns the alerts for items closed here."""
    sd = store.state(cfg)
    resolve_bare_refs(cfg)
    todo = {u for i in items(cfg) for u in i["prs"]}
    if not todo:
        return []
    timeout = float(opts(cfg)["gh_timeout_s"])
    got = {u: pr_info(cfg, u, timeout) for u in sorted(todo)}
    msgs: list[dict] = []
    with store.locked(sd / "work.lock"):
        all_ = work_load(sd)
        keep = []
        for i in all_:
            if i.get("kind") != "routed":
                keep.append(i)
                continue
            i["pr_unknown"] = [u for u in i["prs"] if got.get(u) is None]
            for u in i["prs"]:
                info = got.get(u)
                if not info:
                    continue
                old = i["heads"].get(u)
                if not (
                    old and HEX_HEAD.fullmatch(old)
                ):  # unset, or mangled by an older redaction: rebaseline
                    i["heads"][u] = info["head"]
                elif owner_is_author(cfg, i["owner"], info["author"]):
                    i["review"] = (
                        False  # the person asked wrote the PR: their pushes are the work, not a stale review
                    )
                    i["heads"][u] = info["head"]
                elif i.get("pushes") and not i.get("review"):
                    i["heads"][u] = info[
                        "head"
                    ]  # the owner was asked to push to this PR: its pushes are the work
                elif owner_is_author(cfg, i["owner"], info.get("head_author", "")):
                    i["heads"][u] = info[
                        "head"
                    ]  # the head moved by the owner's own commit: progress, not a stray push
                    i["last_heard"] = max(i.get("last_heard") or 0, now)
                elif not same_head(old, info["head"]):
                    i["head_moved"] = {
                        "url": u,
                        "old": old,
                        "new": info["head"],
                        "at": now,
                    }
                    _event(i, "head_moved", f"{old[:7]} -> {info['head'][:7]}", now)
                    i["heads"][u] = info["head"]
                if (
                    i.get("waiting")
                    and info["review_at"]
                    and info["review_at"] > (i.get("routed_at") or 0)
                ):
                    i["waiting"] = (
                        False  # a review shows on the PR: the owner has started it
                    )
                if info["review_at"] and info["review_at"] > base(cfg, i):
                    i["last_heard"] = info["review_at"]
            res = _landed(cfg, i, got) if i.get("review") else {}
            old = next((d for v, d in res.values() if v == "old" and d), None)
            acked = i.get("verdict_acked")
            i["verdict_old_head"] = (
                old if old and acked != [old["commit"], old["head"]] else None
            )
            done = {u: d for u, (v, d) in res.items() if v == "closed" and d}
            if i.get("reported") and not i.get(
                "review"
            ):  # the owner said done: close only on its artifact
                for u in i["prs"]:
                    info = got.get(u)
                    if info and artifact_exists(i, u, info):
                        done[u] = {
                            "state": "REPORTED",
                            "login": i["owner"],
                            "commit": info["head"],
                            "ci": info.get("ci", "none"),
                        }
            if i.get("review"):  # a merged or closed PR needs no review any more
                for u in i["prs"]:
                    info = got.get(u)
                    if (
                        u not in done
                        and info
                        and info.get("state") in ("MERGED", "CLOSED")
                    ):
                        done[u] = {
                            "state": info["state"],
                            "login": "GitHub",
                            "commit": info["head"],
                            "ci": info.get("ci", "none"),
                        }
            if done and i.get("queue"):
                cur = i["prs"][0]
                if cur in done:
                    msgs.append(_advance(i, done[cur], now))
            elif done:
                msgs.append(_close_landed(cfg, i, done, now))
                if not i["prs"]:
                    continue
            keep.append(i)
        work_save(sd, keep)
    return msgs


def close_asks(cfg: dict, now: float) -> int:
    """The user answered: every open ask-the-user item is done."""
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        all_ = work_load(sd)
        done = [i for i in all_ if i.get("kind") == "ask"]
        for i in done:
            _archive(cfg, i, "answered", now)
        if done:
            work_save(sd, [i for i in all_ if i.get("kind") != "ask"])
    return len(done)
