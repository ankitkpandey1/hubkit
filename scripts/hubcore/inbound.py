"""Asks from other people to the user, tracked as `reply` work items so none is forgotten.

`hubkit inbound ingest` reads the inbox watcher's lines, `<id> · <person> · <where> · <text> · <todo|reply>`, and opens one
item per message id the first time it is seen. The item closes with `hubkit work done`. A reply owed past
`watcher.reply_owed_min` (or, with enough history, the person's own median wait) raises one alert; an ask whose PRs
are all merged, closed or reviewed at their head is "answer due now". `inbound ls` and `inbound replied` live in replies.py."""

from __future__ import annotations

import re
import time

from . import replies, routed, status, store

LINE = re.compile(r"^\s*(\d+)\s·\s(.+?)\s·\s(.+?)\s·\s(.*)\s·\s(todo|reply)\s*$")


def ingest(cfg: dict, text: str) -> list[str]:
    """Open a reply item for each new line that asks for an answer. Returns one line per item opened."""
    sd = store.state(cfg)
    opened = []
    fresh: list[dict] = []
    with store.locked(sd / "work.lock"):
        items = status.work_load(sd)
        have = {i.get("msg_id") for i in items if i.get("kind") == "reply"}
        have |= {r.get("msg_id") for f in (sd / "archive").glob("archive_*.jsonl") for r in store.read_rows(f)[0]}
        for ln in text.splitlines():
            m = LINE.match(ln)
            if not m or m[1] in have:
                continue
            item: dict = {"id": status.next_work_id(sd, items), "kind": "reply", "owner": m[2], "title": m[4][:120], "note": m[3][:80],
                    "added": time.time(), "msg_id": m[1], "prs": routed.refs(cfg, m[4])}
            items.append(item)
            have.add(m[1])
            fresh.append(item)
            opened.append(f"{item['id']} reply owed to {m[2]}: {item['title'][:60]}")
        status.work_save(sd, items)
    from . import standrules, threads

    for i in fresh:  # an ask that names a PR joins the thread of the work on it
        opened += threads.auto(cfg, i["id"])
    return opened + standrules.on_inbound(cfg, fresh)  # standing rules fire after the work lock is released


def alerts(cfg: dict, now: float) -> list[dict]:
    """An answer due now alerts at once; a reply owed past the person's own limit alerts once (see replies.alerts)."""
    return replies.alerts(cfg, now)
