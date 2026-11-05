# hubkit

A Claude Code skill and a stdlib-only Python tool for one coordinating "hub" session. The hub takes the user's orders and routes the work to peers and background agents. hubkit keeps the record the hub cannot keep in its head:

- **Order clock.** Orders opened from the user's own prompts (and card answers), each with a clock, a definition of done and an evidence check on close. Peer and harness text never opens an order.
- **Routed-work tracking.** A hook on `SendMessage` and `Agent` records what the hub asked of whom, one item per (owner, PR). It tracks last heard-from, moved heads, reviews landed at the PR head, and chases. Reviews run on a short clock, with a hard overdue limit.
- **Watchdog and wake-up.** `hubkit tick` (cron) and `hubkit watch` (a background process the hub starts) raise alerts with back-off. A watch exit wakes an idle hub.
- **Stop gate.** The Stop hook blocks the turn end while the hub owes something: no watch running, a stalled item, a verdict not backed by a GitHub review at the PR head, or a question to the user with nothing blocked on him. Every block names its next action.
- **Live status, end-of-day review, learnings.** A status block at session start and on each prompt, a daily archive, `hubkit eod`, and `scripts/learn.py` for a learning loop with an overlay of rules.

`SKILL.md` is the behaviour contract the hub reads. This file is the operator view.

## Install

1. Put the directory at `~/.claude/skills/hub/`. Python 3.14 (the code uses `except A, B:` without parentheses). `gh` on `PATH` is needed for PR checks; without it items show "PR unknown".
2. Register the hooks in `~/.claude/settings.json`. Each runs `python3 ~/.claude/skills/hub/scripts/hubkit hook <event>`: `session` (SessionStart), `prompt` (UserPromptSubmit), `answer` (PostToolUse, matcher `AskUserQuestion`), `routed` (PostToolUse, matcher `SendMessage|Agent`), `stop` (Stop), `record` (schema canary, see `scripts/hubcore/hooks.py`). SKILL.md section 12 has the exact command line. Hooks do nothing outside the bound hub session and always exit 0.
3. In the session that is to be the hub: `python3 ~/.claude/skills/hub/scripts/hubkit bind` (`--pid N` to name the process). `hubkit unbind` frees it. Only an interactive session can be bound.
4. Check with `hubkit doctor` (state dir, bind, hook heartbeats, hook errors, secret-shaped strings in state). `hubkit doctor --check-clean` also scans the skill files for the private terms listed in your config.

## Configuration

User data lives outside this directory:

| File | Purpose |
|---|---|
| `~/.config/hubkit/config.toml` | Settings. Template: `config.example.toml`. Override the path with `$HUBKIT_CONFIG`. Without a file, generic defaults apply. |
| `~/.config/hubkit/intents.toml` | Words that open an order and what "done" needs. Template: `intents.example.toml`. |
| `~/.config/hubkit/overlay.md` | Personal rules the hub reads with SKILL.md. |
| `~/.local/state/hubkit/` | State: orders, work items, alerts, archive, status. Override with `$HUBKIT_STATE`. |

The `[routed]` section sets the stall and review clocks, `repo` (so a bare `#N` resolves for `gh`), `reviewer_logins`, `owner_logins`, `skip_briefs` and `reply_window_min`.

## Verbs

```
hubkit status [--json]               the status block
hubkit order ls|status|open|confirm|move|hold|resume|cancel|close|metrics [id]
hubkit work add|done|ls              --kind active|tracked|priority|routed|ask
hubkit tick [--if-bound]             one watchdog pass (cron)
hubkit watch [--interval S] [--max-cycles N]
hubkit eod [--day D] [--note ..]     end-of-day glance, reflection, archive
hubkit doctor [--check-clean]
hubkit bind [--pid N] | unbind
python3 scripts/learn.py add|list|promote-candidates|address|promote|review
```

Routed work: `hubkit work add --kind routed --owner X --title "..." --pr URL` records one by hand; `hubkit work done <id> --note ".."` closes it. A question for the user that really blocks work is registered with `hubkit work add --kind ask --title ".." --note ".."`; his card answer closes it.

Wake-up: the hub runs `python3 ~/.claude/skills/hub/scripts/hubkit watch --interval 120 --max-cycles 30` in the background. It exits 0 printing `ALERT ...` on a new alert or a backed-off repeat, and 3 after its cycles (re-arm it). Only one watch runs at a time; a second exits 4. The Stop gate forces the hub to start it.

## Tests

```
python3 -m pytest tests -q          # all tests, scratch state dirs only
python3 tests/run_reverts.py        # each guard reverted in a copy of the scripts must fail its test
```

`run_reverts.py` exits 0 only when every reverted copy fails its test and every listed pattern and test exists.

## Known limitations

- Order creation takes a blocking `flock` (`scripts/hubcore/orders.py`); a process that holds the lock indefinitely stalls order opens.
- A hook payload with no `origin` field is treated as human input.
- Redaction matches secret shapes and `key=value` forms; natural-language secrets are not caught. A 40-hex git sha is kept only under head fields.
- Whether a message to a peer is an ask, a reply or a follow-up is decided by rules in `scripts/hubcore/routed.py`; an unusual message can be misjudged.
