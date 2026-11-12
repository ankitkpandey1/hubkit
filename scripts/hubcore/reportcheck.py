"""Checks on the hub's final message that need only the text: a time stated in UTC when the user has another zone."""

from __future__ import annotations

import re

_UTC_ZONES = frozenset({"", "utc", "etc/utc", "gmt", "etc/gmt", "z", "zulu"})
_FENCE = re.compile(r"```.*?```|~~~.*?~~~", re.S)
_INLINE = re.compile(r"`[^`\n]*`")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?\s?(?:Z\b|[+-]00:?00\b)")
_OFFSET = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?\s?[+-]00:?00\b")


def prose_only(text: str) -> str:
    """`text` without fenced blocks and inline code spans."""
    return _INLINE.sub(" ", _FENCE.sub(" ", text))


def utc_time(cfg: dict, last: str) -> str | None:
    """A Stop reason when the message states a time marked UTC/GMT/Z while `user.timezone` is another zone, else None."""
    opts = cfg.get("report") or {}
    zone = str((cfg.get("user") or {}).get("timezone") or "").strip()
    if not opts.get("zone_check", True) or zone.lower() in _UTC_ZONES:
        return None
    marks = "|".join(re.escape(str(m)) for m in opts.get("utc_markers") or [])
    pats = [_ISO, _OFFSET]
    if marks:
        pats.append(re.compile(rf"\d{{1,2}}:\d{{2}}(?::\d{{2}})?\s?(?:{marks})\b"))
    text = prose_only(last)
    for pat in pats:
        if m := pat.search(text):
            return (f"hubkit UTC-TIME: your message states a time in UTC ('{m[0].strip()}') but the user's zone is {zone}. "
                    f"NEXT: give the time in {zone}")
    return None


_CODE_BLOCK = re.compile(r"(?:```|~~~)[^\n]*\n(.*?)(?:```|~~~)", re.S)
FORM_NEXT = "NEXT: put the detail in a file and give its path; lead with the result"


def form(cfg: dict, last: str) -> list[tuple[str, str]]:
    """(check id, reason) pairs when the message is longer than `report.max_lines` non-blank lines or holds a fenced code
    block longer than `report.max_code_lines`. A one-line message is exempt. Both are 0 (off) by default."""
    opts = cfg.get("report") or {}
    max_lines, max_code = int(opts.get("max_lines", 0) or 0), int(opts.get("max_code_lines", 0) or 0)
    lines = [x for x in last.splitlines() if x.strip()]
    if len(lines) <= 1:
        return []
    out = []
    if max_lines and len(lines) > max_lines:
        out.append(("report-length", f"hubkit REPORT-FORM: your message is {len(lines)} lines, more than {max_lines}. {FORM_NEXT}"))
    longest = max((sum(1 for x in m[1].splitlines() if x.strip()) for m in _CODE_BLOCK.finditer(last)), default=0)
    if max_code and longest > max_code:
        out.append(("report-code", f"hubkit REPORT-FORM: your message holds a {longest}-line code block, more than {max_code}. {FORM_NEXT}"))
    return out
