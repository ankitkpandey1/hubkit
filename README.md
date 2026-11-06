# hubkit

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-blue.svg)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)

**hubkit is a session supervisor for Claude Code: a hooks-based kit for multi-agent orchestration that keeps the one session coordinating your other sessions and agents honest. It gives each order a clock, tracks work routed to peers, blocks a premature "done", and learns from your corrections.**

hubkit adapts to you. When you correct the hub ("I told you to wait for the review"), a hook notices, the hub records the correction as a class of mistake, and a class that repeats is promoted to a written rule in your own overlay file. A rule only counts as fixed after a check that the rule really exists, and it stays on probation until it has held. The words that open an urgent order, the clocks, what "done" means and every other setting live in your config files, not in the code, so the kit bends to how you work. This is a log-and-verify loop around the hub model, not machine learning: hubkit detects, stores and checks; the hub session writes the entries and rules.

## Why use a Claude Code session supervisor

A coordinating Claude Code session routes work to peers and background agents, then forgets. After a context compaction it no longer knows which order was urgent, who was asked to review what, or whether "done" was ever proven. hubkit keeps that record outside the model's memory, gives every order a clock and an evidence check, watches routed work for silence, and blocks the hub from ending a turn while it owes something. Example session:

```console
$ hubkit order open --kind hotfix --quote "hotfix the billing bug now"
O-261105-bd22 hotfix [open] open 0m, idle 0m, chases 0; done needs: pr_merged,prod_live
$ hubkit order move O-261105-bd22 --note "PR merged"
$ hubkit order close O-261105-bd22 --evidence pr_merged=https://github.com/acme/repo/pull/12
error: close refused: missing evidence 'prod_live'
$ hubkit order close O-261105-bd22 --evidence pr_merged=https://github.com/acme/repo/pull/12 \
      --evidence prod_live=https://example.com/deploy-log
O-261105-bd22 closed
```

In a live hub, the prompt hook opens that order itself from your sentence, and the hub only reports "done" after the close succeeds.

## What hubkit does for multi-agent coordination

