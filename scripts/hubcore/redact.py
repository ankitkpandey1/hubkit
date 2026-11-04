"""Secret redaction. Every write to state goes through redact_obj (see store.append_row)."""

from __future__ import annotations

import re

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


def redact_obj(obj, limit: int = QUOTE_MAX * 2):
    """Redact every string in a JSON-like structure; cap string length."""
    if isinstance(obj, str):
        text, _ = redact(obj)
        return text[:limit]
    if isinstance(obj, dict):
        return {k: redact_obj(v, limit) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v, limit) for v in obj]
    return obj


def count_hits(text: str) -> int:
    return redact(text)[1]
