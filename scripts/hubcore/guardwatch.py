"""Warn-only PreToolUse checks: work nobody asked for (an artefact, recurring work), the hub editing files itself, a post from the
wrong account, a yes that came before its draft, two agents on one host, and a review nobody asked for."""

from __future__ import annotations

import fnmatch
import os
import re
import tempfile
import time

from . import config, store
from .lessons import recent_prompts

WATCH_CMD = re.compile(r"\bhubkit\b[^\n;&|]*\bwatch\b")  # the hub's own watch is always allowed


def schedule_tools(cfg: dict) -> list[str]:
    """Tool names that schedule recurring work (config `guards.schedule_tools`)."""
    return [str(t) for t in (cfg.get("guards") or {}).get("schedule_tools") or []]


def _asked(cfg: dict, words: list[str], now: float) -> bool:
    window = float((cfg.get("guards") or {}).get("window_min", 60)) * 60
    pat = re.compile("|".join(re.escape(str(w)) for w in words), re.I) if words else None
    return bool(pat) and any(pat.search(r["text"]) for r in recent_prompts(cfg, window, now))  # type: ignore[union-attr]


def warning(cfg: dict, tool: str, tool_input: dict, now: float | None = None) -> str | None:
    """A warning when the tool call creates an artefact (`guards.needs_order`) or schedules recurring work and no recent
    typed prompt of the user asks for it. Never denies; `hubkit watch` is exempt."""
    now = now or time.time()
    g = cfg.get("guards") or {}
    command = str(tool_input.get("command") or "") if tool == "Bash" else ""
    if command and WATCH_CMD.search(command):
        return None
    scheduling = tool in schedule_tools(cfg) or (
        bool(command) and any(re.search(str(p), command) for p in g.get("schedule_regexes") or [])
    )
    if scheduling:
        if _asked(cfg, g.get("schedule_words") or [], now):
            return None
        what = tool if tool != "Bash" else f"'{command[:60]}'"
        return (f"hubkit WARNING UNORDERED-SCHEDULE: {what} sets up recurring work and none of the user's recent typed words asks for a schedule, "
                "loop or watch. Recurring checks cost the user attention and quota. NEXT: do it once now, or ask whether they want it repeated")
    if tool in EDIT_TOOLS:
        return inline_edit_warning(cfg, tool_input, now)
    if command and (w := account_warning(cfg, command)):
        return w
    if command:
        for pat in g.get("needs_order") or []:
            if m := re.search(str(pat), command):
                if _asked(cfg, g.get("needs_order_words") or [], now):
                    return None
                return (f"hubkit WARNING NEEDS-ORDER: '{m[0][:60]}' creates something the user did not ask for in their recent typed words. "
                        "NEXT: do not create it unasked; propose it in the report and wait for their word")
    return None


# ---- the hub doing work inline ----

EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")


def _inside(path: str, root: str) -> bool:
    return bool(root) and (path == root or path.startswith(root.rstrip("/") + "/"))


def inline_ok(cfg: dict, path: str) -> bool:
    """True when the hub may edit this path itself: its own state and config dirs, the temp dir, or a `guards.inline_ok_paths` glob."""
    full = os.path.normpath(os.path.expanduser(path))
    roots = [str(config.state_dir(cfg)), str(cfg.get("_config_dir") or ""), tempfile.gettempdir()]
    if any(_inside(full, os.path.normpath(r)) for r in roots if r):
        return True
    return any(fnmatch.fnmatch(full, os.path.expanduser(str(g))) for g in (cfg.get("guards") or {}).get("inline_ok_paths") or [])


def inline_edit_warning(cfg: dict, tool_input: dict, now: float) -> str | None:
    """A warning, once per turn, when the hub edits a file itself (Edit, Write, NotebookEdit) outside `guards.inline_ok_paths`.
    A turn starts at the user's last typed prompt. `guards.inline_edits = "off"` silences it. Never denies."""
    if str((cfg.get("guards") or {}).get("inline_edits", "warn")).lower() == "off":
        return None
    path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path") or "")
    if not path or inline_ok(cfg, path):
        return None
    mark = store.state(cfg) / "inline-edit-warned"
    try:
        warned = float(mark.read_text())
    except (OSError, ValueError):
        warned = 0.0
    rows = recent_prompts(cfg, 1e12, now)
    turn_start = max((float(r["ts"]) for r in rows), default=now - 1800)
    if warned >= turn_start:
        return None
    store.write_atomic(mark, str(now))
    return (f"hubkit WARNING INLINE-EDIT: the hub is editing {path} itself. The hub coordinates; a worker edits code with a brief and a clock. "
            "NEXT: route it to a worker with a brief, or if the user asked the hub to do it, go on")


# ---- the wrong account, a yes that came before the draft ----

