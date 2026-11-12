"""Standing rules: instructions the user gave once that apply every time ("when peer-a approves, merge it").

A rule is saved with the user's own quote (required), a trigger and an action text:

- `approved`: a counting non-author review that approves a tracked PR at its current head (optionally scoped to a repo or one PR).
- `inbound`: a new inbound ask from `hubkit inbound ingest` (optionally scoped to a person and/or a word in the ask).

When a trigger fires, once per PR head or message, hubkit opens an order (when the action names an order kind, or the rule
gives `--kind`) or an `active` work item, carrying the quote, and prints one line with the exact NEXT command (for a merge it
holds the head sha: `--match-head-commit <sha>`). hubkit never runs the action; the hub does. A rule with `--until` stops
firing after that moment. State lives in `standing.json` (rules and the fired record)."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

from . import replies, routed, status, store
from .orders import LIVE_STATES, PR_URL, Orders, _word_re, match_kind
from .redact import redact_obj

TRIGGERS = ("approved", "inbound")
MERGE = re.compile(r"(?i)\bmerge\b")
KEEP_FIRED = 200


def _opts(cfg: dict) -> dict:
    return cfg["standing"]


def _path(cfg: dict) -> Path:
    return store.state(cfg) / "standing.json"


def load(cfg: dict) -> dict:
    """The saved rules and the fired record (empty when none)."""
    try:
        data = json.loads(_path(cfg).read_text(encoding="utf-8"))
    except OSError, ValueError:
        data = {}
    return {"seq": int(data.get("seq", 0)), "rules": list(data.get("rules", [])), "fired": list(data.get("fired", []))}


def _save(cfg: dict, data: dict) -> None:
    data["fired"] = data["fired"][-KEEP_FIRED:]
    store.write_json_atomic(_path(cfg), data)


def _lock(cfg: dict):
    return store.locked(store.state(cfg) / "standing.lock")


def parse_until(cfg: dict, text: str) -> float:
    """`2026-12-01` (to the end of that day) or `2026-12-01 18:00`, in the user's zone, as an epoch."""
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            d = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d":
            d = d.replace(hour=23, minute=59, second=59)
        return d.replace(tzinfo=store.tzinfo(cfg)).timestamp()
    raise ValueError("--until needs a date like 2026-12-01 or 2026-12-01 18:00 (in the user's zone)")


def add(cfg: dict, trigger: str, quote: str, action: str, *, repo: str = "", pr: str = "", person: str = "", word: str = "",
        until: str = "", cmd: str = "", kind: str = "", now: float | None = None) -> dict:
    """Save one rule. The user's quote and the action are required; scopes must fit the trigger."""
    now = now or time.time()
    if trigger not in TRIGGERS:
        raise ValueError(f"--on must be one of {', '.join(TRIGGERS)}")
    if len(quote.strip()) < 6:
        raise ValueError("standing add needs --quote: the user's own words that gave this rule")
    if not action.strip():
        raise ValueError("standing add needs --action: what to do when it fires")
    if trigger == "approved" and (person or word):
        raise ValueError("--from and --word scope an inbound rule; an approved rule takes --repo or --pr")
    if trigger == "inbound" and (repo or pr):
        raise ValueError("--repo and --pr scope an approved rule; an inbound rule takes --from or --word")
    if repo and not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("--repo is owner/name, for example acme/repo")
    pr_url = ""
    if pr:
        found = routed.refs(cfg, pr if "/" in pr or pr.startswith("#") else f"#{pr}")
        if not found or not found[0].startswith("http"):
            raise ValueError("--pr needs a PR URL (or a number with [routed] repo set)")
        pr_url = found[0]
    if kind and kind not in Orders(cfg).intents.get("kinds", {}):
        raise ValueError(f"--kind must be an order kind from intents.toml: {', '.join(Orders(cfg).intents.get('kinds', {}))}")
    ends = parse_until(cfg, until) if until else 0.0
    if ends and ends <= now:
        raise ValueError("--until is already past")
    with _lock(cfg):
        data = load(cfg)
        data["seq"] += 1
        rule = redact_obj({"id": f"S{data['seq']}", "on": trigger, "quote": store.safe(quote, 300), "action": store.safe(action, 200),
                           "repo": repo, "pr": pr_url, "person": person.strip(), "word": word.strip(), "until": ends,
                           "cmd": store.safe(cmd, 300), "kind": kind, "added": now})
        data["rules"].append(rule)
        _save(cfg, data)
    return rule


def drop(cfg: dict, rid: str, quote: str, now: float | None = None) -> str:
    """Remove a rule; only the user's own words may (the quote is kept in the record)."""
    if len(quote.strip()) < 4:
        raise ValueError("standing drop needs --quote: the user's words that cancel it")
    with _lock(cfg):
        data = load(cfg)
        hit = next((r for r in data["rules"] if r["id"] == rid), None)
        if hit is None:
            raise KeyError(f"no standing rule {rid}")
        data["rules"] = [r for r in data["rules"] if r["id"] != rid]
        store.append_row(store.state(cfg) / "standing-dropped.jsonl", {**hit, "dropped": now or time.time(), "dropped_quote": quote[:300]})
        _save(cfg, data)
    return f"{rid} dropped"


