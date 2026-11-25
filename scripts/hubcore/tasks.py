"""Tasks the user gives: typed words that open a priority item per task, held open until the user or evidence finishes them.

A task is a `priority` work item with `task: true`. The prompt hook opens one per task a human prompt gives
("tasks: ...", "todo", a numbered list introduced as tasks); a question, a mention, peer or harness text and pasted
content open nothing. A task authorises no merge, release, deploy or message. It closes on the user's typed word naming
it, or with `hubkit work done ID --evidence <merged PR URL|file:path>` once a done-when criterion exists. Each task lists
its workers (filled from the SendMessage/Agent hook when a brief names it), is checked on its own interval
(`[tasks] interval_min`, 15; `hubkit tasks interval N`; per task `hubkit work every ID --minutes N`), and holds the Stop
gate while it has no live worker inside its ETA, no ask waiting on the user and no named blocker.

Fast track means first in every list, a shorter clock, a re-brief to another owner after one missed nudge, and never
queued behind other work. It never bypasses a guard, a permission prompt, a denial or any deploy, merge or message rule.
hubkit sends nothing: the hub reads the nudge and sends it.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import slow, status, store
from .redact import redact_obj

NOT_AN_ORDER = "NOT an order: it authorises no merge, release, deploy or message"
WITHIN, WAITING, IDLE, OVERDUE, NOWORKER = "reported-within-ETA", "waiting-on-permission-prompt", "idle", "overdue", "no-worker"
TASK_BLOCKS = 2  # the stop gate blocks this often for the same task reason in one turn, then lets the turn end with a line
OVERRIDE_FILE = "tasks.json"


def _mins(t0: float, t1: float) -> float:
    return max(0.0, (t1 - t0) / 60)


def _nn(name: object) -> str:
    return re.sub(r"\[[^\]]*\]\s*$", "", str(name)).strip().lower()


# ---- what the user gives (typed prompt only; the hook has already dropped peer, harness and pasted text) ----


def _forms(base: str) -> set[str]:
    """A word and its typos: one letter swapped with its neighbour, doubled, or dropped (words of 4+ letters only)."""
    out = {base}
    out |= {base[:i] + base[i + 1] + base[i] + base[i + 2 :] for i in range(len(base) - 1)}
    out |= {base[: i + 1] + base[i] + base[i + 1 :] for i in range(len(base))}
    out |= {base[:i] + base[i + 1 :] for i in range(len(base))}
    return {w for w in out if len(w) >= 4} - {"asks", "tass"}


_TASK_FORMS = _forms("task") | _forms("tasks") | {"todo", "todos", "todoo", "todoos"}
TASK_WORD = re.compile(
    r"(?i)(?<![\w/.-])(?:" + "|".join(sorted(map(re.escape, _TASK_FORMS), key=len, reverse=True)) + r"|to-dos?)(?![\w-]|\.\w|/)"
)
_QUOTED = re.compile(
    r"```.*?```|`[^`\n]*`|\"[^\"\n]*\"|“[^”\n]*”"
    r"|(?<![A-Za-z0-9])'[^'\n]*'(?![A-Za-z0-9])"  # 'quoted'; an apostrophe (don't, Ankit's) is not a quote
    r"|(?m:^[ \t]*(?:>|&gt;)[^\n]*)",  # a quoted reply line
    re.S,
)
_ASKING = {
    "what", "which", "how", "why", "show", "list", "status", "any", "where", "when", "who", "tell", "report", "many",
    "did", "does", "is", "are", "was", "were", "have", "has", "about", "if", "whether", "count", "check", "see", "read",
}  # fmt: skip
_QUESTION_START = {
    "what", "which", "how", "why", "where", "when", "who", "is", "are", "was", "were", "can", "could", "should",
    "would", "will", "any", "show", "list", "tell", "has", "have", "did", "does",
}  # fmt: skip
_GIVE = re.compile(
    r"(?i)\b(?:give|giving|gave|assign|assigning|add|adding|create|creating|new|here(?:'s|\s+(?:is|are))|these|following"
    r"|i\s+have|i've\s+got|got|do|finish|complete|handle|take|work\s+on)\b"
)
_QUALIFIERS = {"new", "a", "an", "the", "my", "your", "our", "one", "another", "extra", "next", "more", "few", "some", "please", "also"}
_STATEMENT_AFTER = re.compile(
    r"(?i)^\s+(?:is|are|was|were|has|have|had|will|status|list|count|finished|done|running|open|pending|queue)\b"
)
LISTMARK = re.compile(r"^\s*(?:\d{1,2}[.)]|\(\d{1,2}\)|[-*•])\s+(\S.*)$")
_INLINE_NUM = re.compile(r"(?:^|(?<=\s))\(?(\d{1,2})[.)]\s+")
DONE_WHEN = re.compile(r"(?is)\b(?:done|finish(?:ed)?|complete(?:d)?)\s+when\b[:,]?\s*(?P<c>.+)$")
MAX_TASKS = 12


def _mask(text: str) -> str:
    """The text with quoted and code spans blanked (same length): a quoted "tasks" is a mention, not a gift."""
    return _QUOTED.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", s.lower())


def _locate(text: str) -> tuple[re.Match, str] | None:
    """The task word of a prompt that GIVES tasks, with the form it was given in ("colon", "list", "give", "lead"); None for
    a question, a status ask or any other mention."""
    masked = _mask(text)
    for m in TASK_WORD.finditer(masked):
        if m.start() > 200:
            break
        ls = masked.rfind("\n", 0, m.start()) + 1
        le = masked.find("\n", m.end())
        le = len(masked) if le < 0 else le
        pre = re.split(r"[.!?]", masked[ls : m.start()])[-1]
        post = masked[m.end() : le]
        pw = _words(re.sub(r"(?i)\bhere(?:'s|\s+(?:is|are))\b|\bthese\s+are\b", " ", pre))
        if masked[ls:le].rstrip().endswith("?") or (pw and pw[0] in _QUESTION_START) or set(pw) & _ASKING:
            continue
        if len(pw) <= 5 and re.match(r"[^.!?:\n]{0,40}(?::|\s[-–—]\s)", post):
            return m, "colon"
        nxt = next((x for x in text[le:].splitlines() if x.strip()), "")
        if LISTMARK.match(nxt):
            return m, "list"
        if re.search(rf"(?i)\b(?:is|are|was)\b[^.\n]{{0,20}}{_FIN}\b", post):
            continue  # "task 5 is done" is a report on a task, not a gift
        first_line = masked[:ls].strip() == ""
        if first_line and set(pw) <= _QUALIFIERS and post.strip() and not _STATEMENT_AFTER.match(post) and not post.lstrip().startswith(","):
            return m, "lead"
        if _GIVE.search(pre[-40:]) and not _STATEMENT_AFTER.match(post):
            return m, "give"
    return None


def _norm(s: str) -> str:
    return " ".join(_words(s))


def _numbered(first: str) -> list[str]:
    """"1. a 2. b" on one line -> ["a", "b"] (needs at least two, counting from 1); else []."""
    marks = list(_INLINE_NUM.finditer(first))
    if len(marks) < 2 or marks[0].group(1) != "1":
        return []
    ends = [m.start() for m in marks[1:]] + [len(first)]
    return [first[m.end() : e].strip() for m, e in zip(marks, ends, strict=True) if first[m.end() : e].strip()]


def _criterion(piece: str) -> tuple[str, str]:
    """(task text, done-when text) of one piece; the criterion is "" when the piece names none."""
    m = DONE_WHEN.search(piece)
    if not m:
        return piece.strip(), ""
    return piece[: m.start()].strip(" ,;.-"), m.group("c").strip(" .;")


def parse_tasks(text: str) -> list[dict]:
    """The tasks a prompt gives: [{"title", "done_when"}]. A list is split into one task each; a prompt that gives tasks but
    names none makes one task quoting the prompt. Empty for a question, a status ask or any other mention."""
    loc = _locate(text)
    if loc is None:
        return []
    m, form = loc
    le = text.find("\n", m.end())
    le = len(text) if le < 0 else le
    rest = text[m.end() : le]
    if form == "colon":
        first = rest.split(":", 1)[-1] if ":" in rest else re.sub(r"^\s*[-–—]\s*", "", rest)
    elif form == "lead":
        first = rest
    else:
        first = ""
    items: list[str] = []
    default = ""
    marked = False
    for ln in text[le:].splitlines():
        lm = LISTMARK.match(ln)
        if lm:
            items.append(lm.group(1).strip())
            marked = True
        elif ln.strip() and re.match(r"(?i)\s*(?:done|finish(?:ed)?|complete(?:d)?)\s+when\b", ln):
            default = _criterion(ln)[1]
        elif ln.strip() and ln[:1] in " \t" and items:
            items[-1] += " " + ln.strip()
    if not marked:
        tail = re.search(r"(?is)[;.]\s*((?:done|finish(?:ed)?|complete(?:d)?)\s+when\b.*)$", first)
        if tail:  # "1. a 2. b; done when X": after a ; or . it covers every task of the line
            default = default or _criterion(tail.group(1))[1]
            first = first[: tail.start()]
        items = _numbered(first) or [x.strip() for x in first.split(";") if x.strip()]
    elif (c := _criterion(first)[1]) and not default:
        default = c
    found: list[dict] = []
    for piece in items:
        title, crit = _criterion(piece)
        if not title and crit:
            default = default or crit  # "...; done when X" is the criterion of the tasks before it
        elif title:
            found.append({"title": title[:160], "done_when": crit})
    if not found:
        title, crit = _criterion(" ".join(text.split()))
        found = [{"title": (title or " ".join(text.split()))[:160], "done_when": crit}]
    out = []
    for t in found[:MAX_TASKS]:
        own = bool(t["done_when"])
        out.append({**t, "done_when": t["done_when"] or default, "shared": 0 if own or not default else 1})
    n = sum(t["shared"] for t in out)
    return [{**t, "shared": n if t["shared"] and n > 1 else 0} for t in out]


def open_tasks(cfg: dict) -> list[dict]:
    """Task items that are not on hold: the ones the stop gate, the checks and the lists count."""
    return [i for i in status.work_load(store.state(cfg)) if i.get("task") and not i.get("held")]


def task_lead_items(cfg: dict) -> list[dict]:
    return [i for i in open_tasks(cfg) if not i.get("first_action")]


def task_intake(cfg: dict, text: str, now: float) -> list[str]:
    """Open one priority item per task a human prompt gives; ids of the NEW items. A task given again (same words) is not
    duplicated, and one he had put on hold is back on. Items authorise no merge, release, deploy or message."""
    found = parse_tasks(text)
    if not found:
        return []
    quote = redact_obj({"q": " ".join(text.split())[:160]})["q"]
    sd = store.state(cfg)
    new: list[str] = []
    with store.locked(sd / "work.lock"):
        items = status.work_load(sd)
        for t in found:
            key = _norm(t["title"])
            hit = next((i for i in items if i.get("task") and _norm(i["title"]) == key), None)
            if hit is not None:
                hit.pop("held", None)
                hit["prompts"] = int(hit.get("prompts", 1)) + 1
                hit["done_when"] = hit.get("done_when") or redact_obj({"c": t["done_when"]})["c"]
                continue
            item = redact_obj(
                {
                    "id": status.next_work_id(sd, items),
                    "kind": "priority",
                    "task": True,
                    "lead": True,
                    "title": t["title"],
                    "owner": cfg["user"]["name"],
                    "note": NOT_AN_ORDER + ". Open until he marks it finished or its done-when is proven with evidence.",
                    "added": now,
                    "checked": now,
                    "quotes": [quote],
                    "prompts": 1,
                    "workers": [],
                    "done_when": t["done_when"],
                    **({"done_when_by": "him"} if t["done_when"] else {}),
                    **({"done_when_shared": t["shared"]} if t.get("shared") else {}),
                }
            )
            items.append(item)
            new.append(item["id"])
        status.work_save(sd, items)
    return new


def task_lead_lines(cfg: dict, now: float) -> list[str]:
    """One line per task with no worker yet: first in the prompt and stop hooks, with the one NEXT."""
    out = []
    for i in task_lead_items(cfg):
        dw = (i.get("done_when") and i["done_when"] + (f" (his one done-when covers all {i['done_when_shared']} tasks of that prompt)" if i.get("done_when_shared") else "")) or f'NOT SET (it closes only on his word, or set one: hubkit work criterion {i["id"]} --done-when "..")'
        out.append(
            f"hubkit TASK {i['id']} ({NOT_AN_ORDER}): he gave this task: \"{i['title'][:120]}\" (first {int((now - float(i['added'])) / 60)} min ago), "
            f"no worker yet. It is priority: first in every list, never queued behind other work, open until he marks it finished or its done-when is proven. "
            f"Done when: {dw}. NEXT: brief a free owner naming {i['id']} now (the hook records the worker), or hubkit work assign {i['id']} --owner <who>"
        )
    return out


# ---- his words on tasks: hold, resume, finished ----

HOLD = re.compile(
    r"(?i)^\W*(?:please\s+)?(?:stop|halt|pause|hold)(?:\s+(?:it|this|all|everything|now|here|work|the\s+tasks?|tasks?|all\s+(?:the\s+)?tasks?"
    r"|everything\s+now|all\s+work))?\W*$"
)
HOLD_ONE = re.compile(r"(?i)^\W*(?:please\s+)?(?:stop|halt|pause|hold)\s+(?:the\s+|task\s+|work\s+on\s+)?(?P<ref>(?:W\d+|[^.!?\n]{3,80}?))\W*$")
RESUME = re.compile(r"(?i)^\W*(?:please\s+)?(?:resume|unpause|continue\s+(?:the\s+)?tasks?)(?:\s+(?:all|the|tasks?|it|work|everything|W\d+))*\W*$")
_FIN = r"(?:finish(?:ed)?|done|complete[d]?|closed)"
FINISH = [
    re.compile(rf"(?i)\b(?:mark|set|call|consider|count)\s+(?P<ref>[^.!?\n]{{2,100}}?)\s+(?:as\s+)?{_FIN}\b"),
    re.compile(rf"(?i)(?P<ref>\bW\d+\b|[^.!?\n,;:]{{3,80}}?)\s+(?:is|are|was)\s+(?:now\s+|all\s+)?{_FIN}\b"),
    re.compile(r"(?i)\b(?:finish(?:ed)?|close|closing)\s+(?P<ref>W\d+)\b"),
    re.compile(rf"(?i)^\W*(?P<ref>W\d+)\s+{_FIN}\b"),
]
_STOPWORDS = set("the and for are was this that task tasks item thing one all now our your his her its with from".split())


def _sig(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2 and w not in _STOPWORDS}


def _match_task(open_: list[dict], ref: str, explicit: bool) -> list[dict]:
    """The open tasks a reference names: an id, or most of the significant words of its title (one word is enough only for "mark ... finished")."""
    ids = {x.lower() for x in re.findall(r"(?i)\bW\d+\b", ref)}
    if ids:
        return [t for t in open_ if t["id"].lower() in ids]
    r = _sig(ref)
    out = []
    for t in open_:
        sh = len(r & _sig(t["title"]))
        if r and ((sh >= 2 and sh / len(r) >= 0.6) or (explicit and len(r) == 1 and sh == 1)):
            out.append(t)
    return out


def _set_held(cfg: dict, ids: list[str], held: float | None, quote: str) -> None:
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        items = status.work_load(sd)
        for i in items:
            if i["id"] in ids:
                if held is None:
                    i.pop("held", None)
                else:
                    i["held"], i["held_by"] = held, redact_obj({"q": quote})["q"]
        status.work_save(sd, items)


def task_words(cfg: dict, text: str, now: float) -> list[str]:
    """What his typed words do to open tasks: "stop"/"halt"/"pause the tasks" holds them, "resume" releases them, and
    "mark X finished" / "X is done" closes the one task X names. A question or an ambiguous name closes nothing."""
    quote = " ".join(text.split())[:160]
    body = _mask(text).strip()  # quoted text ("mark W1 finished") is a mention, not his word
    if HOLD.match(body):
        ids = [i["id"] for i in open_tasks(cfg)]
        if not ids:
            return []
        _set_held(cfg, ids, now, quote)
        return [f"hubkit: his bare word {quote!r} holds ALL {len(ids)} open task(s): {', '.join(ids)}. They are not chased and do not gate the stop until he says resume. To hold one only, he names it (\"stop W5\"). NEXT: tell their workers to stop (SendMessage, never TaskStop)"]
    if (one := HOLD_ONE.match(body)) and not RESUME.match(body):
        hits = _match_task(open_tasks(cfg), one.group("ref"), explicit=True)
        if len(hits) == 1:
            _set_held(cfg, [hits[0]["id"]], now, quote)
            return [f"hubkit: his word {quote!r} holds {hits[0]['id']} ({hits[0]['title'][:60]}) only; the other tasks stay open. NEXT: tell its workers to stop (SendMessage, never TaskStop)"]
        if len(hits) > 1:
            return [f"hubkit: his word {quote!r} fits {len(hits)} open tasks ({', '.join(t['id'] for t in hits)}), so none was held. NEXT: he names the id"]
    if RESUME.match(body):
        held = [i for i in status.work_load(store.state(cfg)) if i.get("task") and i.get("held")]
        named = {x.lower() for x in re.findall(r"(?i)\bW\d+\b", body)}
        ids = [i["id"] for i in held if not named or i["id"].lower() in named]
        if not ids:
            return []
        _set_held(cfg, ids, None, quote)
        return [f"hubkit: his word {quote!r} resumes {', '.join(ids)}. They are open again: chase, check and gate as before"]
    first = (_words(body) or [""])[0]
    if body.endswith("?") or first in _QUESTION_START:
        return []
    out: list[str] = []
    for rx in FINISH:
        for fm in rx.finditer(body):
            if re.search(r"(?i)\b(?:not|never|isn't|aren't|wasn't)\b", body[max(0, fm.start() - 12) : fm.end()]):
                continue
            hits = _match_task(open_tasks(cfg), fm.group("ref"), explicit=rx is FINISH[0])
            if len(hits) == 1:
                t = hits[0]
                try:
                    close(cfg, t["id"], f"his word: {quote}")
                except KeyError:
                    continue
                who = ", ".join(w["name"] for w in t.get("workers", [])) or "no worker"
                out.append(f"hubkit: his word closed {t['id']} ({t['title'][:60]}) as finished and archived it. NEXT: tell {who} it is finished (SendMessage, never TaskStop)")
            elif len(hits) > 1:
                out.append(f"hubkit: his word {quote!r} fits {len(hits)} open tasks ({', '.join(t['id'] for t in hits)}), so none was closed. NEXT: ask him which, or he names the id")
    return out


# ---- workers, criterion, blocker, interval ----


def _task(items: list[dict], wid: str) -> dict:
    hit = next((i for i in items if i["id"] == wid), None)
    if hit is None or not hit.get("task"):
        raise KeyError(f"no live task {wid}")
    return hit


def set_criterion(cfg: dict, wid: str, text: str) -> str:
    """Set what "finished" means for a task, in words the hub can prove with evidence."""
    if not text.strip():
        raise ValueError("a criterion needs --done-when: the observable thing that shows the task is finished")
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        items = status.work_load(sd)
        t = _task(items, wid)
        t["done_when"], t["done_when_by"] = redact_obj({"c": text.strip()})["c"][:300], "hub"
        status.work_save(sd, items)
    return f"{wid} done-when set; close it with: hubkit work done {wid} --evidence <url|file:path>"


def assign(cfg: dict, wid: str, owner: str, via: str = "hub", routed_id: str = "", eta: float | None = None, now: float | None = None) -> str:
    """Record that an agent or peer holds a task (the first action of a task). Assigning the same holder again is a nudge, not a duplicate."""
    if not owner.strip():
        raise ValueError("assign needs --owner: the agent or peer that holds the task")
    now = now or time.time()
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        items = status.work_load(sd)
        t = _task(items, wid)
        w = next((x for x in t.setdefault("workers", []) if _nn(x["name"]) == _nn(owner)), None)
        if w is None:
            w = {"name": owner.strip(), "t": now, "via": via}
            t["workers"].append(w)
        else:
            w["last_nudge"] = now
        if routed_id:
            w["routed"] = routed_id
        if eta:
            w["eta_min"] = float(eta)
        if not t.get("first_action"):
            t["first_action"], t["first_action_note"] = now, f"{owner.strip()} holds it"
        status.work_save(sd, items)
    return f"{wid} held by {', '.join(x['name'] for x in t['workers'])}"


def _brief_owner(tool: str, ti: dict, resp: object) -> str:
    if tool == "SendMessage":
        return str(ti.get("to") or ti.get("recipient") or "")
    found = re.search(r"agentId: (\w+)", resp) if isinstance(resp, str) else None
    ident = resp.get("agentId") if isinstance(resp, dict) else (found.group(1) if found else None)
    return str(ident or "agent:" + str(ti.get("description", "")))


def _pr_numbers(s: str) -> set[str]:
    return set(re.findall(r"(?<![\w/])#(\d{2,6})\b", s)) | set(re.findall(r"/pull/(\d+)", s))


def names_task(t: dict, brief: str) -> bool:
    """A brief names a task when it holds its id, one of its PR numbers, or clearly its title (80% of its words, 3 or more)."""
    if re.search(rf"(?i)(?<![\w-]){re.escape(t['id'])}\b", brief):
        return True
    if _pr_numbers(t["title"]) & _pr_numbers(brief):
        return True
    tw = _sig(t["title"])
    return len(tw) >= 3 and len(tw & _sig(brief)) / len(tw) >= 0.8


def note_workers(cfg: dict, tool: str, ti: dict, resp: object, item: dict | None, now: float | None = None) -> list[str]:
    """A SendMessage or Agent brief that names an open task makes its recipient a worker of that task. Returns what was recorded."""
    open_ = open_tasks(cfg)
    if not open_:
        return []
    owner = str(item["owner"]) if item else _brief_owner(tool, ti, resp)
    brief = "\n".join(str(ti.get(k) or "") for k in ("description", "summary", "message", "prompt"))
    if not owner.strip() or owner.lower() == "inbox":
        return []
    out = []
    for t in open_:
        if names_task(t, brief):
            est = item.get("estimate_min") if item and not item.get("estimate_defaulted") else None
            out.append(assign(cfg, t["id"], owner, "hook", str(item["id"]) if item else "", est, now))
    return out


def task_block(cfg: dict, wid: str, on: str, fix: str, holder: str) -> str:
    """A named blocker on a task: what it waits on, the fix running, who runs it. All three, or it is not a blocker (empty --on clears)."""
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        items = status.work_load(sd)
        t = _task(items, wid)
        if not on.strip():
            t.pop("blocker", None)
            msg = f"{wid} no longer blocked"
        elif not (fix.strip() and holder.strip()):
            raise ValueError("a task blocker needs --on (what it waits on), --fix (the action running now) and --holder (who runs it)")
        else:
            t["blocker"] = redact_obj({"on": on.strip(), "fix": fix.strip(), "holder": holder.strip(), "t": time.time()})
            msg = f"{wid} blocked on {on.strip()}; fix {fix.strip()!r} held by {holder.strip()}"
        status.work_save(sd, items)
    return msg


def set_every(cfg: dict, wid: str, minutes: int) -> str:
    """Check this task every `minutes` (0 returns it to the task interval)."""
    if minutes < 0:
        raise ValueError("--minutes must be 1 or more (0 clears)")
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        items = status.work_load(sd)
        t = _task(items, wid)
        if minutes:
            t["every_min"] = minutes
        else:
            t.pop("every_min", None)
        status.work_save(sd, items)
    return f"{wid} checked every {minutes} min" if minutes else f"{wid} back on the task interval"


# ---- finishing: his word, or evidence against a criterion ----

PR_URL = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/pull/\d+/?$")


def done_proof(cfg: dict, item: dict, evidence: str, by_word: str) -> str:
    """What closes a task, or ValueError naming the two ways. His typed word closes it (the hook passes it); the hub closes it
    only with evidence of its done-when criterion: a file that exists and postdates the task, or a merged PR (checked on GitHub)."""
    wid = item["id"]
    if by_word:
        return f"his word: {by_word}"
    crit = str(item.get("done_when") or "")
    if not crit:
        raise ValueError(
            f"{wid} is a task he gave and it has no done-when criterion, so it closes only two ways: (1) his typed word naming it "
            f"(\"mark {wid} finished\"), or (2) a criterion first: hubkit work criterion {wid} --done-when \"<text>\", then hubkit work done {wid} --evidence <url|file:path>"
        )
    ev = evidence.strip()
    if not ev:
        raise ValueError(f"{wid} is a task with the criterion {crit!r}: close it with --evidence <merged PR URL or file:<path>> that shows it is met")
    if ev.startswith("file:"):
        p = Path(ev[5:]).expanduser()
        if not p.is_file():
            raise ValueError(f"evidence file {p} does not exist")
        if os.path.getmtime(p) <= float(item["added"]):
            raise ValueError(f"evidence file {p} is older than the task ({wid} was given later), so it proves nothing about it")
        return f"evidence {ev}"
    if PR_URL.match(ev):
        from . import routed

        info = routed.pr_info(cfg, ev.rstrip("/"), float(routed.opts(cfg)["gh_timeout_s"]))
        if info is None:
            raise ValueError(f"GitHub could not be read for {ev}: evidence that cannot be checked is not evidence")
        if info["state"] != "MERGED":
            raise ValueError(f"{ev} is {info['state']}, not MERGED: a PR is evidence of a finished task only once it merged")
        return f"evidence {ev} (merged)"
    raise ValueError(
        "evidence is a merged GitHub PR URL or file:<path>. Any other page cannot be checked from here: save the proof "
        "(page text, log, command output) to a file made after the task was given and pass --evidence file:<path>"
    )


# ---- tasks: workers, ETA, cover, due ----


def worker_state(cfg: dict, name: str) -> dict:
    """Whether a worker is working: a live session that is not stuck on a prompt, or a background agent whose output moved
    within `watcher.stale_agent_min`."""
    ss = session_state(cfg, name)
    if ss["live"]:
        return {"live": not ss.get("waiting"), "waiting": bool(ss.get("waiting")), "text": ss["text"]}
    path = slow.holder_transcript(cfg, name, None)
    if path:
        try:
            age = (time.time() - os.path.getmtime(path)) / 60
        except OSError:
            return {"live": False, "waiting": False, "text": ss["text"]}
        if age <= float(cfg["watcher"]["stale_agent_min"]):
            return {"live": True, "waiting": False, "text": f"background agent, output {int(age)}m ago"}
        return {"live": False, "waiting": False, "text": f"background agent, silent {int(age)}m"}
    return {"live": False, "waiting": False, "text": ss["text"]}


def eta_left(cfg: dict, task: dict, w: dict, now: float) -> float:
    """Awake minutes left in a worker's ETA (negative = past): the task's own estimate (from when it was given) or the worker's
    (from when it took it), the later one; with neither, `task_eta_default_min` from when it took it."""
    spans = []
    if task.get("estimate_min") is not None:
        spans.append((float(task["added"]), float(task["estimate_min"])))
    if w.get("eta_min"):
        spans.append((float(w["t"]), float(w["eta_min"])))
    if not spans:
        spans.append((float(w["t"]), float(cfg["tasks"]["eta_default_min"])))
    return max(m - _mins(t0, now) for t0, m in spans)


def task_state(cfg: dict, task: dict, now: float) -> tuple[str, str, str]:
    """(state, why, session text) of a task: within ETA when some worker is live and inside its ETA."""
    ws = task.get("workers", [])
    if not ws:
        return NOWORKER, "no agent or peer holds it", "none"
    seen = []
    for w in ws:
        st = worker_state(cfg, w["name"])
        left = eta_left(cfg, task, w, now)
        seen.append((w, st, left))
        if st["live"] and left > 0:
            return WITHIN, f"{w['name']} is working; its ETA has {int(left)}m left", st["text"]
    w, st, left = seen[-1]
    if any(x[1]["waiting"] for x in seen):
        return WAITING, f"{w['name']} waits on a prompt", st["text"]
    if not st["live"]:
        return IDLE, f"{w['name']} is not live ({st['text']})", st["text"]
    return OVERDUE, f"{w['name']} is {int(-left)}m past its ETA", st["text"]


def _asked_about(cfg: dict, task: dict) -> bool:
    """An `ask` work item names the task: it waits on him."""
    pat = re.compile(rf"(?i)(?<![\w-]){re.escape(task['id'])}\b")
    return any(i.get("kind") == "ask" and pat.search(f"{i.get('title', '')} {i.get('note', '')}") for i in status.work_load(store.state(cfg)))


def task_cover(cfg: dict, task: dict, now: float) -> str | None:
    """None when the stop may pass this task (a live worker inside its ETA, or it waits on him in an ask item, or on a
    blocker with a fix and a holder); else the line saying why not, with the one NEXT."""
    state, why, _ = task_state(cfg, task, now)
    b = task.get("blocker") or {}
    if state == WITHIN or _asked_about(cfg, task) or all(b.get(k) for k in ("on", "fix", "holder")):
        return None
    return f"{task['id']} ({task['title'][:50]}): {why}. NEXT: {task_nudge(task, state)}"


def task_nudge(task: dict, state: str) -> str:
    """The fast-track action for a task: never wait, never queue; one missed nudge means re-brief another owner."""
    tid = task["id"]
    if state == NOWORKER:
        return f"brief a free owner naming {tid} now (the hook records the worker), or hubkit work assign {tid} --owner <who>; never behind other work"
    if state == WAITING:
        return f"answer its prompt in that session at once, or re-brief {tid} to a free owner"
    if state == WITHIN:
        return ""
    if int(task.get("nudges", 0)) >= 1:
        return f"re-brief {tid} to another free owner now (the nudge went unanswered), then hubkit work assign {tid} --owner <who>, or name a blocker: hubkit work block {tid} --on .. --fix .. --holder .."
    crit = task.get("done_when") or "none set"
    return f"SendMessage the worker: one-line state and a new ETA for {tid} (done-when: {crit}), then hubkit work estimate {tid} --minutes N --why \"..\""


def task_rows(cfg: dict, now: float) -> list[dict]:
    """One row per open task, before every other row."""
    out = []
    for t in open_tasks(cfg):
        state, why, sess = task_state(cfg, t, now)
        out.append(
            {
                "id": t["id"],
                "who": ", ".join(w["name"] for w in t.get("workers", [])) or "NO WORKER",
                "item": t["title"][:50],
                "state": state,
                "why": why,
                "session": sess,
                "socket": None,
                "task": True,
                "done_when": t.get("done_when") or "NOT SET",
                "task_item": t,
            }
        )
    return out


def task_due(cfg: dict, task: dict, now: float) -> bool:
    """A task is due a check when its interval of awake minutes passed since the last check (or since it was given)."""
    return _mins(float(task.get("checked") or task["added"]), now) >= interval_of(cfg, task)


def task_due_lines(cfg: dict, now: float) -> list[str]:
    """One line for the prompt hook: the tasks whose check is due, with the one NEXT."""
    due = [r for r in task_rows(cfg, now) if task_due(cfg, r["task_item"], now)]
    if not due:
        return []
    names = ", ".join(f"{r['id']} ({r['state']})" for r in due[:8])
    return [
        f"hubkit TASK CHECK DUE: {len(due)} task(s) past their check interval: {names}. NEXT: hubkit tasks (tasks first), SendMessage each worker that is "
        "not within its ETA for a one-line state and an ETA (re-brief at once after one missed nudge; hubkit never sends), then: hubkit tasks done"
    ]


# ---- the check interval ----


def _override(cfg: dict) -> dict:
    try:
        d = json.loads((store.state(cfg, create=False) / OVERRIDE_FILE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except OSError, ValueError:
        return {}


def task_interval_min(cfg: dict) -> float:
    """Minutes between checks of a task: `hubkit tasks interval` (state), else `[tasks] interval_min` (15)."""
    return float(_override(cfg).get("interval_min") or cfg["tasks"]["interval_min"])


def set_interval(cfg: dict, minutes: int) -> str:
    """Set the check interval of tasks for this state dir; the config sets the default."""
    if not 1 <= minutes <= 24 * 60:
        raise ValueError("the interval is 1 to 1440 minutes")
    store.write_atomic(store.state(cfg) / OVERRIDE_FILE, json.dumps({**_override(cfg), "interval_min": minutes}))
    return f"task check interval is {minutes} min"


def interval_of(cfg: dict, task: dict) -> float:
    """Minutes between checks of one task: its own `work every`, else the task interval."""
    return float(task.get("every_min") or task_interval_min(cfg))


def session_state(cfg: dict, owner: str) -> dict:
    """Whether the owner's live session file says it is working or stuck on a prompt."""
    sess = slow.holder_session(cfg, owner)
    if not sess:
        return {"live": False, "waiting": False, "text": "background agent (no session file)" if slow.AGENT_ID.match(owner) else "no live session"}
    waiting = bool(sess.get("waitingFor")) or str(sess.get("status")) in ("waiting", "blocked")
    return {"live": True, "waiting": waiting, "text": f"live, {sess.get('status', '?')}"}


