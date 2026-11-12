# Command reference

`hubkit` below means `python3 ~/.claude/skills/hub/scripts/hubkit`.

## Exit codes and `--json`

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | `doctor` found a problem (a secret-shaped string, or a private term with `--check-clean`) |
| 2 | bad usage: a missing or invalid argument, or another invalid request (also `bind` refusals) |
| 3 | refused: the request is valid but hubkit will not do it (`order close` without evidence, an order in a state that forbids it, `work done` on a request) |
| 4 | not found: an unknown order, work, request, rule or playbook id or name |

`hook` always exits 0 and `watch` has its own codes (below). Every error prints `error: <message>` and a last line `NEXT: <exact command>` on stderr. With `--json` on a verb that has it, stdout carries one JSON document and nothing else; an error also prints `{"error": "...", "next": "...", "code": N}` on stdout. Without `--json`, output stays the human text. `hubkit --help` and `hubkit VERB --help` show examples and the exit codes.

## Verb table

| Verb | What it does | Read / write | `--json` |
|---|---|---|---|
| `next` | The one most urgent action now with its command | read | yes: `{"kind","id","why","next"}` |
| `mine` | Everything open by whose court it is in, one entry per thread | read | yes: `{"lines","threads"}` |
| `thread ls`, `thread show ID` | Threads (an ask with its routed jobs, PRs and replies owed); the full chain with times | read | yes: `{"threads": [...]}`, `{"lines","thread"}` |
| `thread link ID ROOT`, `thread unlink ID` | Put a record under a root ask, take it out | write | no |
| `status` | Rebuild and print the status block | write (status.json) | yes: the status document |
| `order ls`, `order status [ID]` | Live orders | read | yes: `{"orders": [...]}` |
| `order open\|confirm\|move\|block\|result\|hold\|resume\|cancel\|close` | Change an order | write | no |
| `order metrics` | A day's orders and chases | read | always JSON |
| `work ls` | Live work items | read | yes: `{"work": [...]}` |
| `work add\|done\|estimate\|block\|verify` | Change work items | write | no |
| `req ls` | Open requests (with parts); `--learned`: the verbs intake muted and why | read | yes: `{"requests": [...]}`; `--learned`: `{"muted", "keys", "misses"}` |
| `req show ID` | The full record: text, parts, notes, chases, age | read | yes: `{"request": {...}}` |
| `req done\|drop\|note\|add\|relearn` | Change a request, open one by hand, restore a muted verb | write | no |
| `peers ls` | The peer roster | read | yes: `{"peers","stats","briefs","role_corrections"}` |
| `peers set\|forget` | Change the roster | write | no |
| `inbound ls` | Open asks from other people | read | yes: `{"lines"}` |
| `inbound ingest\|replied` | Track an ask, mark it answered | write | no |
| `standing ls` | Active standing rules | read | yes: `{"rules": [...]}` |
| `standing add\|drop` | Change standing rules | write | no |
| `how ls` | Playbooks | read | yes: `{"playbooks": [...]}` |
| `how show\|match`, `how save` | Read a playbook, save one | read, write | no |
| `gate ls` | Stop-gate checks | read | yes: `{"lines"}` |
| `gate false-alarm\|restore\|set` | Change a check | write | no |
| `find WORD` | Search orders, work, asks, PRs | read | yes: `{"lines"}` |
| `digest` | What finished and what waits | read | yes: `{"lines"}` |
| `doctor` | Health check | read | yes: `{"ok","exit","lines"}` |
| `asks ls`, `trust ls`, `eod`, `tick`, `watch`, `bind`, `unbind`, `skills`, `hooks`, `hook` | See below | mixed | no |

## hubkit