def expired(rule: dict, now: float) -> bool:
    return bool(rule.get("until")) and now > float(rule["until"])


def active(cfg: dict, now: float, trigger: str = "") -> list[dict]:
    """Rules that can fire now: saved, not expired, of this trigger when one is given."""
    return [r for r in load(cfg)["rules"] if not expired(r, now) and (not trigger or r["on"] == trigger)]


def _scope(rule: dict) -> str:
    bits = [f"repo {rule['repo']}" if rule.get("repo") else "", f"PR {rule['pr']}" if rule.get("pr") else "",
            f"from {rule['person']}" if rule.get("person") else "", f"word '{rule['word']}'" if rule.get("word") else ""]
    return ", ".join(b for b in bits if b) or "any"


def ls_lines(cfg: dict, now: float) -> list[str]:
    data = load(cfg)
    if not data["rules"]:
        return ["no standing rules. NEXT: hubkit standing add --on approved --quote \"<the user's words>\" --action \"<what to do>\""]
    out = [f"{len(data['rules'])} standing rule(s)"]
    for r in data["rules"]:
        n = sum(f["rule"] == r["id"] for f in data["fired"])
        end = (f", until {store.day_of(cfg, r['until'])} {store.clock(cfg, r['until'])}" + (" EXPIRED" if expired(r, now) else "")) if r.get("until") else ""
        out.append(f"{r['id']} on {r['on']} ({_scope(r)}{end}): {r['action']} | the user said: \"{r['quote'][:100]}\" | fired {n}x")
    return out


# ---- firing ----

def _close_hint(kind: str, ref: str) -> str:
    return (f"hubkit order close {ref} --evidence name=value" if kind == "order" else f"hubkit work done {ref} --note \"<what was done>\"")


def _fire(cfg: dict, rule: dict, key: str, what: str, links: str, next_cmd: str, now: float, prompted: bool) -> str | None:
    """Open the order or work item for one firing, once per key. Returns the line, or None when this key already fired."""
    with _lock(cfg):
        data = load(cfg)
        if any(f["key"] == key for f in data["fired"]):
            return None
        orders = Orders(cfg)
        kind = rule.get("kind") or match_kind(orders.intents, rule["action"])
        ctx = f" [standing {rule['id']}: {what}]"
        if kind:
            o = orders.open(kind, rule["quote"][:max(20, 300 - len(ctx))] + ctx, now=now)
            ref, how = o["id"], "order"
        else:
            w = status.work_add(cfg, "active", f"standing {rule['id']}: {rule['action'][:60]} ({what})"[:160], note=f"the user said: {rule['quote'][:200]}",
                                estimate=int(_opts(cfg)["estimate_min"]))
            ref, how = w["id"], "work"
        data["fired"].append(redact_obj({"rule": rule["id"], "key": key, "how": how, "ref": ref, "what": what, "links": links,
                                         "next": next_cmd, "ts": now, "prompted": prompted}))
        _save(cfg, data)
    from . import threads

    threads.auto(cfg, ref, allow_root=True)  # what a firing opens joins the thread of the PR it is about
    return (f"hubkit STANDING {rule['id']} fired: {what}. The user said: \"{rule['quote'][:140]}\". Opened {how} {ref}. "
            f"NEXT: {next_cmd}; when done: {_close_hint(how, ref)}")


def _approvers(cfg: dict, info: dict) -> list[str]:
    """Logins whose latest counting review is an approval at the PR's current head."""
    last: dict[str, dict] = {}
    for r in sorted(info["reviews"], key=lambda r: r["at"]):
        if routed.counts_as_review(cfg, r, info):
            last[r["login"]] = r
    return [login for login, r in last.items() if r["state"].upper() == "APPROVED" and routed.same_head(r["commit"], info["head"])]


def _fits(rule: dict, url: str) -> bool:
    if rule.get("pr") and not routed.same_pr(rule["pr"], url):
        return False
    return not rule.get("repo") or f"/{rule['repo'].lower()}/pull/" in url.lower()


def _fill(template: str, **kw: str) -> str:
    """A command template with {pr} {sha} {repo} (approved) or {person} {where} {msg} (inbound); a stray brace leaves it as typed."""
    try:
        return template.format(**kw)
    except KeyError, IndexError, ValueError:
        return template


def _approved_cmd(cfg: dict, rule: dict, url: str, sha: str, ci: str) -> str:
    if rule.get("cmd"):
        base = _fill(rule["cmd"], pr=url, sha=sha, repo=rule.get("repo", ""))
    elif MERGE.search(rule["action"]):
        base = _fill(str(_opts(cfg)["approved_cmd"]), pr=url, sha=sha, repo=rule.get("repo", ""))
    else:
        base = f"{rule['action']} (PR {url}, head {sha})"
    if ci in ("failing", "pending"):
        return f"CI is {ci} at {sha[:7]}: do not act on red or unfinished checks; get CI green first, then {base}"
    return base


