"""Secret redaction. Every write to state goes through redact_obj (see store.append_row).

A secret is replaced in place by `[REDACTED]`; the text around it is kept (see `redact` for the exact rules)."""

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
        r"(?i)[\w.-]*"
        r"(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|auth|credentials?|private[_-]?key|access[_-]?key)"
        r"(?:[_.-][A-Za-z0-9]+)*[\"']?\s*[=:]\s*[\"']?\S+"
    ),
    re.compile(r"(?i)(?<![A-Za-z0-9])authorization\s*[=:]\s*(?:basic\s+)?\S+"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"glpat-[A-Za-z0-9_-]{15,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
]
QUOTE_MAX = 300
LONG_RUN = re.compile(r"(?<![A-Za-z0-9+])(?=[A-Za-z0-9+]*\d)(?=[A-Za-z0-9+]*[A-Za-z])[A-Za-z0-9+]{32,}")
COMMIT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{32})")
MARK = "[REDACTED]"


def _spans(text: str) -> list[tuple[int, int]]:
    """Every secret-shaped span of `text`, overlapping or touching spans merged."""
    found = [m.span() for pat in PATTERNS for m in pat.finditer(text)]
    # A long run is a secret unless it is pure lowercase hex of exactly 40 (git commit id) or 32 (md5 / uuid
    # without dashes) characters: those are ids people quote in prose. Mixed-case, base64-like or longer runs stay masked.
    found += [m.span() for m in LONG_RUN.finditer(text) if not COMMIT_ID.fullmatch(m.group())]
    merged: list[tuple[int, int]] = []
    for lo, hi in sorted(found):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else:
            merged.append((lo, hi))
    return merged


def redact(text: str) -> tuple[str, int]:
    """Return (text, hits): each secret-shaped span is replaced in place by `[REDACTED]`, the text around it is kept.

    A key=value secret is matched by whole key name or key segment (`token`, `api_token`, `AUTH_TOKEN`, `x-auth-token`),
    never by a longer word that merely contains one (`author`, `tokenizer`). A long alphanumeric run (32+ chars with a
    digit and a letter) is masked unless it is pure lowercase hex of exactly 40 characters (a git commit id) or 32
    (a dashless uuid / md5): those ids are kept so quotes and notes stay readable."""
    spans = _spans(text)
    if not spans:
        return text, 0
    out: list[str] = []
    pos = 0
    for lo, hi in spans:
        out += [text[pos:lo], MARK]
        pos = hi
    out.append(text[pos:])
    return "".join(out), len(spans)


def redact_obj[T](obj: T, limit: int = QUOTE_MAX * 2) -> T:
    """Redact every string in a JSON-like structure (in place, see `redact`); cap string length."""
    if isinstance(obj, str):
        return cast(T, redact(obj)[0][:limit])
    if isinstance(obj, dict):
        return cast(T, {k: redact_obj(v, limit) for k, v in obj.items()})
    if isinstance(obj, list):
        return cast(T, [redact_obj(v, limit) for v in obj])
    return obj


def count_hits(text: str) -> int:
    """Number of secret-shaped strings in `text`."""
    return redact(text)[1]


def count_hits_obj(obj: object) -> int:
    """count_hits over a parsed row."""
    if isinstance(obj, str):
        return count_hits(obj)
    if isinstance(obj, dict):
        return sum(count_hits_obj(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(count_hits_obj(v) for v in obj)
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
