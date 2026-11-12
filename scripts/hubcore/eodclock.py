"""End of day on a clock: `report.eod_time` ("HH:MM" in the user's zone, empty = off).

After that time the tick writes the day's end-of-day review once (status.eod) unless the file already exists, and raises one
alert if that fails, or if `report.eod_check_min` minutes after the time there is still no eod file for the day."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from . import status, store

HHMM = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


def parse(cfg: dict) -> tuple[int, int] | None:
    """(hour, minute) of `report.eod_time`, or None when it is empty or not HH:MM."""
    m = HHMM.fullmatch(str(cfg["report"].get("eod_time", "")).strip())
    return (int(m[1]), int(m[2])) if m else None


def doctor_line(cfg: dict) -> str:
    raw = str(cfg["report"].get("eod_time", "")).strip()
    if not raw:
        return "eod clock: off (set [report] eod_time = \"HH:MM\" and the tick writes the end-of-day review itself)"
    if parse(cfg) is None:
        return f"eod clock: INVALID report.eod_time {raw!r}; use HH:MM in 24 hours"
    return f"eod clock: on at {raw} ({cfg['user'].get('timezone') or 'the system zone'}), alert if no eod file {int(cfg['report']['eod_check_min'])} min later"


def _state(sd: Path) -> dict:
    try:
        return json.loads((sd / "eodclock.json").read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}


def run(cfg: dict, now: float) -> list[dict]:
    """The tick's part: write today's eod once after the time; return the alert when it failed or the file is still missing."""
    at = parse(cfg)
    if at is None:
        return []
    sd = store.state(cfg)
    day = store.day_of(cfg, now)
    start = datetime.fromtimestamp(now, store.tzinfo(cfg)).replace(hour=at[0], minute=at[1], second=0, microsecond=0).timestamp()
    if now < start:
        return []
    st = _state(sd)
    if st.get("day") != day:
        st = {"day": day}
    path = sd / "eod" / f"eod_{day}.md"
    out: list[dict] = []
    if not path.exists() and not st.get("tried"):
        st["tried"] = now
        try:
            status.eod(cfg, day)
        except Exception as e:  # the alert says so; the tick goes on
            st["error"] = repr(e)[:200]
    if not path.exists() and not st.get("alerted") and (st.get("error") or now >= start + float(cfg["report"]["eod_check_min"]) * 60):
        st["alerted"] = True
        why = f"failed: {st['error']}" if st.get("error") else f"no eod file {int(float(cfg['report']['eod_check_min']))} min after {cfg['report']['eod_time']}"
        out.append({"key": f"eod_clock:{day}", "msg": f"end-of-day review for {day} {why}. NEXT: hubkit eod --day {day}"})
    store.write_json_atomic(sd / "eodclock.json", st)
    return out
