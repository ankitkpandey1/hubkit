#!/usr/bin/env python3
"""Hub learnings log: add, list, promote-candidates, address, review. Stdlib only.

Entry format: see hubcore/learnings.py. The file is state (default: <state dir>/learnings.md),
edited under a lock with an atomic replace, and every field is redacted before it is written.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hubcore import config, learnings  # noqa: E402


def main() -> int:
    """Run one learn.py verb. Returns the exit code."""
    p = argparse.ArgumentParser(description="Hub learnings log")
    p.add_argument("--file", help="learnings file (default: from config)")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="append an entry or a repeat of a class")
    a.add_argument("--class", dest="cls", required=True)
    a.add_argument("--gate", default="?")
    a.add_argument("--quote", required=True, help="the user's own words")
    a.add_argument("--date", default=date.today().isoformat())
    for f in ("situation", "wrong", "right", "evidence"):
        a.add_argument(f"--{f}", default="")
    ls = sub.add_parser("list")
    ls.add_argument("--last", type=int, default=30)
    pc = sub.add_parser("promote-candidates")
    pc.add_argument("--min", type=int, default=2)
    ad = sub.add_parser("address", help="mark a class as fixed (rule promoted, hook added)")
    ad.add_argument("--class", dest="cls", required=True)
    ad.add_argument("--how", required=True)
    ad.add_argument("--date", default=date.today().isoformat())
    ad.add_argument("--check", default="", help="a command that exits 0 while the rule holds; every earlier check must still pass")
    pm = sub.add_parser("promote", help="write a short rule into the overlay under its gate")
    pm.add_argument("cls")
    pm.add_argument("--gate", required=True)
    pm.add_argument("--rule", required=True)
    rv = sub.add_parser("review", help="new, repeated and addressed status for a day")
    rv.add_argument("--day", default=date.today().isoformat())
    args = p.parse_args()

    cfg = config.load()
    path = Path(args.file) if args.file else learnings.default_file(cfg)
    try:
        if args.cmd == "add":
            print(learnings.add(path, args.cls, args.quote, args.date, args.gate,
                                situation=args.situation, wrong=args.wrong,
                                right=args.right, evidence=args.evidence,
                                basis=learnings.rules_hash(cfg)))
        elif args.cmd == "list":
            for b in learnings.recent(path, args.last):
                print("\n".join(b).rstrip() + "\n")
        elif args.cmd == "promote-candidates":
            _, blocks = learnings.read_blocks(path)
            hits = sorted(((len(learnings.seen_lines(b)), b) for b in blocks
                           if len(learnings.seen_lines(b)) >= args.min), key=lambda r: -r[0])
            if not hits:
                print(f"none seen >= {args.min}x")
            for n, b in hits:
                m = learnings.HEADER_RE.match(b[0])
                print(f"{n}x  {m['cls']}  (gate {m['gate']})" if m else f"{n}x ?")
        elif args.cmd == "address":
            ov = [f for f in (learnings.overlay_path(cfg), config.SKILL_DIR / "SKILL.md") if f.exists()]
            print(learnings.mark_addressed(path, args.cls, args.date, args.how, learnings.rules_hash(cfg),
                                           max(f.stat().st_mtime for f in ov),
                                           "\n".join(f.read_text(encoding="utf-8", errors="replace") for f in ov), args.check))
        elif args.cmd == "promote":
            print(learnings.promote(path, learnings.overlay_path(cfg), args.cls, args.gate, args.rule))
        elif args.cmd == "review":
            for k, v in learnings.review(path, args.day).items():
                print(f"{k}: {', '.join(v) or '-'}")
    except (ValueError, KeyError) as e:
        print(f"error: {e.args[0]}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
