"""Playbooks: how the user's recurring commands are done, saved once and replayed.

One file per playbook at `<config dir>/playbooks/<name>.md`: a frontmatter block (`name`, `trigger` lines, `updated`,
`quote` lines) and numbered steps. A human prompt that matches a trigger gets the steps injected verbatim. A trigger is a
word or phrase (whole words, any case), or a regex when it starts with `re:`. `hubkit how save` writes one; the
hooks and the stop gate use `matched_this_turn` / `missing` to make sure a command with no playbook leaves one behind."""

from __future__ import annotations

import re
import time
from pathlib import Path

from . import config, store

NAME = re.compile(r"[a-z0-9][a-z0-9-]*")


def pdir() -> Path:
    return config.config_path().parent / "playbooks"


def _parse(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    head, _, body = text.removeprefix("---\n").partition("\n---\n")
    out: dict = {"name": path.stem, "triggers": [], "quotes": [], "updated": "", "steps": body.strip()}
    for ln in head.splitlines():
        key, _, val = ln.partition(": ")
        if key == "name":
            out["name"] = val.strip()
        elif key == "trigger":
            out["triggers"].append(val.strip())
        elif key == "quote":
            out["quotes"].append(val.strip())
        elif key == "updated":
            out["updated"] = val.strip()
    return out


def _write(path: Path, pb: dict) -> None:
    head = ["---", f"name: {pb['name']}", *(f"trigger: {t}" for t in pb["triggers"]), f"updated: {pb['updated']}",
            *(f"quote: {' '.join(q.split())}" for q in pb["quotes"]), "---"]
    store.write_atomic(path, "\n".join(head) + "\n" + pb["steps"].strip() + "\n")


def load_all() -> list[dict]:
    """Every playbook, by name."""
    return [_parse(p) for p in sorted(pdir().glob("*.md"))]


def get(name: str) -> dict:
    path = pdir() / f"{name}.md"
    if not path.exists():
        raise KeyError(f"no playbook {name}; see: hubkit how ls")
    return _parse(path)


def _hits(trigger: str, text: str) -> bool:
    if trigger.startswith("re:"):
        return bool(re.search(trigger[3:], text, re.I))
    return bool(re.search(rf"(?<!\w){re.escape(trigger)}(?!\w)", text, re.I))


def match(text: str) -> list[dict]:
    """Playbooks with a trigger in `text`."""
    return [pb for pb in load_all() if any(_hits(t, text) for t in pb["triggers"])]


def save(name: str, triggers: list[str], steps: str, quotes: list[str], replace: bool = False) -> str:
    """Write a playbook. An existing one is replaced only with `replace`, and the old file is kept as `.md.bak-<date>`."""
    if not NAME.fullmatch(name):
        raise ValueError("playbook name must be kebab-case (a-z, 0-9, -)")
    if not triggers or not steps.strip():
        raise ValueError("a playbook needs at least one --trigger and steps")
    for t in triggers:
        if t.startswith("re:"):
            re.compile(t[3:])
    path = pdir() / f"{name}.md"
    day = time.strftime("%Y-%m-%d")
    if path.exists():
        if not replace:
            raise ValueError(f"playbook {name} exists; pass --replace to overwrite (the old one is kept as {path.name}.bak-{day})")
        path.replace(path.with_name(f"{path.name}.bak-{day}"))
    pdir().mkdir(parents=True, exist_ok=True)
    _write(path, {"name": name, "triggers": triggers, "updated": day, "quotes": quotes, "steps": steps})
    return f"saved playbook {name} ({len(triggers)} triggers)"


def add_quote(name: str, quote: str) -> None:
    pb = get(name)
    pb["quotes"].append(quote[:200])
    pb["updated"] = time.strftime("%Y-%m-%d")
    _write(pdir() / f"{name}.md", pb)


def inject(pbs: list[dict]) -> list[str]:
    return [f"PLAYBOOK {pb['name']}: follow these steps\n{pb['steps']}" for pb in pbs]


# ---- per-turn memory: what matched, and what has no playbook yet ----

def note_turn(cfg: dict, names: list[str]) -> None:
    store.write_atomic(store.state(cfg) / "playbook-turn.json", ",".join(names))


def matched_this_turn(cfg: dict) -> list[str]:
    p = store.state(cfg) / "playbook-turn.json"
    return [n for n in p.read_text(encoding="utf-8").split(",") if n] if p.exists() else []


def note_missing(cfg: dict, cmd: str, now: float) -> None:
    store.append_row(store.state(cfg) / "playbook-missing.jsonl", {"ts": now, "cmd": store.safe(cmd, 80)})


def note_saved(cfg: dict, now: float) -> None:
    store.append_row(store.state(cfg) / "playbook-saved.jsonl", {"ts": now})


def missing(cfg: dict) -> str | None:
    """The command of a prompt that matched no playbook, when an order has closed or work been archived since and no
    playbook was saved since: the steps were done but not kept."""
    sd = store.state(cfg)
    asked = store.read_rows(sd / "playbook-missing.jsonl")[0]
    if not asked:
        return None
    last = asked[-1]
    saved = max([r["ts"] for r in store.read_rows(sd / "playbook-saved.jsonl")[0]], default=0.0)
    if saved >= last["ts"]:
        return None
    done = [r for r in store.read_rows(sd / "orders.jsonl")[0] if r.get("op") == "close"]
    done += [r for f in (sd / "archive").glob("archive_*.jsonl") for r in store.read_rows(f)[0] if r.get("type") == "work"]
    return last["cmd"] if any(r.get("ts", 0) >= last["ts"] for r in done) else None