| Verb | What it does |
|---|---|
| `skills --install DIR` | Copies the hub, hub-mep and hubkit-setup skills into DIR (re-run refreshes; the running copy is left alone). |
| `hooks [--skills-dir DIR] [--merge-guard]` | Prints the `hooks` JSON for settings.json. `--merge-guard` adds the warn-only PreToolUse guard (matcher `Bash|TaskStop|AskUserQuestion|CronCreate|ScheduleWakeup|Edit|Write|NotebookEdit`, see [architecture](architecture.md#hooks-scriptshubcorehookspy)). Never edits settings.json. |
| `hubkit bind [--pid N]` | Bind this session (found by walking up the process tree, or the process `N`) as the hub. Interactive sessions only; refuses while another hub is live. |
| `hubkit unbind` | Remove the bind. |
| `hubkit next [--json]` | The single most urgent action for the hub now, with its exact command. In this order: a Stop-gate blocker or STOP-LOSS on an open order; an overdue order (deadline, no first action, stalled, held too long); a reply due now; stalled routed work (the next ladder step); a reply owed or an overdue request; any other watchdog alert; a question waiting on the user (`waiting on the user: ...`); else `nothing is due`. Built from the lines the hooks and the tick already produce; it changes nothing. `--json`: `{"kind","id","why","next"}`. |
| `hubkit status [--json]` | Rebuild `status.json` and print the status block (or the JSON). |
| `hubkit order ls` | One line per live order. |
| `hubkit order status [ID]` | The status line of one order, or all live orders. |
| `hubkit order open --kind K --quote "..." [--unconfirmed]` | Open an order by hand. |
| `hubkit order confirm ID` | Turn an unconfirmed order into an open one. |
| `hubkit order move ID --note "..." [--estimate N]` | Record real movement. The first move also records the order's estimate (N minutes, else the usual time for its kind; printed when defaulted). |
| `hubkit digest [--since 12h]` | What finished, which orders closed, the latest alerts, what waits on the user, the PRs the user's account opened and what each peer session was asked, for a user coming back. |
| `hubkit inbound ingest [--file F]` | Turns the inbox watcher's lines into `reply` work items (one per asker, no duplicates). |
| `hubkit inbound ls` | One screen: each open ask with person, where, age, the person's learned usual wait, each PR it names with its state read from GitHub now, and `reply due: yes/no` (yes when every PR is merged, closed or reviewed at its head). |
| `hubkit inbound replied ID --note "..."` | The person was answered: ID is the work id (`W3`) or the chat message id. Archives the reply item; the note is required. A chat-send command in a Bash call that names the message id or the person does this automatically (`tools.send_regex`). |
| `hubkit trust record --class C --outcome O` / `trust ls` | Per-class record of approved and corrected outcomes; a class with repeated corrections is shown as ask-first. |
| `hubkit how ls` / `show NAME` / `match TEXT` / `save NAME --trigger X --steps-file F --quote Q [--replace]` | Playbooks in `<config dir>/playbooks/`: a prompt that matches a trigger gets the steps injected; an order or imperative prompt with none is told to save one, and the Stop hook holds the turn end until it is. `--replace` keeps the old file as `.bak-<date>`. |
| `hubkit work verify ID` | Stamp the item as checked against reality just now. An `active` or `priority` item not verified for `watcher.verify_stale_min` raises an alert. It runs no check itself. |
| `hubkit peers ls` / `set NAME [--role ROLE] [--note TEXT] [--busy\|--free]` / `forget NAME` | The peer roster: role and free/busy state learned from the peers' own messages (or set by hand), how old that is (stale past `roster.stale_min`), jobs done, median minutes and late count per peer, the corrections that followed briefs to each role, and the hosts in use (`guards.hosts`). `forget` drops a peer and its record. |
| `hubkit find WORD [--since 2d]` | Searches orders, work, the archive, the user's asks, peer session prompts and the PRs the user's account opened (title, body, changed file paths). Needs `gh` for the PR part; a `gh` failure is printed as UNKNOWN, never skipped. Read only on peer sessions; prompts are redacted. |
| `hubkit req ls [--learned]` / `show ID` / `done ID --evidence ".." [--part N]` / `drop ID --quote ".." [--part N]` / `drop ID --not-a-request` / `note ID --note ".."` / `add "text"` / `relearn VERB` | Requests: the user's plain asks. `done` is refused without evidence, `drop` without the user's words. A request with parts: `done ID` is refused (exit 3) while a part is open, naming them; `--part N` closes one part and the last one closes the request. `drop --not-a-request` closes a false intake with no quote and teaches intake; `add` opens a request the intake missed (logged as a miss, shown in `eod`); `relearn VERB` restores a muted verb. |
| `hubkit mine` | Read only: everything open, grouped by whose court it is in (the user, the hub, peers and agents, other people), one line each with id, age, title and link. Under the user it lists the PRs they owe a re-review on (`[review_debt]`); under the hub, standing rules that fired and are not yet acted on; an item whose every named PR is merged or closed ends with ` — PR merged 2d ago, probably stale; last activity 5d ago` (`[stale]`, at most `stale.max_reads` gh reads, warn only). |
| `hubkit standing ls` / `add --on approved\|inbound --quote ".." --action ".." [--repo OWNER/NAME] [--pr URL] [--from PERSON] [--word WORD] [--kind ORDERKIND] [--cmd "CMD"] [--until DATE]` / `drop ID --quote ".."` | Standing rules: what the user said to do every time something happens. `add` is refused without the user's quote and an action; `drop` without their words. `approved` fires on a counting non-author approval at the current head of a PR hubkit tracks (routed, reply or order PRs, or the rule's `--pr`); `inbound` fires on a new ask from `inbound ingest`, scoped by `--from` and/or `--word`. A firing opens an order (the action names an order kind such as merge, or `--kind`) or an `active` work item, prints one line with the user's quote and the exact NEXT (for a merge: `standing.approved_cmd` with the head sha, `--match-head-commit <sha>`; `--cmd` replaces it; CI red or pending turns it into a hold), once per PR head or message. hubkit never runs the action. `--until` takes `2026-12-01` or `2026-12-01 18:00` in the user's zone; an expired rule does not fire. |
| `hubkit asks ls [--since 12h]` | The user's recent prompts (from `recent-prompts.jsonl`), one line each, newest last. Marks the asks as re-read for the Stop gate. |
| `hubkit order block ID --on "..." [--fix "..."] [--holder who]` | Record what blocks an order and the fix running now (a second call with the same `--on` adds the fix). No fix, or a fix older than `blocker_fix_min` with no result, alerts and holds the Stop gate. |
| `hubkit order result ID --note "..."` | Record the result of an action: movement, clears the blocker and any STOP-LOSS. Notes from `order move` do neither. |
| `hubkit order hold ID --quote "..."` / `resume ID` | Hold (with the user's words) and resume. |
| `hubkit order cancel ID --quote "..."` | Cancel and archive. |
| `hubkit order close ID --evidence name=value ...` | Close; every name in the kind's `done_when` is required. `file:<path>` values must exist and be newer than the order. Every PR number the order names must appear in the evidence. |
| `hubkit order metrics [--day YYYY-MM-DD]` | Orders and chases for a day, as JSON. |
| `hubkit work add --kind active\|tracked\|priority --title "..." [--owner X] [--note ".."]` | Add a live work item; prints its id. |
| `hubkit work add --kind routed --owner X --title "..." [--pr URL ...] [--head SHA]` | Record routed work by hand. |
| `hubkit work add --kind ask --title ".." --note ".."` | Register a question that really blocks work on the user; their card answer closes it. |
| `hubkit work add ... [--estimate N]` | `--estimate` is the minutes the job should take (routed items default to the usual time for their step). |
| `hubkit work estimate ID --minutes N --why "..."` | Hub-internal. Record a new estimate for a routed item or live order beside the old one (never over it). `N` must exceed the minutes already spent and `--why` is required. This is one way to address an overrun. |
| `hubkit work block ID --on OTHER` | Hub-internal. Say that routed item ID waits on job OTHER: while OTHER is live, ID raises no STALLED/OVERDUE or SLOW. `hubkit work block ID` (no `--on`) clears it. |
| `hubkit work done ID --note ".."` | Archive an item. Refused with exit 3 for a `request` item: use `hubkit req done ID --evidence ".."`. |
| `hubkit work ls` | List live items. |
| `hubkit tick [--if-bound] [--no-notify]` | One watchdog pass; prints `ALERT ...` lines. `--if-bound` is a no-op unless a hub is bound or an order is open (for cron). |
| `hubkit watch [--interval S] [--max-cycles N]` | Loop `tick` (defaults 300 s, 12 cycles). Exits 0 printing `ALERT ...`, 3 after the cycles, 4 if another watch runs. |
| `hubkit eod [--day D] [--note ".."]` | Write and print the end-of-day review, including the day's PRs and what each peer session was asked. |
| `hubkit gate ls` | Every Stop-gate check: mode (hold, warn, off), fires and false alarms in the window, and why (default, learned, set by hand). |
| `hubkit gate false-alarm ID --why ".."` | Say a check fired wrongly (the id is in the hold line). Enough of them downgrade it to a next-prompt warning; see `[gate]`. `verdict`, `ci`, `merge-state` never downgrade. |
| `hubkit gate restore ID` / `hubkit gate set ID hold\|warn\|off` | Put a check back to its default and forget its false alarms, or set its mode by hand (learning leaves it alone). |
| `hubkit doctor [--check-clean]` | Print config, state, bind, hook heartbeats, ledger health and a count of secret-shaped strings in state. Warns when a Stop-gate check has downgraded itself, when your `intents.toml` lacks keys the example has, when a hubkit hook is registered twice in `settings.json` (read only), and when no scheduler entry runs `hubkit eod`. `--check-clean` also lists checkout files containing a `private.terms` word. Exit 1 on a problem. |
| `hubkit hook EVENT` | Run one hook (`session`, `prompt`, `answer`, `routed`, `stop`, `record`, `pretool`); reads the payload from stdin. For Claude Code, not for people. |

## learn.py

`python3 ~/.claude/skills/hub/scripts/learn.py [--file PATH] VERB`

| Verb | What it does |
|---|---|
| `add --class C --quote "..." [--gate G1..G8] [--date D] [--situation ..] [--wrong ..] [--right ..] [--evidence ..]` | Record a class of mistake, or a dated repeat of one. |
| `list [--last N]` | The N most recently seen entries (default 30). |
| `promote-candidates [--min N]` | Classes seen at least N times (default 2). |
| `promote CLASS --gate G --rule "..."` | Write a rule line tagged `[CLASS]` into the overlay under its gate. Refused at `learn.overlay_max_lines` lines, listing rules to merge or drop. |
| `address --class C --how "..." [--check CMD]` | Mark a class fixed. Refused unless the overlay or SKILL.md changed since it was last seen and holds a `[C]` rule line. `--check` also runs a command that must exit 0 first, and a class that regressed (seen again after an address) needs `--check`; every earlier `--check` must still pass. Promote is refused past the overlay's 200-line cap. |
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
