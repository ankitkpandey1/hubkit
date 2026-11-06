# CLAUDE.md

Guidance for an agent changing hubkit. Read `README.md` for what it does and `SKILL.md` for the behaviour contract the hub follows.

## Layout

```
SKILL.md                  behaviour contract for the hub session (keep under ~200 lines)
README.md                 operator view          docs/  architecture, configuration, commands, troubleshooting
LICENSE (Apache-2.0)      pyproject.toml  ruff, mypy, pytest settings    .github/workflows/ci.yml
config.example.toml       generic config template   intents.example.toml   generic intents
references/               design notes
scripts/hubkit            entry point (hooks never raise; an import failure is logged and exits 0)
scripts/learn.py          learnings log CLI
scripts/hubcore/
  cli.py                  argparse verbs, doctor
  config.py               defaults + config loading (DEFAULTS is the full key list)
  hooks.py                the hook events, bind/unbind, is_hub
  orders.py               order ledger, intake, evidence-checked close
  routed.py               routed-work items: record/chase/ladder, gh reads, heard-from, close
  gate.py                 Stop-gate reasons
  slow.py                 job records, estimates, usual-time baseline, SLOW check, estimate vs actual
  watcher.py              hub/peer/watch/cron checks, desktop notify, false-alarm excuses, blocked-on link
  status.py               work items, tick/watch, status block, EOD
  store.py                state dir, locked/atomic writes, row IO
  redact.py               secret redaction (every write passes through it)
  learnings.py            learnings log
tests/conftest.py         `env` fixture: scratch state, fake sessions, `env.run`/`env.hook`
tests/test_hubkit.py      all tests
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
