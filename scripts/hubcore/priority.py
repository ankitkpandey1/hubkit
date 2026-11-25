"""What he says is urgent leads. Learning class `priority-not-applied`: a note did not hold, so this is code.

A typed prompt with an urgent word ("urgent", "code red", "top priority", "asap", and their typos) opens or
updates ONE priority work item that quotes him. A "check msgs" style ask is an ask whose first action is an
inbox read. While such an item has no first action, the prompt and stop hooks lead with it and old
STALLED/OVERDUE lines are folded under it as a count.

This is NOT an order. It authorises no merge, release, deploy or message (R-001); orders stay a hand act
(intents.toml `auto_open = false`).

Tasks the user gives are in tasks.py; this module only leads with them (lead_lines) and raises their no-worker alert.
"""

from __future__ import annotations

import difflib
import json
import re
import time
from pathlib import Path

from . import status, store
from . import tasks as tasks_mod
from .redact import redact_obj

MERGE_MIN = 10
NOT_AN_ORDER = "NOT an order: it authorises no merge, release, deploy or message"
FOLDED = re.compile(r"^hubkit (?:STALLED|OVERDUE): ")

_URGENT_WORDS = ("urgent", "urgently")
_PRIORITY_WORD = "priority"
_RANK = {"top", "high", "highest", "first", "main", "number", "p0", "p1"}
_FIXED = re.compile(
    r"(?i)\basap\b|\ba\.s\.a\.p\b|\bas soon as possible\b|\bcode[\s_-]*red\b"
)
INBOX_ASK = re.compile(
    r"(?i)\b(?:check|read|see|open|review|look\s+(?:at|through)|go\s+through|catch\s+up\s+on)\b[^.?!\n]{0,40}\b(?:msgs?|messages?|inbox|dms?)\b"
    r"|\b(?:is|did|has|have|was)\b[^.?!\n]{0,40}\b(?:msgs?|messages?)\b[^.?!\n]{0,20}\b(?:done|read|seen|replied|answered|handled)\b"
)
INBOX_CALL = re.compile(
    r"(?i)\bmep\b|\bmiseenplace\b|\binbox\b|\bzulip\b|\bmsgs?\b|\bmessages\b"
)
INBOX_BASH = re.compile(
    r"(?i)\b(?:mep|miseenplace)\s+(?:watch|inbox|ls|list|read|msgs?|messages?|unread|threads?|show|get)\b|\bhubkit(?:\s+\S+)?\s+inbound\b|\binbound\s+ingest\b"
)


def _near(word: str, target: str) -> bool:
    return word == target or difflib.SequenceMatcher(None, word, target).ratio() >= 0.8


def is_urgent(text: str) -> bool:
    """An urgent word, a typo of one, or a rank word ("top", "high") before a typo of "priority"."""
    if _FIXED.search(text):
        return True
    words = re.findall(r"[a-z0-9.']+", text.lower())
    for n, w in enumerate(words):
        if len(w) >= 5 and any(_near(w, u) for u in _URGENT_WORDS):
            return True
        if len(w) >= 6 and _near(w, _PRIORITY_WORD) and n and words[n - 1] in _RANK:
            return True
    return False


def is_inbox_ask(text: str) -> bool:
    return bool(INBOX_ASK.search(text))


# ---- the work item ----


def lead_items(cfg: dict) -> list[dict]:
    """Live priority-led work items (opened here) with no first action yet."""
    return [
        i
        for i in status.work_load(store.state(cfg))
        if i.get("kind") != "request" and i.get("lead") and not i.get("first_action") and not i.get("task")  # a request has a "lead" word too: not this
    ]


def _title(quotes: list[str], inbox: bool) -> str:
    q = " | ".join(f'"{x}"' for x in quotes)[:220]
    return f"HIS WORDS {q}" + (" -> read his messages first" if inbox else "")


