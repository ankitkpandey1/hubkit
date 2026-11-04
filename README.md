# hubkit

A Claude Code skill for a coordinating "hub" session: order clock with evidence-checked close, watchdog, live work status, end-of-day review and a learning loop. See `SKILL.md` for the behaviour and `config.example.toml` / `intents.example.toml` for configuration. User data lives outside this repo (`~/.config/hubkit`, `~/.local/state/hubkit`).

Tests: `python3 -m pytest tests -q`; guard check: `python3 tests/run_reverts.py`.

## Known limitations

- Order creation takes a blocking `flock` (`scripts/hubcore/orders.py`); a process that holds the lock indefinitely stalls order opens.
- A hook payload with no `origin` field is treated as human input.
- Redaction matches secret shapes and `key=value` forms; natural-language secrets are not caught.