- **Learns from corrections.** A prompt hook flags correction phrases and repeated instructions. `scripts/learn.py` records, promotes and verifies them. See [docs/architecture.md](docs/architecture.md#the-learn-loop).
- **Order clock.** Orders open from your own prompts (never from peer or harness text), each with a first-action limit, a stall limit and a definition of done checked by evidence on close.
- **Routed-work tracking.** A hook on `SendMessage` and `Agent` records what the hub asked of whom, one item per (owner, PR). It follows last heard-from, moved heads and GitHub reviews at the PR head.
- **Watchdog and wake-up.** `hubkit tick` (cron) and `hubkit watch` (a background process) raise alerts with back-off; a watch exit wakes an idle hub.
- **Stop gate.** The Stop hook refuses to let the turn end while the hub owes something (no watch running, a stalled item, an unbacked verdict, a question to you that blocks nothing, a "tests pass" claim with no command run, a PR number with no link, an approval on red CI, built work not pushed).
- **Tool guards.** An optional warn-only PreToolUse hook (`hubkit hooks --merge-guard`) warns about an unordered merge, a post without your typed yes, a banned command, stopping an agent and an unmarked decision card. It never denies.
- **Status, digest, end-of-day review.** A status block at session start and on each prompt, a daily archive, `hubkit digest` for when you come back (it also lists the PRs your account opened and what each peer session was asked), `hubkit find WORD` to search orders, work, asks, peer prompts and your PRs, `hubkit eod`, a per-class `hubkit trust` ledger, and `hubkit inbound ingest` to track asks from other people so none goes unanswered.

`SKILL.md` is the behaviour contract the hub session reads. This file is the operator view.

## Requirements

- Linux. hubkit has only been run on Linux so far (see [Platform support](#platform-support)).
- Python 3.14 or newer (the code uses `except A, B:` without parentheses, PEP 758). Standard library only.
- [Claude Code](https://code.claude.com), which provides the hooks and the skills directory.
- Optional: `gh` (GitHub CLI) on `PATH` for PR checks; without it items show "PR unknown". Optional: [`mep`](https://pypi.org/project/miseenplace-cli/) for a personal task list in the status block; without it the block shows `mep: UNVERIFIED (mep CLI not found ...)`, and `[mep] enabled = false` silences that.

## Paste this into your agent

```text
Install hubkit with `uv tool install hubkit-cli`, run `hubkit skills --install DIR` with the skills directory
for your tool, then run the hubkit-setup skill and follow it. Ask me only for things you cannot find out yourself.
```

hubkit runs on Claude Code hooks, so the skills directory is Claude Code's: `~/.claude/skills` (user level) or `.claude/skills` (project level). `hubkit-cli` is on [PyPI](https://pypi.org/project/hubkit-cli/). To install the latest commit instead: `uv tool install git+https://github.com/ankitkpandey1/hubkit`.

`hubkit skills --install DIR` copies three skills into `DIR`: `hub` (the rules and the scripts the hooks run), `hub-mep` (optional) and `hubkit-setup` (finds your facts, writes the config, registers the hooks, binds, proves it with `hubkit doctor`). Run it again after an upgrade to refresh the copies. `hubkit hooks --skills-dir DIR` prints the hooks block for `~/.claude/settings.json`.

### Install by hand

```bash
git clone https://github.com/ankitkpandey1/hubkit ~/.claude/skills/hub
python3 --version                             # 3.14 or newer
python3 ~/.claude/skills/hub/scripts/hubkit doctor
```

The directory name must be `hub`: Claude Code finds the skill there, and the hooks and the stop gate use that path. `doctor` prints the config and state paths, the bind and the hook heartbeats; on a fresh install it shows `config: ... (defaults)` and `bind: none or dead`, which is correct.

## Claude Code hook setup

Add the hooks to `~/.claude/settings.json` (merge with any existing `hooks` block). Each command is a shim that prints a visible line if the script is missing, then runs one hook event:

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "[ -f $HOME/.claude/skills/hub/scripts/hubkit ] || echo 'hubkit: script missing'; python3 $HOME/.claude/skills/hub/scripts/hubkit hook session"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 $HOME/.claude/skills/hub/scripts/hubkit hook prompt"}]}],
    "PostToolUse": [
      {"matcher": "AskUserQuestion", "hooks": [{"type": "command", "command": "python3 $HOME/.claude/skills/hub/scripts/hubkit hook answer"}]},
      {"matcher": "SendMessage|Agent", "hooks": [{"type": "command", "timeout": 6, "command": "python3 $HOME/.claude/skills/hub/scripts/hubkit hook routed"}]}
    ],
    "Stop": [{"hooks": [{"type": "command", "command": "python3 $HOME/.claude/skills/hub/scripts/hubkit hook stop"}]}]
  }
}
```

Hooks do nothing, print nothing and exit 0 in every session that is not the bound hub. Then, in the session that is to be the hub (interactive sessions only):

```bash
python3 ~/.claude/skills/hub/scripts/hubkit bind      # --pid N to name the process
python3 ~/.claude/skills/hub/scripts/hubkit doctor    # now shows "bind: live <name>"
```

`hubkit unbind` frees it. For the `hubkit` command used below, add an alias such as `alias hubkit='python3 ~/.claude/skills/hub/scripts/hubkit'`.

## Quick start

```bash
hubkit order open --kind hotfix --quote "hotfix the billing bug now"   # or let the prompt hook do it
hubkit order ls
hubkit work add --kind active --title "triage the flaky test"
hubkit status                                                          # the status block
hubkit find billing --since 2d                                         # search orders, asks, peer prompts, your PRs
python3 ~/.claude/skills/hub/scripts/learn.py add --class ask-before-merge --gate G5 \
    --quote "I told you to wait for the review" --situation "..." --wrong "..." --right "..." --evidence "..."
```

Optional cron line for the watchdog (a no-op unless a hub is bound or an order is open). hubkit needs Python 3.14; cron's `PATH` can put an older `python3` first (for example `/usr/local/bin`), which fails on the first import and, with output sent to `/dev/null`, leaves no trace except a stale `seen/cron`. Name the interpreter:

```
*/5 * * * * /usr/bin/python3 $HOME/.claude/skills/hub/scripts/hubkit tick --if-bound
```

## Configuration files

Everything personal lives outside the checkout, in `~/.config/hubkit/`:

| File | Purpose |
|---|---|
| `config.toml` | Settings. Template: [`config.example.toml`](config.example.toml). Override the path with `$HUBKIT_CONFIG`. No file means generic defaults. |
| `intents.toml` | Words that open an order, clocks, what "done" needs. Template: [`intents.example.toml`](intents.example.toml). |
| `overlay.md` | Your own rules (people, accounts, routing). The hub reads it with `SKILL.md`; promoted learnings are written here. |
| `ext/*.py` | Optional extensions: extra status lines and watchdog checks. |
| `~/.local/state/hubkit/` | State: orders, work items, alerts, archive, status. Override with `$HUBKIT_STATE`. Refused if inside a git repository. |

Every key is documented in [docs/configuration.md](docs/configuration.md).

## What hubkit enforces and what it only warns about

| Behaviour | Strength |
|---|---|
| The Stop hook holds the turn while the hub owes something: no watch running, a stalled item, an unbacked verdict, an unsolved blocker, an item over its estimate, a "done" while your newest prompt is unread, a claim of passing tests with no command run, an approval while CI is not green | Enforced (blocks the turn end) |
| An order closes only with the evidence its kind requires | Enforced (the close is refused) |
| A learned class counts as fixed only if a rule for it exists (and, with `--check`, its command passes) | Enforced (the address is refused) |
| A merge order closes only with the merge commit sha | Enforced (the close is refused) |
| A second job sent to a busy owner while an order is open; a brief that bans the action a blocker needs; a brief with no done-when, output or estimate line; more than 4 agents holding work | Warning only |
| `gh pr merge` with no live order naming that PR, a post without your typed yes, a banned command, `TaskStop`, a repeated command, a whole-suite `pytest`, an unmarked decision card (`hubkit hook pretool`) | Warning only, never denies |
| `hubkit doctor` warning when your `intents.toml` lacks keys the example has, a hook registered twice, or no scheduled `hubkit eod` | Warning only |

Details and exact rules: [SKILL.md](SKILL.md) and [docs/architecture.md](docs/architecture.md#stop-gate-gatepy).

## Documentation

- [docs/architecture.md](docs/architecture.md): hooks, orders, routed work, the stop gate and the learn loop, with a diagram.
- [docs/configuration.md](docs/configuration.md): every `config.toml` and `intents.toml` key.
- [docs/commands.md](docs/commands.md): every `hubkit` and `learn.py` verb.
- [docs/troubleshooting.md](docs/troubleshooting.md): common problems and what to check.
- [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CHANGELOG.md](CHANGELOG.md).

## Tests

```bash
python3 -m pytest tests -q          # real subprocesses, scratch state dirs only
python3 tests/run_reverts.py        # each guard reverted in a copy must fail its test (minutes)
```

`run_reverts.py` exits 0 only when every reverted copy fails its test and every listed pattern and test exists. Install `pytest` first (`pip install pytest`).

## Platform support

hubkit has only been run on Linux so far, and the full test suite, ruff and mypy pass there. The code has paths for macOS and Windows, all in `scripts/hubcore/compat.py` (process lookup, file locks, scheduler check, UTF-8 stdio), but they have never been run on those systems. Treat them as unsupported until someone does.

## Limits and known gaps

- Python 3.14 only, and Linux only so far; see Platform support.
- It targets one hub per machine. A second live hub is refused at bind.
- Order creation takes a blocking `flock` (`scripts/hubcore/orders.py`); a process that holds the lock forever stalls order opens.
- A hook payload with no `origin` field is treated as human input.
- Redaction matches secret shapes and `key=value` forms. Natural-language secrets ("the password is hunter2") are not caught. See [SECURITY.md](SECURITY.md).
- Whether a message to a peer is an ask, a reply or a follow-up is decided by rules in `scripts/hubcore/routed.py`; an unusual message can be misjudged.
- The learn loop needs the hub model to act on the `CORRECTION` line; hubkit cannot make it record or write a rule, only refuse to mark a class fixed without one.
- `skills/hub-mep` describes an optional integration with the `mep` task-list CLI and is of no use without it.
- Hooks are written against the Claude Code hook payloads of late 2026; `payload-samples/` in the state dir records the real payload keys so a schema change is visible.

## License

Apache License 2.0. See [LICENSE](LICENSE). Copyright 2026 the hubkit contributors.