def close(cfg: dict, wid: str, proof: str) -> str:
    """Archive a finished task with what finished it (the user's word, or the evidence)."""
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        items = status.work_load(sd)
        hit = next((i for i in items if i["id"] == wid), None)
        if hit is None:
            raise KeyError(f"no live item {wid}")
        now = time.time()
        day = store.day_of(cfg, now)
        store.append_row(sd / "archive" / f"archive_{day}.jsonl", {"type": "work", **hit, "outcome": "done", "done_proof": proof, "ts": now})
        status.work_save(sd, [i for i in items if i["id"] != wid])
    return f"{wid} archived to archive_{day} ({proof})"


def finish(cfg: dict, wid: str, evidence: str, note: str = "") -> str | None:
    """`work done` on a task: closes it when the proof holds (ValueError names why not); None when `wid` is no task."""
    item = next((i for i in status.work_load(store.state(cfg)) if i["id"] == wid), None)
    if item is None or not item.get("task"):
        return None
    return close(cfg, wid, done_proof(cfg, item, evidence, ""))


# ---- the stop gate, the watchdog, the status block ----


def gate_reasons(cfg: dict, now: float) -> list[str]:
    """Reasons a task holds the turn end (those that already blocked twice this turn go to the let-through note instead)."""
    open_ = open_tasks(cfg)
    if not open_:
        return []
    reasons = [f"hubkit TASK OPEN: {line}" for t in open_ if (line := task_cover(cfg, t, now))]
    block, let = _limit_repeats(cfg, reasons)
    store.write_atomic(store.state(cfg) / "task-letthrough.json", json.dumps(let))
    return block


