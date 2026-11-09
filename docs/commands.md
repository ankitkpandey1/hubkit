# Command reference

`hubkit` below means `python3 ~/.claude/skills/hub/scripts/hubkit`. Exit codes: 0 success, 2 a refused or invalid request (message on stderr), 1 `doctor` found a problem. `hook` always exits 0.

## hubkit

| Verb | What it does |
|---|---|
| `skills --install DIR` | Copies the hub, hub-mep and hubkit-setup skills into DIR (re-run refreshes; the running copy is left alone). |
| `hooks [--skills-dir DIR] [--merge-guard]` | Prints the `hooks` JSON for settings.json. `--merge-guard` adds the warn-only PreToolUse guard (matcher `Bash|TaskStop|AskUserQuestion`, see [architecture](architecture.md#hooks-scriptshubcorehookspy)). Never edits settings.json. |
| `hubkit bind [--pid N]` | Bind this session (found by walking up the process tree, or the process `N`) as the hub. Interactive sessions only; refuses while another hub is live. |
| `hubkit unbind` | Remove the bind. |
| `hubkit status [--json]` | Rebuild `status.json` and print the status block (or the JSON). |
| `hubkit order ls` | One line per live order. |
| `hubkit order status [ID]` | The status line of one order, or all live orders. |
| `hubkit order open --kind K --quote "..." [--unconfirmed]` | Open an order by hand. |
| `hubkit order confirm ID` | Turn an unconfirmed order into an open one. |
| `hubkit order move ID --note "..." [--estimate N]` | Record real movement. The first move also records the order's estimate (N minutes, else the usual time for its kind; printed when defaulted). |
| `hubkit digest [--since 12h]` | What finished, which orders closed, the latest alerts, what waits on the user, the PRs his account opened and what each peer session was asked, for a user coming back. |
| `hubkit inbound ingest [--file F]` | Turns the inbox watcher's lines into `reply` work items (one per asker, no duplicates). |
| `hubkit trust record --class C --outcome O` / `trust ls` | Per-class record of approved and corrected outcomes; a class with repeated corrections is shown as ask-first. |
| `hubkit how ls` / `show NAME` / `match TEXT` / `save NAME --trigger X --steps-file F --quote Q [--replace]` | Playbooks in `<config dir>/playbooks/`: a prompt that matches a trigger gets the steps injected; an order or imperative prompt with none is told to save one, and the Stop hook holds the turn end until it is. `--replace` keeps the old file as `.bak-<date>`. |
| `hubkit work verify ID` | Stamp the item as checked against reality just now. An `active` or `priority` item not verified for `watcher.verify_stale_min` raises an alert. It runs no check itself. |
| `hubkit find WORD [--since 2d]` | Searches orders, work, the archive, his asks, peer session prompts and the PRs his account opened (title, body, changed file paths). Needs `gh` for the PR part; a `gh` failure is printed as UNKNOWN, never skipped. Read only on peer sessions; prompts are redacted. |
| `hubkit asks ls [--since 12h]` | His recent prompts (from `recent-prompts.jsonl`), one line each, newest last. Marks the asks as re-read for the Stop gate. |
| `hubkit order block ID --on "..." [--fix "..."] [--holder who]` | Record what blocks an order and the fix running now (a second call with the same `--on` adds the fix). No fix, or a fix older than `blocker_fix_min` with no result, alerts and holds the Stop gate. |
| `hubkit order result ID --note "..."` | Record the result of an action: movement, clears the blocker and any STOP-LOSS. Notes from `order move` do neither. |
| `hubkit order hold ID --quote "..."` / `resume ID` | Hold (with the user's words) and resume. |
| `hubkit order cancel ID --quote "..."` | Cancel and archive. |
| `hubkit order close ID --evidence name=value ...` | Close; every name in the kind's `done_when` is required. `file:<path>` values must exist and be newer than the order. Every PR number the order names must appear in the evidence. |
| `hubkit order metrics [--day YYYY-MM-DD]` | Orders and chases for a day, as JSON. |
| `hubkit work add --kind active\|tracked\|priority --title "..." [--owner X] [--note ".."]` | Add a live work item; prints its id. |
| `hubkit work add --kind routed --owner X --title "..." [--pr URL ...] [--head SHA]` | Record routed work by hand. |
| `hubkit work add --kind ask --title ".." --note ".."` | Register a question that really blocks work on the user; his card answer closes it. |
| `hubkit work add ... [--estimate N]` | `--estimate` is the minutes the job should take (routed items default to the usual time for their step). |
| `hubkit work estimate ID --minutes N --why "..."` | Hub-internal. Record a new estimate for a routed item or live order beside the old one (never over it). `N` must exceed the minutes already spent and `--why` is required. This is one way to address an overrun. |
| `hubkit work block ID --on OTHER` | Hub-internal. Say that routed item ID waits on job OTHER: while OTHER is live, ID raises no STALLED/OVERDUE or SLOW. `hubkit work block ID` (no `--on`) clears it. |
| `hubkit work done ID --note ".."` | Archive an item. |
| `hubkit work ls` | List live items. |
| `hubkit tick [--if-bound] [--no-notify]` | One watchdog pass; prints `ALERT ...` lines. `--if-bound` is a no-op unless a hub is bound or an order is open (for cron). |
| `hubkit watch [--interval S] [--max-cycles N]` | Loop `tick` (defaults 300 s, 12 cycles). Exits 0 printing `ALERT ...`, 3 after the cycles, 4 if another watch runs. |
| `hubkit eod [--day D] [--note ".."]` | Write and print the end-of-day review, including the day's PRs and what each peer session was asked. |
| `hubkit doctor [--check-clean]` | Print config, state, bind, hook heartbeats, ledger health and a count of secret-shaped strings in state. Warns when your `intents.toml` lacks keys the example has, when a hubkit hook is registered twice in `settings.json` (read only), and when no scheduler entry runs `hubkit eod`. `--check-clean` also lists checkout files containing a `private.terms` word. Exit 1 on a problem. |
| `hubkit hook EVENT` | Run one hook (`session`, `prompt`, `answer`, `routed`, `stop`, `record`, `pretool`); reads the payload from stdin. For Claude Code, not for people. |

## learn.py

`python3 ~/.claude/skills/hub/scripts/learn.py [--file PATH] VERB`

| Verb | What it does |
|---|---|
| `add --class C --quote "..." [--gate G1..G8] [--date D] [--situation ..] [--wrong ..] [--right ..] [--evidence ..]` | Record a class of mistake, or a dated repeat of one. |
| `list [--last N]` | The N most recently seen entries (default 30). |
| `promote-candidates [--min N]` | Classes seen at least N times (default 2). |
| `promote CLASS --gate G --rule "..."` | Write a rule line tagged `[CLASS]` into the overlay under its gate. |
| `address --class C --how "..." [--check CMD]` | Mark a class fixed. Refused unless the overlay or SKILL.md changed since it was last seen and holds a `[C]` rule line. `--check` also runs a command that must exit 0 first, and every earlier `--check` must still pass. Promote is refused past the overlay's 200-line cap. |
| `review [--day D]` | New, repeated, regressed, holding and unaddressed classes for a day, plus rules to expire (addressed and not seen since) and rules that exist only as prose. A class seen again after it was addressed is marked ineffective. |

## Example

```console
$ hubkit work add --kind active --title "triage the flaky test"
W1
$ hubkit tick --no-notify
$ hubkit status
hubkit: you are the hub session for the user. Re-read the hub SKILL.md and the overlay before acting.
status built 0 min ago
active: W1 triage the flaky test
mep: UNVERIFIED (mep CLI not found; it is optional, set [mep] enabled = false in config.toml)
hook errors in scan window: 0
open orders: 0
```
