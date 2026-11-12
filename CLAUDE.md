# CLAUDE.md

Guidance for an agent changing hubkit. Read `README.md` for what it does and `SKILL.md` for the behaviour contract the hub follows.

## Layout

```
SKILL.md                  behaviour contract for the hub session (keep under ~200 lines)
README.md                 operator view          docs/  architecture, configuration, commands, troubleshooting
SECURITY.md               what redaction catches and does not    CHANGELOG.md  release notes (## Unreleased on top)
LICENSE (Apache-2.0)      pyproject.toml  ruff, mypy, pytest settings
config.example.toml       generic config template   intents.example.toml   generic intents
references/               design notes
.github/workflows/ci.yml  lean CI: ruff, mypy, pytest
scripts/hubkit            entry point (hooks never raise; an import failure is logged and exits 0)
scripts/learn.py          learnings log CLI
scripts/hubcore/
  cli.py                  argparse verbs, doctor
  agentio.py              --json views, exit codes (0/2/3/4), NEXT on every error, `hubkit next`, help examples
  config.py               defaults + config loading (DEFAULTS is the full key list)
  compat.py               the one place that knows the operating system (processes, locks, scheduler)
  hooks.py                the hook events, bind/unbind, is_hub
  install.py              `skills --install` and the `hooks` JSON block
  orders.py               order ledger, intake, evidence-checked close
  routed.py               routed-work items: record/chase/ladder, gh reads, heard-from, close
  preroute.py             check at routing time that the work is not already done
  roster.py               peer roster: role, free/busy, past jobs per peer
  inbound.py              asks from other people tracked as reply items
  replies.py              reply loop: is the asked PR done, was the person told
  mergestate.py           Stop-gate check: "ready to merge" must be mergeable on GitHub
  owed.py                 tracked requests (the user's plain asks) and the `mine` view
  standrules.py           standing rules (`hubkit standing`): fired once per PR head or message, opens an order or work item
  reviewdebt.py           the user's own re-reviews (rate-limited gh sweep, read only)
  stale.py                open items whose PR is merged or closed are marked probably stale
  nopr.py                 routed item with no PR whose owner reported done: check it yourself
  threads.py              threads: the `thread` field, linking rules, the `mine` fold, settle/root-closed lines, `Done-when:` checks
  eodclock.py             `report.eod_time`: the tick writes the day's eod once, alerts on failure
  sweeps.py               the one seam where the tick, status block, prompt, mine and digest call the five above
  playbooks.py            how a recurring command is done, saved once and replayed
  lifecycle.py            the one table of states, transitions, required proof and clocks for orders, routed, requests, replies
  gate.py                 Stop-gate reasons as (check id, text); urgent-ask
  gatectl.py              check modes (hold/warn/off), false alarms, self-downgrade and restore, warning queue
  standing.py             stated standing rules in a typed prompt
  lessons.py              checks born of repeated corrections; warn-only tool_warning
  guardwatch.py           warn-only guards: unasked tickets and recurring work, hub inline edits, wrong account, yes before the draft, one agent per host, unasked review
  reviewreport.py         review report completeness (`roles.<role>.report_must_include`)
  reportcheck.py          final-message checks: a time stated in UTC for a user in another zone
  slow.py                 job records, estimates, usual-time baseline, SLOW check, estimate vs actual
  watcher.py              hub/peer/watch/cron checks, desktop notify, false-alarm excuses, blocked-on link
  status.py               work items, tick/watch, status block, EOD
  ledger.py               the day's whole record: the user's PRs, peer prompts; digest and find
  trust.py                per-class trust from accepted/edited/rejected proposals
  store.py                state dir, locked/atomic writes, row IO
  redact.py               secret redaction in place (every write passes through it)
  learnings.py            learnings log, promote (overlay cap), address, review, regressed
tests/conftest.py         `env` fixture: scratch state, fake sessions, `env.run`/`env.hook`
tests/test_*.py           one file per area (hubkit = core/orders/routing, plus lessons, guards, tool_guards, slow, watcher, ...)
tests/run_reverts.py      revert proof
```

## Run

```
python3 -m pytest tests -q
ruff check . && mypy scripts/hubcore scripts/learn.py scripts/hubkit
python3 tests/run_reverts.py          # slow (minutes), prints one line per entry, exits 0 only if all are OK
python3 scripts/hubkit doctor --check-clean
```

Use the scratch state the `env` fixture gives. For a manual run set `HUBKIT_STATE` and `HUBKIT_CONFIG` to a scratch directory; never point tests or experiments at the live state dir or the live bind.

## Rules

- Every behaviour gets a test and a `run_reverts.py` entry: `(name, file, old, new, test)`, where `old` occurs exactly once in `file`, `new` breaks the behaviour, and `test` fails with it. An entry whose pattern is gone or whose test is not collected is reported BROKEN; fix or delete it. Do not edit `scripts/` while a revert run is in progress (it copies them per entry).
- A test must be able to fail. Only GitHub (`gh`) may be a local stand-in, and only because tests cannot reach it; say so in the test. Use real 40-hex shas for heads.
- Hooks always exit 0 and print nothing outside the bound hub. Fail loud inside the logic, not silently in the hook.
- Config keys have defaults in `config.py` `DEFAULTS`; add new keys there and in `config.example.toml`.
- Python 3.14; `except A, B:` without parentheses is the required form. Type annotations on functions; stdlib only.
- Keep `SKILL.md`, `README.md` and `docs/` true: when a behaviour or verb changes, change them in the same edit, and check every command and path you write against the code.

## No personal data in this directory

No person's name, host, company or product term in scripts, tests, skill text or docs. `hubkit doctor --check-clean` scans these files for the terms in `[private] terms` of your config and must report none. Personal rules, names, terms and evidence live in `~/.config/hubkit/` (`config.toml`, `intents.toml`, `overlay.md`, `research/`), never here. Tests use neutral names such as `peer-a`.
