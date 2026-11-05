"""Secret redaction. Every write to state goes through redact_obj (see store.append_row)."""

from __future__ import annotations

import re
from typing import cast

PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@]+@"),
    re.compile(r"(?i)\b(?:authorization:\s*)?bearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|auth|credential"
        r"|private[_-]?key|access[_-]?key)[\w.-]*\s*[=:]\s*\S+"
    ),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"glpat-[A-Za-z0-9_-]{15,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"(?<![A-Za-z0-9+])(?=[A-Za-z0-9+]*\d)(?=[A-Za-z0-9+]*[A-Za-z])[A-Za-z0-9+]{32,}"),
]
QUOTE_MAX = 300


def redact(text: str) -> tuple[str, int]:
    """Return (text, hits). With hits, keep the prefix before the first hit and mark the rest."""
    first = len(text)
    hits = 0
    for pat in PATTERNS:
        for m in pat.finditer(text):
            hits += 1
            first = min(first, m.start())
    if not hits:
        return text, 0
    return f"{text[: min(first, QUOTE_MAX)]}[REDACTED {hits}]", hits


SHA_KEYS = frozenset({"head", "heads", "old", "new", "commit", "oid", "verdict_acked"})
GIT_SHA = re.compile(r"[0-9a-f]{40}")


def redact_obj[T](obj: T, limit: int = QUOTE_MAX * 2, sha_ok: bool = False) -> T:
    """Redact every string in a JSON-like structure; cap string length.

    A value that is exactly a 40-hex git sha is kept when it sits under a head/commit field (`SHA_KEYS`): it is
    a public commit id there, and redacting it breaks the head comparison. Anywhere else it is still redacted."""
    if isinstance(obj, str):
        if sha_ok and GIT_SHA.fullmatch(obj):
            return obj
        text, _ = redact(obj)
        return cast(T, text[:limit])
    if isinstance(obj, dict):
        return cast(T, {k: redact_obj(v, limit, sha_ok or k in SHA_KEYS) for k, v in obj.items()})
    if isinstance(obj, list):
        return cast(T, [redact_obj(v, limit, sha_ok) for v in obj])
    return obj


def count_hits(text: str) -> int:
    """Number of secret-shaped strings in `text`."""
    return redact(text)[1]


def count_hits_obj(obj: object, sha_ok: bool = False) -> int:
    """count_hits over a parsed row, with the same head-field exemption as redact_obj."""
    if isinstance(obj, str):
        return 0 if sha_ok and GIT_SHA.fullmatch(obj) else count_hits(obj)
    if isinstance(obj, dict):
        return sum(count_hits_obj(v, sha_ok or k in SHA_KEYS) for k, v in obj.items())
    if isinstance(obj, list):
        return sum(count_hits_obj(v, sha_ok) for v in obj)
    return 0


def count_hits_text(text: str) -> int:
    """count_hits over JSON lines, row by row; a line that is not JSON is counted as text."""
    import json

    n = 0
    for line in text.splitlines():
        try:
            n += count_hits_obj(json.loads(line))
        except ValueError:
            n += count_hits(line)
    return n