def take_let_through(cfg: dict) -> list[str]:
    """The task reasons the last gate run let through (read once by the Stop hook)."""
    p = store.state(cfg) / "task-letthrough.json"
    try:
        out = json.loads(p.read_text(encoding="utf-8"))
        p.unlink()
    except OSError, ValueError:
        return []
    return [str(x) for x in out] if isinstance(out, list) else []


def _limit_repeats(cfg: dict, reasons: list[str]) -> tuple[list[str], list[str]]:
    import hashlib

    sd = store.state(cfg)
    seen = sd / "seen" / "prompt"
    turn = seen.stat().st_mtime if seen.exists() else 0.0
    path = sd / "task-gate.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        data = {}
    counts: dict[str, int] = data.get("counts", {}) if data.get("turn") == turn else {}
    block, let = [], []
    for r in reasons:
        key = hashlib.sha1(re.sub(r"\d+", "#", r).encode()).hexdigest()[:12]  # minute counters do not make a new reason
        if counts.get(key, 0) >= TASK_BLOCKS:
            let.append(r)
        else:
            counts[key] = counts.get(key, 0) + 1
            block.append(r)
    store.write_atomic(path, json.dumps({"turn": turn, "counts": counts}))
    return block, let


def alerts(cfg: dict, now: float) -> list[dict]:
    """Watchdog: one alert per task whose check is due; the key moves with each check, so each interval alerts again."""
    return [
        {
            "key": f"{r['id']}:task_check:{int(float(r['task_item'].get('checked') or r['task_item']['added']))}",
            "msg": (
                f"hubkit TASK CHECK DUE {r['id']} ({r['item']}): {int(interval_of(cfg, r['task_item']))} min since the last check; {r['state']}: {r['why']}. "
                f"NEXT: hubkit tasks check, nudge or re-brief its worker, then: hubkit tasks done"
            ),
        }
        for r in task_rows(cfg, now)
        if task_due(cfg, r["task_item"], now)
    ]