def account_warning(cfg: dict, command: str) -> str | None:
    """A warning when a command matches an `[[accounts.rules]]` `match` and lacks that rule's `require` (the account flag)."""
    for rule in (cfg.get("accounts") or {}).get("rules") or []:
        try:
            if not re.search(str(rule.get("match") or "(?!)"), command):
                continue
            need = str(rule.get("require") or "")
            if need and re.search(need, command):
                continue
        except re.error:
            continue
        hint = str(rule.get("hint") or "use the account the user named")
        return f"hubkit WARNING WRONG-ACCOUNT: this post would go from the wrong account. NEXT: {hint}"
    return None


def draft_since(cfg: dict) -> float | None:
    """When the open draft work item (title `draft: `) was opened or last asked about again; None when no draft item is open."""
    from . import status

    stamps = [max(float(i.get("added") or 0), float(i.get("touched") or 0)) for i in status.work_load(store.state(cfg))
              if i.get("kind") == "active" and str(i.get("title", "")).startswith("draft: ")]
    return max(stamps) if stamps else None


def touch_draft(cfg: dict) -> None:
    """Stamp the open draft item as changed now: an earlier yes no longer approves it."""
    from . import status

    sd = store.state(cfg)
    with store.locked(sd / "work.lock"):
        all_ = status.work_load(sd)
        for i in all_:
            if i.get("kind") == "active" and str(i.get("title", "")).startswith("draft: "):
                i["touched"] = time.time()
        status.work_save(sd, all_)


def typed_yes(cfg: dict, yes: re.Pattern[str], seconds: float, now: float) -> bool:
    """A typed yes within `seconds`; with a draft item open it must be typed after that draft was opened or last changed."""
    since = draft_since(cfg)
    return any(yes.search(r["text"]) and (since is None or float(r["ts"]) > since) for r in recent_prompts(cfg, seconds, now))


# ---- one agent per host ----

def hosts(cfg: dict) -> list[str]:
    """Host names that hold one agent at a time (`guards.hosts`)."""
    return [str(h) for h in (cfg.get("guards") or {}).get("hosts") or []]


def _names(host: str, text: str) -> bool:
    return bool(re.search(rf"(?<![\w.-]){re.escape(host)}(?![\w-])", text, re.I))


def host_lines(cfg: dict, item: dict, text: str) -> list[str]:
    """Record the configured hosts a new routed brief names on the item; warn for each one another owner's live item holds."""
    named = [h for h in hosts(cfg) if _names(h, text)]
    if not named or item.get("hosts"):
        return []
    from . import status

    sd = store.state(cfg)
    out = []
    with store.locked(sd / "work.lock"):
        all_ = status.work_load(sd)
        for h in named:
            for o in all_:
                if o.get("kind") == "routed" and o["id"] != item["id"] and o["owner"] != item["owner"] and h in (o.get("hosts") or []):
                    out.append(f"hubkit WARNING HOST-BUSY: {h} is already in use by {o['owner']} ({o['id']}). NEXT: pick a free host or wait")
        for o in all_:
            if o["id"] == item["id"]:
                o["hosts"] = named
        status.work_save(sd, all_)
    item["hosts"] = named
    return out


def hosts_in_use_line(cfg: dict) -> str:
    """`hosts in use: host-a (W3 peer-a)` for `hubkit peers ls`, or an empty string."""
    from . import status

    use = [f"{h} ({i['id']} {i['owner']})" for i in status.work_load(store.state(cfg)) if i.get("kind") == "routed" for h in i.get("hosts") or []]
    return "hosts in use: " + ", ".join(use) if use else ""


# ---- a review nobody asked for ----

def unasked_review(cfg: dict, item: dict, now: float) -> str | None:
    """A warning when a new review brief names a PR that no open order, live work item (reply, request, standing work)
    or recent typed prompt (`guards.review_ask_window_min`) names."""
    from . import routed, status
    from .orders import Orders

    nums = [routed.pr_num(u) for u in item.get("prs") or []]
    if not item.get("review") or not nums:
        return None
    window = float((cfg.get("guards") or {}).get("review_ask_window_min", 240)) * 60
    texts = [" ".join([str(o.get("quote", "")), *map(str, o.get("refs") or [])]) for o in Orders(cfg).live()]
    texts += [" ".join([str(i.get("title", "")), str(i.get("note", "")), *map(str, i.get("prs") or [])])
              for i in status.work_load(store.state(cfg)) if i.get("kind") != "routed"]
    texts += [r["text"] for r in recent_prompts(cfg, window, now)]
    missing = [n for n in nums if not any(re.search(rf"(?<!\d){re.escape(n)}(?!\d)", t) for t in texts)]
    if not missing:
        return None
    return (f"hubkit WARNING NO-ASK-REVIEW: nobody asked for a review of {', '.join('#' + n for n in missing)} "
            "(no open order, reply, request or recent typed word names it). NEXT: say who asked, or do not send it")