def intake(cfg: dict, text: str, now: float) -> str | None:
    """Open or update the one priority item for a human prompt. Returns its id, or None when the prompt is neither urgent nor an inbox ask."""
    urgent, inbox = is_urgent(text), is_inbox_ask(text)
    if not (urgent or inbox):
        return None
    quote = " ".join(text.split())[:160]
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        items = status.work_load(sd)
        hit = next(
            (
                i
                for i in items
                if i.get("kind") != "request"
                and i.get("lead")
                and not i.get("task")
                and (
                    not i.get("first_action")
                    or now - float(i.get("last_prompt", 0)) <= MERGE_MIN * 60
                )
            ),
            None,
        )
        if hit is None:
            hit = redact_obj(
                {
                    "id": status.next_work_id(sd, items),
                    "kind": "priority" if urgent else "active",
                    "title": "",
                    "owner": cfg["user"]["name"],
                    "note": NOT_AN_ORDER + ". First action due at once.",
                    "added": now,
                    "lead": True,
                    "quotes": [],
                    "prompts": 0,
                    "estimate_min": 15,
                    "estimates": [
                        {
                            "t": now,
                            "min": 15,
                            "source": "hub",
                            "why": "priority intake default",
                        }
                    ],
                }
            )
            items.append(hit)
        if urgent and hit["kind"] != "priority":
            hit["kind"] = "priority"
        hit["quotes"] = [*hit.get("quotes", []), redact_obj({"q": quote})["q"]][-6:]
        hit["prompts"] = int(hit.get("prompts", 0)) + 1
        hit["last_prompt"] = now
        hit["needs_inbox"] = bool(hit.get("needs_inbox")) or inbox
        hit["title"] = _title(hit["quotes"], hit["needs_inbox"])
        status.work_save(sd, items)
    return str(hit["id"])


def mark_acted(cfg: dict, wid: str, note: str, inbox_read: bool = False) -> str:
    """Record the first action. An item that needs an inbox read takes only an inbox read (--inbox-read, with what it found)."""
    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        items = status.work_load(sd)
        hit = next((i for i in items if i["id"] == wid), None)
        if hit is None:
            raise KeyError(f"no live item {wid}")
        if not note.strip():
            raise ValueError("a first action needs --note: what was done")
        if hit.get("needs_inbox") and not hit.get("inbox_read") and not inbox_read:
            raise ValueError(
                f'{wid} asks for an inbox read first: read his messages (an agent runs the inbox watcher), then --inbox-read --note "<what the read found>"'
            )
        hit["first_action"] = time.time()
        hit["first_action_note"] = redact_obj({"n": note})["n"][:200]
        if inbox_read:
            hit["inbox_read"] = hit["first_action"]
        status.work_save(sd, items)
    return f"{wid} first action recorded"


# ---- evidence of an inbox read, from tool calls ----


def inbox_tool_words(cfg: dict) -> list[str]:
    """Name parts that make an mcp tool a chat or mail tool (`[priority] inbox_tools`; add your own chat product)."""
    return [w for w in cfg.get("priority", {}).get("inbox_tools", ["gmail", "zulip", "slack", "chat"]) if w]


def is_inbox_call(name: str, inp: dict, tool_words: list[str]) -> bool:
    """A tool call that reads his messages: the inbox CLI, an agent briefed on the inbox, or a chat/mail read tool."""
    if name == "Bash":
        return bool(INBOX_BASH.search(str(inp.get("command") or "")))
    if name in ("Agent", "Task"):
        return bool(
            INBOX_CALL.search(
                f"{inp.get('description') or ''} {inp.get('prompt') or ''}"
            )
        )
    return (
        name.startswith("mcp__")
        and bool(re.search("(?i)" + "|".join(map(re.escape, tool_words)), name))
        and bool(re.search(r"(?i)search|list|read|get|thread|message", name))
    )


def _stamp(entry: dict) -> float | None:
    from datetime import datetime

    try:
        return datetime.fromisoformat(
            str(entry["timestamp"]).replace("Z", "+00:00")
        ).timestamp()
    except KeyError, ValueError:
        return None