def check_done(cfg: dict, now: float | None = None) -> str:
    """Record that the hub checked every task now: restarts the clocks and counts a missed nudge for a task that is not covered."""
    now = now or time.time()
    late = {r["id"]: r["state"] != WITHIN and task_cover(cfg, r["task_item"], now) is not None for r in task_rows(cfg, now)}
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        items = status.work_load(sd)
        for i in items:
            if i.get("task") and i["id"] in late:
                i["checked"] = now
                i["nudges"] = int(i.get("nudges", 0)) + 1 if late[i["id"]] else 0
        status.work_save(sd, items)
    return f"task check recorded for {len(late)} task(s); next due in {int(task_interval_min(cfg))} min"


def render(cfg: dict, now: float | None = None) -> list[str]:
    """Every open task, first the ones with no worker: state, who holds it, the done-when and the nudge."""
    now = now or time.time()
    rows = task_rows(cfg, now)
    out = [f"tasks: {len(rows)} open; checked every {int(task_interval_min(cfg))} min; hubkit sends nothing, you do"]
    for r in sorted(rows, key=lambda r: r["state"] != NOWORKER):
        out.append(f"  {r['id']:<6} {r['who'][:22]:<22} {r['state']:<28} {r['item']} | {r['why']} | done-when: {r['done_when']}")
        nxt = task_nudge(r["task_item"], r["state"])
        if nxt:
            out.append(f"         NEXT: {nxt}")
    return out


def block_lines(cfg: dict, tasks: list[dict], now: float) -> list[str]:
    """Status block: tasks first, with the criterion, who holds each (or that nobody does) and its check clock."""
    out = []
    for i in tasks:
        who = ", ".join(w["name"] for w in i.get("workers", [])) or "NO WORKER"
        crit = i.get("done_when") or "NOT SET (closes only on his word)"
        if i.get("done_when") and i.get("done_when_by") == "hub":
            crit += " [written by the hub, he has not seen it]"
        tail = " | HELD by his word" if i.get("held") else f" | check every {int(interval_of(cfg, i))} min"
        out.append(f"task: {i['id']} {i['title'][:80]} | done-when: {crit} | workers: {who}{tail}" + (" | BLOCKED: " + i["blocker"]["on"][:40] if i.get("blocker") else ""))
    return out
