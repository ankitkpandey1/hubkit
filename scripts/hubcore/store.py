"""Append-only JSONL, flock, atomic replace, human-prompt test, time helpers."""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
import unicodedata
from collections.abc import Iterator
from datetime import datetime
from datetime import tzinfo as TzInfo
from pathlib import Path

from . import compat, config
from .redact import redact_obj

# Harness and peer wrappers. The test is a denylist on the whole normalised text, plus: any text
# that starts with a tag is not a typed prompt, and a payload origin other than "human" is rejected.
HARNESS_PREFIXES = ("caveat", "this session is being continued", "/compact", "[artifact", "[system", "[cross-session")
HARNESS_MARKERS = (
    "cross-session-message",
    "another-claude-session-sent-a-message",
    "system-notification",
    "task-notification",
    "system-reminder",
    "teammate-message",
    "local-command-stdout",
    "command-name",
    "bash-input",
    "bash-stdout",
    "artifact-content-authored-by",
)


class StateError(RuntimeError):
    pass


def _normalise(text: str) -> str:
    """NFKC, all dash look-alikes to '-', zero-width and format characters removed, lower case."""
    out = []
    for ch in unicodedata.normalize("NFKC", text):
        cat = unicodedata.category(ch)
        if cat == "Cf":
            continue
        out.append("-" if cat == "Pd" else ch)
    return "".join(out).lower()


def is_human(text: str, origin: str | None = None) -> bool:
    """True only for a typed prompt. origin, when the payload has it, must be 'human'."""
    if origin is not None and origin != "human":
        return False
    t = re.sub(r"</?pasted_content[^>]*>", " ", _normalise(text)).lstrip()  # a typed paste is human
    if not t or t.startswith(HARNESS_PREFIXES) or re.match(r"<[a-z/!?]", t):
        return False
    squashed = re.sub(r"[\s_]+", "-", t)
    return not any(m in squashed for m in HARNESS_MARKERS)


def _inside_git(path: Path) -> bool:
    p = path.resolve()
    return any((q / ".git").exists() for q in [p, *p.parents])


def state(cfg: dict, create: bool = True) -> Path:
    """The state directory (created on demand); refuses one inside a git repository or the skill dir."""
    d = config.state_dir(cfg)
    if d.resolve().is_relative_to(config.SKILL_DIR):
        raise StateError(f"state dir {d} is inside the skill dir")
    if _inside_git(d):
        raise StateError(f"state dir {d} is inside a git repository")
    if create:
        for sub in ("", "seen", "archive", "eod", "payload-samples"):
            (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def tzinfo(cfg: dict) -> TzInfo | None:
    """The configured timezone, else the system one."""
    name = cfg["user"].get("timezone", "")
    if name:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo


def day_of(cfg: dict, epoch: float) -> str:
    """YYYY-MM-DD of an epoch in the configured timezone."""
    return datetime.fromtimestamp(epoch, tzinfo(cfg)).strftime("%Y-%m-%d")


def clock(cfg: dict, epoch: float) -> str:
    """HH:MM of an epoch in the configured timezone."""
    return datetime.fromtimestamp(epoch, tzinfo(cfg)).strftime("%H:%M")


def append_row(path: Path, row: dict) -> None:
    """Redact, then one os.write on an O_APPEND fd. No lock needed for a pure append."""
    row = redact_obj(row)
    row.setdefault("ts", time.time())
    data = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def read_rows(path: Path) -> tuple[list[dict], int]:
    """Return (rows, bad_lines). A torn or corrupt line is skipped and counted."""
    rows: list[dict] = []
    bad = 0
    if not path.exists():
        return rows, bad
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(obj, dict):
            rows.append(obj)
        else:
            bad += 1
    return rows, bad


@contextlib.contextmanager
def locked(path: Path, blocking: bool = True, retries: int = 3) -> Iterator[bool]:
    """flock on a lock file. Yields True when held. Non-blocking mode retries then yields False."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    got = False
    try:
        for _ in range(retries if not blocking else 1):
            got = compat.lock_exclusive(fd, wait=blocking)
            if got:
                break
            time.sleep(0.05)
        yield got
    finally:
        os.close(fd)


def write_atomic(path: Path, text: str) -> None:
    """Write text via a temp file and rename."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_json_atomic(path: Path, obj: object, indent: int | None = 1) -> None:
    """Redact every string, then write atomically (status.json, work.json)."""
    write_atomic(path, json.dumps(redact_obj(obj, 2000), indent=indent, ensure_ascii=False) + "\n")


def safe(text: str, n: int = 300) -> str:
    """Redact first, then truncate, so a cut secret cannot leak a prefix."""
    from .redact import redact

    return redact(text)[0][:n]


def touch_seen(sd: Path, name: str) -> None:
    """Touch a heartbeat file under seen/."""
    (sd / "seen").mkdir(exist_ok=True)
    (sd / "seen" / name).touch()