def inbox_read_since(transcript: str | Path | None, since: float, tool_words: list[str]) -> bool:
    """True when the transcript holds an inbox-read tool call made after `since`."""
    if not transcript:
        return False
    try:
        lines = (
            Path(transcript).read_text(encoding="utf-8", errors="replace").splitlines()
        )
    except OSError:
        return False
    for line in reversed(lines):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        t = _stamp(o)
        if t is not None and t < since:
            break
        content = (o.get("message") or {}).get("content")
        for b in (
            content
            if o.get("type") == "assistant" and isinstance(content, list)
            else []
        ):
            if (
                isinstance(b, dict)
                and b.get("type") == "tool_use"
                and is_inbox_call(str(b.get("name")), b.get("input") or {}, tool_words)
            ):
                return True
    return False


def note_tool(cfg: dict, tool: str, tool_input: dict) -> None:
    """A routed SendMessage/Agent is a first action for a plain item; an inbox-reading call is one for any item."""
    inbox = is_inbox_call(tool, tool_input, inbox_tool_words(cfg))
    for i in lead_items(cfg):
        if inbox:
            mark_acted(cfg, i["id"], f"{tool} read his messages", inbox_read=True)
        elif not i.get("needs_inbox"):
            mark_acted(cfg, i["id"], f"{tool} routed work")


# ---- lines for the prompt and stop hooks ----


def lead_lines(
    cfg: dict, now: float, transcript: str | Path | None = None, tasks: bool = True
) -> list[str]:
    """One line per priority item with no first action: his words, the age, the one NEXT. Tasks first (the stop gate passes
    tasks=False: it judges tasks itself, with a repeat limit)."""
    out = tasks_mod.task_lead_lines(cfg, now) if tasks else []
    for i in lead_items(cfg):
        if i.get("needs_inbox") and inbox_read_since(transcript, float(i["added"]) - 5, inbox_tool_words(cfg)):
            mark_acted(
                cfg, i["id"], "inbox read seen in the transcript", inbox_read=True
            )
            continue
        quote = "; ".join(f'"{q[:90]}"' for q in i.get("quotes", [])[-3:])
        nxt = (
            (
                "read his messages now: an agent runs the inbox watcher (`mep watch --cursor hub`), then hubkit work act "
                f"{i['id']} --inbox-read --note \"<what the read found>\""
            )
            if i.get("needs_inbox")
            else (
                f"take or route the first action now, then hubkit work act {i['id']} --note \"<what you did>\""
            )
        )
        out.append(
            f"hubkit PRIORITY {i['id']} ({NOT_AN_ORDER}): he said {quote} ({i.get("prompts", 1)} prompt(s), first {int((now - float(i["added"])) / 60)} min ago), "
            f"no first action yet. Everything below waits for it. NEXT: {nxt}"
        )
    return out


def _label(line: str) -> str:
    return re.split(r"[:\s]", FOLDED.sub("", line), maxsplit=1)[0]


def fold(cfg: dict, lines: list[str], lead: list[str]) -> list[str]:
    """With a priority line leading, old STALLED/OVERDUE lines become one count under it. Without one, lines pass unchanged."""
    if not lead:
        return lines
    old = [x for x in lines if FOLDED.match(x)]
    rest = [x for x in lines if not FOLDED.match(x)]
    if not old:
        return rest
    names = ", ".join(dict.fromkeys(_label(x) for x in old))[:120]
    return [
        *rest,
        f"hubkit: {len(old)} older STALLED/OVERDUE line(s) held under the priority item ({names}); hubkit status lists them. They come back at its first action.",
    ]


def alerts(cfg: dict, now: float) -> list[dict]:
    """Watchdog: a priority item with no first action after 5 minutes."""
    return [
        {
            "key": f"{i['id']}:lead_no_first_action",
            "msg": (
                f"{i['id']} his urgent words have no first action after {int((now - float(i['added'])) / 60)} min. NEXT: hubkit work act {i['id']} --note \"..\""
            ),
        }
        for i in lead_items(cfg)
        if now - float(i["added"]) > 300
    ] + [
        {
            "key": f"{i['id']}:lead_no_first_action",
            "msg": (
                f"{i['id']} task he gave has no worker after {int((now - float(i['added'])) / 60)} min. NEXT: brief a free owner naming {i['id']} now (the hook records it), "
                f"or hubkit work assign {i['id']} --owner <who>"
            ),
        }
        for i in tasks_mod.task_lead_items(cfg)
        if now - float(i["added"]) > 300
    ]
