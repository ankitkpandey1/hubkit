# Troubleshooting

Start with `hubkit doctor`: it prints the config path, state path, bind, hook heartbeats (`seen/<hook>`), ledger health, hook errors and a count of secret-shaped strings in state.

| Symptom | Cause and fix |
|---|---|
| Hooks print nothing | Expected outside the bound hub. Run `hubkit bind` in the session meant to be the hub; `doctor` must show `bind: live <name>`. Headless runs and non-interactive sessions are never the hub. `HUBKIT_DISABLE` also silences hooks. |
| `seen/session: never` (or any hook) | The hook is not registered in `~/.claude/settings.json`, or Claude Code was not restarted after editing it. Check the command line against the README. |
| `seen/cron: never` or old | The cron line is missing or fails. `tick` touches `seen/cron` on every run, even the early no-op, so this tells a dead cron from an idle one. `doctor` prints `cron: STALE` when a crontab line runs hubkit but `seen/cron` is older than `watchdog.cron_stale_min`. The usual cause is cron running an older `python3` than 3.14 (hubkit fails with a `SyntaxError` that goes to `/dev/null`): run `env -i PATH="$(grep ^PATH= /etc/environment | cut -d'"' -f2)" sh -c 'python3 --version'` and put the full interpreter path in the crontab line. Add `DISPLAY` and `DBUS_SESSION_BUS_ADDRESS` to the crontab for desktop notifications. |
| `error: state dir ... is inside a git repository` | State must not be committed. Move it with `$HUBKIT_STATE` or `paths.state_dir`, or run from a home directory that is not a git repository. |
| `error: no Claude session found for this process (pass --pid)` | `bind` walks up the process tree to a Claude Code process listed in `paths.sessions_dir`. Run it from the hub session, or pass `--pid`. |
| `error: live hub already bound` | Another hub is bound. `hubkit unbind` from that session, or wait for it to exit (a dead process frees the bind). |
| `mep: UNVERIFIED (mep CLI not found ...)` | The optional `mep` CLI is missing. Set `[mep] enabled = false` in `config.toml`. |
| `PR unknown` on routed items | `gh` is missing, not authenticated, or the PR is in a repository the token cannot read. Check `gh auth status`; set `routed.repo` so bare `#N` resolves. |
| A prompt did not open an order | The sentence was a question, held a `not_order_phrases` phrase, had no `imperative` word (so it is `unconfirmed`: `hubkit order confirm ID`), or came from peer or harness text, which never opens orders. |
| `close refused: missing evidence` | Give every name in the kind's `done_when`: `--evidence name=value`. `file:<path>` must exist and be newer than the order. |
| The turn will not end (Stop gate) | Read the `NEXT:` in the block reason. Usually: start the watch (`hubkit watch --interval 120 --max-cycles 30` in the background), act on a stalled item, drop an unbacked verdict, or register the question with `hubkit work add --kind ask`. |
| `import-errors.log` in the state dir | hubkit failed to import (wrong Python version, damaged checkout). Hooks exit 0 so Claude Code is unaffected. Run `python3 --version` (3.14 or newer needed) and `hubkit doctor`. |
| `SyntaxError` on `except A, B:` | Python older than 3.14. |
| `doctor --check-clean` lists files | The files contain a word from `private.terms`. Edit them; the match is not printed. |
| `learn.py address` refused | No rule line tagged `[class]` exists in the overlay or SKILL.md, or neither changed since the class was last seen. Run `learn.py promote` first. |