def tracked_prs(cfg: dict, orders: Orders) -> set[str]:
    """PRs hubkit already tracks: routed items, reply items, live orders, and any PR a rule names."""
    urls = {u for i in routed.items(cfg) for u in [*i.get("prs", []), *i.get("queue", [])]} | replies.pr_urls(cfg)
    for o in orders.live():
        urls |= set(PR_URL.findall(" ".join(o.get("refs") or [])))
    return {u for u in urls if u.startswith("http")}


def tick(cfg: dict, orders: Orders, now: float) -> list[dict]:
    """Approved rules: read each tracked PR a rule can apply to (one gh read each, at most `standing.max_reads`) and fire
    for an approval at the current head. Returns the alerts for firings made now."""
    rules = active(cfg, now, "approved")
    if not rules:
        return []
    urls = sorted(tracked_prs(cfg, orders) | {r["pr"] for r in rules if r.get("pr")})
    out: list[dict] = []
    timeout = float(_opts(cfg)["gh_timeout_s"])
    for url in [u for u in urls if any(_fits(r, u) for r in rules)][:int(_opts(cfg)["max_reads"])]:
        info = routed.pr_info(cfg, url, timeout)
        if not info or info.get("state") != "OPEN":
            continue
        who = _approvers(cfg, info)
        if not who:
            continue
        sha = info["head"]
        for r in rules:
            if not _fits(r, url):
                continue
            what = f"{who[0]} approved {url} at {sha[:7]}"
            line = _fire(cfg, r, f"{r['id']}:{url}@{sha}", what, url, _approved_cmd(cfg, r, url, sha, info.get("ci", "none")), now, False)
            if line:
                out.append({"key": f"{r['id']}:standing:{routed.pr_num(url)}:{sha[:8]}", "msg": line})
    return out


def on_inbound(cfg: dict, items: list[dict], now: float | None = None) -> list[str]:
    """New inbound asks (the reply items just opened): fire each matching `inbound` rule once per message."""
    now = now or time.time()
    out = []
    for rule in active(cfg, now, "inbound"):
        for i in items:
            if rule.get("person") and rule["person"].lower() not in str(i.get("owner", "")).lower():
                continue
            if rule.get("word") and not _word_re([rule["word"]]).search(f"{i.get('title', '')} {i.get('note', '')}"):
                continue
            who = f"{i.get('owner', '?')} asked in {i.get('note', '?')}: \"{str(i.get('title', ''))[:80]}\""
            cmd = _fill(rule["cmd"], person=str(i.get("owner", "")), where=str(i.get("note", "")), msg=str(i.get("msg_id", ""))) if rule.get("cmd") else rule["action"]
            line = _fire(cfg, rule, f"{rule['id']}:msg:{i.get('msg_id') or i.get('id')}", who, "", cmd, now, True)
            if line:
                out.append(line)
    return out


# ---- what the hub sees ----

def unacted(cfg: dict) -> list[dict]:
    """Fired rules whose order or work item is still open."""
    data = load(cfg)
    if not data["fired"]:
        return []
    orders = {o["id"] for o in Orders(cfg).all().values() if o["state"] in LIVE_STATES}
    work = {i["id"] for i in status.work_load(store.state(cfg))}
    return [f for f in data["fired"] if f["ref"] in (orders if f["how"] == "order" else work)]


def block_lines(cfg: dict, now: float) -> list[str]:
    """One status line: how many rules are active and how many fired ones wait to be acted on."""
    n = len(active(cfg, now))
    if not n:
        return []
    waiting = len(unacted(cfg))
    return [f"standing rules: {n} active (hubkit standing ls)" + (f"; {waiting} fired, not acted on (hubkit mine)" if waiting else "")]


def prompt_lines(cfg: dict) -> list[str]:
    """Each firing the hub has not been told about at a prompt yet, once (a tick fired it while the user was typing elsewhere)."""
    with _lock(cfg):
        data = load(cfg)
        fresh = [f for f in data["fired"] if not f.get("prompted")]
        if not fresh:
            return []
        orders = {o["id"] for o in Orders(cfg).all().values() if o["state"] in LIVE_STATES}
        work = {i["id"] for i in status.work_load(store.state(cfg))}
        for f in fresh:
            f["prompted"] = True
        _save(cfg, data)
    return [f"hubkit STANDING {f['rule']} fired: {f['what']}. NEXT: {f['next']}" for f in fresh if f["ref"] in (orders if f["how"] == "order" else work)]


def mine_rows(cfg: dict, now: float) -> list[str]:
    """Rows for `hubkit mine` under the hub: fired, not yet acted on."""
    from .owed import age_text

    return [f"  {f['rule']} fired {age_text(now - f['ts'])} ago ({f['how']} {f['ref']}): {f['what'][:80]}. NEXT: {f['next'][:160]}" for f in unacted(cfg)]
