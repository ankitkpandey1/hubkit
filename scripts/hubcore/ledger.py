"""The day's whole record, not only what the hub routed: PRs the user's account opened, what each peer session was asked,
and a word search over all of it. Read-only on peers; a gh failure is shown, never swallowed.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import config, hooks, routed, store
from .orders import Orders
from .redact import redact
from .status import work_load

PRS_TTL_S = 600
SKIP_PREFIXES = ("<task-notification", "<system-reminder", "<command-")


def _gh_json(cfg: dict, args: list[str]) -> object:
    """Run gh and parse its JSON; raises RuntimeError with the reason when gh is missing, fails or prints no JSON."""
    exe = routed._gh(cfg)
    if exe is None:
        raise RuntimeError("gh not found (set [tools] gh)")
    try:
        r = subprocess.run(
            [exe, *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"gh did not run: {e}") from e
    if r.returncode != 0:
        raise RuntimeError(f"gh {args[0]} failed: {redact(r.stderr.strip())[0][:160]}")
    try:
        return json.loads(r.stdout)
    except ValueError as e:
        raise RuntimeError(f"gh {args[0]} printed no JSON") from e


def fetch_prs(cfg: dict, day: str) -> dict:
    """PRs the user's account opened on or after `day`: number, title, url, body, changed file paths. {'prs': [...], 'error': str}."""
    try:
        found = _gh_json(
            cfg,
            [
                "search",
                "prs",
                "--author",
                "@me",
                "--created",
                f">={day}",
                "--limit",
                "50",
                "--json",
                "number,title,url,body,repository,createdAt",
            ],
        )
        prs = []
        for p in found if isinstance(found, list) else []:
            view = _gh_json(cfg, ["pr", "view", p["url"], "--json", "files"])
            files = (
                [f.get("path", "") for f in view.get("files") or []]
                if isinstance(view, dict)
                else []
            )
            prs.append(
                {
                    "number": p["number"],
                    "title": p["title"],
                    "url": p["url"],
                    "body": p.get("body") or "",
                    "files": files,
                }
            )
        return {"prs": prs, "error": ""}
    except RuntimeError as e:
        return {"prs": [], "error": str(e)}


def prs_since(cfg: dict, day: str) -> dict:
    """fetch_prs cached for ten minutes in the state dir (the cache file is per day)."""
    cache = store.state(cfg) / f"ledger-prs-{day}.json"
    try:
        hit = json.loads(cache.read_text(encoding="utf-8"))
        if time.time() - hit["at"] < PRS_TTL_S and not hit["data"]["error"]:
            return hit["data"]
    except OSError, ValueError, KeyError:
        pass
    data = fetch_prs(cfg, day)
    store.write_atomic(
        cache, json.dumps({"at": time.time(), "data": redact_obj_safe(data)})
    )
    return data


def redact_obj_safe(data: dict) -> dict:
    """The PR text goes through the same redaction as state before it is written."""
    from .redact import redact_obj

    return redact_obj(data, limit=4000)


def _text(msg: object) -> str:
    content = (msg or {}).get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _names(cfg: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in config.sessions_dir(cfg).glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out[str(d.get("sessionId"))] = str(d.get("name") or "")
        except OSError, ValueError:
            continue
    return out


def peer_prompts(cfg: dict, since: float) -> list[dict]:
    """What each other session was asked since `since` (epoch): {ts, session, text}, redacted, oldest first. Reads transcripts only."""
    bind = hooks.read_bind(cfg) or {}
    own, names, out = str(bind.get("sessionId") or ""), _names(cfg), []
    for f in glob.glob(os.path.expanduser(cfg["paths"]["transcripts_glob"])):
        sid = Path(f).stem
        try:
            if sid == own or os.stat(f).st_mtime < since:
                continue
            lines = Path(f).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                o = json.loads(line)
                ts = datetime.fromisoformat(
                    str(o["timestamp"]).replace("Z", "+00:00")
                ).timestamp()
            except KeyError, ValueError:
                continue
            text = _text(o.get("message")).strip() if o.get("type") == "user" else ""
            if ts >= since and text and not text.startswith(SKIP_PREFIXES):
                out.append(
                    {
                        "ts": ts,
                        "session": names.get(sid) or sid[:8],
                        "text": redact(" ".join(text.split()))[0][:300],
                    }
                )
    return sorted(out, key=lambda r: r["ts"])


def day_start(cfg: dict, day: str) -> float:
    """Epoch of 00:00 on `day` in the configured timezone."""
    return (
        datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=store.tzinfo(cfg)).timestamp()
    )


