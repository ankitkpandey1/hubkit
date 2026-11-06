"""Asks from other people to the user, tracked as `reply` work items so none is forgotten.

`hubkit inbound ingest` reads the inbox watcher's lines, `<id> · <person> · <where> · <text> · <todo|reply>`, and opens one
item per message id the first time it is seen. The item closes with `hubkit work done`. A reply owed past
`watcher.reply_owed_min` raises one alert."""

from __future__ import annotations

import re
import time

from . import status, store
from .orders import PR_REF

LINE = re.compile(r"^\s*(\d+)\s·\s(.+?)\s·\s(.+?)\s·\s(.*)\s·\s(todo|reply)\s*$")


def ingest(cfg: dict, text: str) -> list[str]:
    """Open a reply item for each new line that asks for an answer. Returns one line per item opened."""
    sd = store.state(cfg)
    opened = []
    with store.locked(sd / "work.lock"):
        items = status.work_load(sd)
        have = {i.get("msg_id") for i in items if i.get("kind") == "reply"}
        have |= {r.get("msg_id") for f in (sd / "archive").glob("archive_*.jsonl") for r in store.read_rows(f)[0]}
        for ln in text.splitlines():
            m = LINE.match(ln)
            if not m or m[1] in have:
                continue
            item: dict = {"id": status.next_work_id(sd, items), "kind": "reply", "owner": m[2], "title": m[4][:120], "note": m[3][:80],
                    "added": time.time(), "msg_id": m[1], "prs": PR_REF.findall(m[4])}
            items.append(item)
            have.add(m[1])
            opened.append(f"{item['id']} reply owed to {m[2]}: {item['title'][:60]}")
        status.work_save(sd, items)
    return opened


def alerts(cfg: dict, now: float) -> list[dict]:
    """One alert per reply owed longer than the limit."""
    limit = float(cfg["watcher"]["reply_owed_min"])
    return [{"key": f"{i['id']}:reply_owed", "msg": f"{i['id']} reply owed to {i['owner']} for {int((now - i['added']) / 60)} min ({i['title'][:50]}). NEXT: answer them, then: hubkit work done {i['id']} --note \"<what you sent>\""}
            for i in status.work_load(store.state(cfg)) if i.get("kind") == "reply" and (now - i["added"]) / 60 > limit]
