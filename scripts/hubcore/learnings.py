"""Learnings log: one block per class of mistake. Locked, atomic, redacted."""

from __future__ import annotations

import contextlib
import hashlib
import re
from datetime import datetime
from pathlib import Path

from . import config, store
from .redact import redact

HEADER_RE = re.compile(r"^## (?P<cls>[a-z0-9][a-z0-9-]*) \| gate: (?P<gate>.+)$")
SEEN_RE = re.compile(r"^  - (\d{4}-\d{2}-\d{2}) (.*)$")
BASIS_RE = re.compile(r"^- basis: (\w+)$")
ADDR_RE = re.compile(r"^- addressed: (\d{4}-\d{2}-\d{2}) (.*)$")
PREAMBLE = (
    "# Hub learnings\n\n"
    "One block per class of mistake. A repeat appends to `seen`.\n"
    "A class seen 2+ times is promoted into the overlay (`learn.py promote-candidates`, then `learn.py promote`).\n"
)


def default_file(cfg: dict) -> Path:
    """The learnings file: paths.learnings, else <state dir>/learnings.md."""
    return config.path_of(cfg, "learnings", store.state(cfg) / "learnings.md")


def read_blocks(path: Path) -> tuple[str, list[list[str]]]:
    """Split the learnings file into its preamble and one block of lines per class."""
    if not path.exists():
        return PREAMBLE, []
    pre: list[str] = []
    blocks: list[list[str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
        else:
            pre.append(line)
    return ("\n".join(pre) + "\n" if pre else PREAMBLE), blocks


def write_blocks(path: Path, pre: str, blocks: list[list[str]]) -> None:
    """Write the preamble and blocks back atomically."""
    body = "\n\n".join("\n".join(b).rstrip() for b in blocks)
    store.write_atomic(path, pre.rstrip() + "\n\n" + body + "\n")


def block_class(block: list[str]) -> str:
    """The class name of a block (from its header line)."""
    m = HEADER_RE.match(block[0])
    return m["cls"] if m else ""


def seen_lines(block: list[str]) -> list[str]:
    """The dated `seen` lines of a block."""
    return [ln for ln in block if SEEN_RE.match(ln)]


def seen_dates(block: list[str]) -> list[str]:
    """The dates a block's class was seen."""
    return [m[1] for ln in seen_lines(block) if (m := SEEN_RE.match(ln))]


def addressed(block: list[str]) -> tuple[str, str] | None:
    """(date, how) when the block is marked addressed, else None."""
    for ln in block:
        if m := ADDR_RE.match(ln):
            return m[1], m[2]
    return None


def field(block: list[str], name: str) -> str:
    """The text of a `- name: ...` line in a block, or an empty string."""
    for ln in block:
        if ln.startswith(f"- {name}: "):
            return ln[len(name) + 4 :]
    return ""


def clean(text: str) -> str:
    """Collapse whitespace and redact secrets from user-supplied text."""
    return redact(" ".join(text.split()))[0]


def locked_edit(path: Path) -> contextlib.AbstractContextManager[bool]:
    """Exclusive lock around an edit of `path`."""
    return store.locked(path.with_name(f".{path.name}.lock"))


def overlay_path(cfg: dict) -> Path:
    """The overlay file (personal rules): paths.overlay, else <config dir>/overlay.md."""
    return config.path_of(cfg, "overlay", Path(cfg["_config_dir"]) / "overlay.md")


def rules_hash(cfg: dict) -> str:
    """Hash of the rule texts a learning can be promoted into (overlay + SKILL.md)."""
    h = hashlib.sha1()
    for f in (overlay_path(cfg), config.SKILL_DIR / "SKILL.md"):
        h.update(f.read_bytes() if f.exists() else b"")
    return h.hexdigest()[:12]


def _set_basis(block: list[str], basis: str) -> None:
    if not basis:
        return
    block[:] = [ln for ln in block if not BASIS_RE.match(ln)]
    block.insert(next(i for i, ln in enumerate(block) if ln == "- seen:"), f"- basis: {basis}")


def add(path: Path, cls: str, quote: str, day: str, gate: str = "?", basis: str = "", **fields: str) -> str:
    """Record a correction: new block for a new class, a dated `seen` line for a repeat."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", cls):
        raise ValueError("--class must be kebab-case (a-z, 0-9, -)")
    q = clean(quote).replace('"', "'")
    seen = f'  - {day} "{q}"'
    with locked_edit(path):
        pre, blocks = read_blocks(path)
        for block in blocks:
            if block_class(block) != cls:
                continue
            if seen in block:
                return f"skip: duplicate of {cls}"
            idx = max(i for i, ln in enumerate(block) if SEEN_RE.match(ln))
            block.insert(idx + 1, seen)
            _set_basis(block, basis)
            write_blocks(path, pre, blocks)
            return f"repeat: {cls} now seen {len(seen_lines(block))}x"
        for name in ("situation", "wrong", "right", "evidence"):
            if not fields.get(name):
                raise ValueError(f"new class needs --{name}")
        blocks.append(
            [f"## {cls} | gate: {clean(gate)}"]
            + [f"- {n}: {clean(fields[n])}" for n in ("situation", "wrong", "right", "evidence")]
            + ["- seen:", seen]
        )
        _set_basis(blocks[-1], basis)
        write_blocks(path, pre, blocks)
    return f"added: {cls}"


def mark_addressed(path: Path, cls: str, day: str, how: str, current: str, rules_mtime: float, rule_text: str = "") -> str:
    """Mark a class fixed. Refused unless the overlay or SKILL.md changed since the entry was seen."""
    with locked_edit(path):
        pre, blocks = read_blocks(path)
        for block in blocks:
            if block_class(block) != cls:
                continue
            basis = next((m[1] for ln in block if (m := BASIS_RE.match(ln))), "")
            if basis:
                changed = current != basis
            else:  # old entry without a basis: file must be newer than the last sighting's day
                last = last_seen(block)
                changed = bool(last) and rules_mtime >= datetime.strptime(last, "%Y-%m-%d").timestamp()
            if f"[{cls}]" not in rule_text:
                raise ValueError(f"refused: no rule line tagged [{cls}] in the overlay or SKILL.md; run learn.py promote first")
            if not changed:
                raise ValueError(f"refused: overlay and SKILL.md unchanged since {cls} was seen; promote the rule first")
            block[:] = [ln for ln in block if not ADDR_RE.match(ln)]
            block.insert(next(i for i, ln in enumerate(block) if ln == "- seen:"), f"- addressed: {day} {clean(how)}")
            write_blocks(path, pre, blocks)
            return f"addressed: {cls}"
    raise KeyError(f"no class {cls}")


PROMOTED = "## Promoted rules (learn.py promote)"


def promote(path: Path, overlay: Path, cls: str, gate: str, rule: str) -> str:
    """Write one short rule line into the overlay under its gate heading. User data stays out of SKILL.md."""
    _, blocks = read_blocks(path)
    if not any(block_class(b) == cls for b in blocks):
        raise KeyError(f"no class {cls}")
    if not re.fullmatch(r"G[1-9]", gate):
        raise ValueError("--gate must look like G1..G9")
    line = f"- {clean(rule)} [{cls}]"
    with locked_edit(overlay):
        lines = overlay.read_text(encoding="utf-8").splitlines() if overlay.exists() else []
        if line in lines:
            return f"skip: {cls} already promoted"
        if PROMOTED not in lines:
            lines += ["", PROMOTED, ""]
        head = f"### {gate}"
        start = lines.index(PROMOTED)
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        if head not in lines[start:end]:
            lines[end:end] = [head, ""]
            end += 2
        at = lines.index(head, start)
        stop = next((i for i in range(at + 1, len(lines)) if lines[i].startswith("#")), len(lines))
        while stop > at + 1 and not lines[stop - 1].strip():
            stop -= 1
        lines.insert(stop, line)
        store.write_atomic(overlay, "\n".join(lines) + "\n")
    return f"promoted: {cls} -> {gate}"


def last_seen(block: list[str]) -> str:
    """Latest date a block was seen, or an empty string."""
    return max(seen_dates(block), default="")


def recent(path: Path, n: int) -> list[list[str]]:
    """The `n` most recently seen blocks."""
    _, blocks = read_blocks(path)
    return sorted(blocks, key=last_seen)[-n:]


def review(path: Path, day: str) -> dict[str, list[str]]:
    """Compare learnings with the day: new today, repeated today, and addressed status."""
    _, blocks = read_blocks(path)
    out: dict[str, list[str]] = {
        "new": [], "repeated": [], "regressed": [], "holding": [], "unaddressed": []
    }
    for b in blocks:
        cls, dates = block_class(b), seen_dates(b)
        if dates and min(dates) == day:
            out["new"].append(cls)
        elif day in dates:
            out["repeated"].append(cls)
        addr = addressed(b)
        if addr:
            after = [d for d in dates if d > addr[0]]
            (out["regressed"] if after else out["holding"]).append(
                f"{cls} (addressed {addr[0]}" + (f", seen again {max(after)})" if after else ")")
            )
        elif len(dates) >= 2:
            out["unaddressed"].append(f"{cls} ({len(dates)}x)")
    return out