def day_lines(cfg: dict, day: str) -> list[str]:
    """The ledger lines for a day: the user's PRs and what each peer session was asked."""
    data = prs_since(cfg, day)
    out = [f"PRs opened by the user's account since {day}: {len(data['prs'])}"]
    out += [
        f"  #{p['number']} {p['title'][:80]} ({len(p['files'])} files) {p['url']}"
        for p in data["prs"]
    ]
    if data["error"]:
        out.append(f"  PRs UNKNOWN: {data['error']}")
    peers = peer_prompts(cfg, day_start(cfg, day))
    out.append(f"Peer session prompts since {day}: {len(peers)}")
    seen: dict[str, int] = {}
    for r in peers:
        seen[r["session"]] = seen.get(r["session"], 0) + 1
        if seen[r["session"]] <= 3:
            out.append(
                f"  {store.clock(cfg, r['ts'])} {r['session']}: {r['text'][:110]}"
            )
    out += [f"  {s}: {n - 3} more" for s, n in seen.items() if n > 3]
    return out


def _snip(text: str, word: str) -> str:
    i = text.lower().find(word.lower())
    return redact(" ".join(text[max(0, i - 40) : i + 80].split()))[0]


def find(cfg: dict, word: str, span: str = "2d") -> list[str]:
    """Lines `[source] id: ...text...` for every record that holds `word` (case-insensitive): orders, work, the archive,
    the user's asks, peer session prompts, and the user's PRs (title, body, changed file paths).
    """
    from .lessons import parse_span

    sd, now = store.state(cfg), time.time()
    since = now - parse_span(span)
    hits: list[str] = []

    def check(source: str, ident: str, *texts: str) -> None:
        for t in texts:
            if t and word.lower() in t.lower():
                hits.append(f"[{source}] {ident}: {_snip(t, word)}")
                return

    for o in Orders(cfg).all().values():
        check("order", o["id"], o.get("quote", ""), o.get("kind", ""))
    for i in work_load(sd):
        check("work", i["id"], i.get("title", ""), i.get("note", ""))
    for f in sorted(glob.glob(str(sd / "archive" / "archive_*.jsonl"))):
        for r in store.read_rows(Path(f))[0]:
            check(
                "archive",
                str(r.get("id", "?")),
                str(r.get("title", "")),
                str(r.get("note", "")),
                str(r.get("done_note", "")),
                str(r.get("quote", "")),
            )
    for r in store.read_rows(sd / "recent-prompts.jsonl")[0]:
        if r["ts"] >= since:
            check("ask", store.clock(cfg, r["ts"]), r.get("text", ""))
    for r in peer_prompts(cfg, since):
        check("peer", f"{r['session']} {store.clock(cfg, r['ts'])}", r["text"])
    data = prs_since(cfg, store.day_of(cfg, since))
    for p in data["prs"]:
        check(
            "pr",
            f"#{p['number']} {p['url']}",
            p["title"],
            p["body"],
            " ".join(p["files"]),
        )
    if data["error"]:
        hits.append(f"[pr] UNKNOWN, PRs could not be searched: {data['error']}")
    return hits or [f"no record holds {word!r} in the last {span}"]
