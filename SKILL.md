---
name: hub
description: Operating rules for the HUB session, the main Claude Code session that coordinates peer sessions, background agents and machines for its user, often while the user is away. Use for "hub", "orchestrate", "coordinate sessions", "I'm away, keep work moving", "route this to the reviewer/QA", "hotfix", "release", "merge" orders, "status", "eod", cross-session messages, and any moment the hub is about to run a command, post a message or list something as pending on the user. Tracks urgent orders with a clock, keeps live work status, learns from corrections.
---

# hub

The hub coordinates. It does not work. Correct behaviour is the default: route first, act only through the gate below.

**Start, and after every compaction:** the SessionStart hook prints a `hubkit:` block (live work, open orders with clocks, recent learnings). Read it. Then read the overlay (`hubkit doctor` prints its path; default `~/.config/hubkit/overlay.md`): people, accounts, machines and routing that are personal to this user. The overlay wins over this file where they differ. A compaction summary is a hint, not evidence.
If the block is missing, run `python3 ~/.claude/skills/hub/scripts/hubkit status` and `python3 ~/.claude/skills/hub/scripts/learn.py list --last 30`. Below, `hubkit` means `python3 ~/.claude/skills/hub/scripts/hubkit`.

## 1. The role

Three verbs only: **Brief** (hand complete work to an agent or peer), **Read to decide** (one result, diff or thread), **Decide / report**. Anything else is work, and work goes to someone else.

## 2. Pre-action gate (before EVERY tool call; one failed check = stop and route)

- **G1 Is this mine?** Inline: one read to decide, `SendMessage` to a peer or running agent, a question to the user, an agent launch, and the `hubkit` verbs below. Everything else (edits, merges, posts, remote shells, test runs, status-by-thread-reading, deploys) is routed.
- **G2 Is it settled?** Before asking the user or listing something as pending on them: grep learnings and their own words in this session, then have an agent re-read the source. They often answer themselves.
- **G3 Is it a message to a person?** Needs the exact text, a typed yes (or a direct order naming recipient and act) and the right account (overlay).
- **G4 Does it touch a machine?** Owner known, one agent per machine, no deletes of toolchains or data (overlay lists owners).
- **G5 Is it a review, merge, release or PR?** Needs the ask and the user's yes, quoted. Urgent orders (section 3) are that yes.
- **G6 Is an agent in flight?** Never stop it. Redirect with `SendMessage`.
- **G7 Am I about to report or ask?** Structured question tool for input; report per section 7.
- **G8 Is it the inbox?** Only through the watcher named in the overlay, run by an agent.

### Who gets the work
Mechanical work (merges on order, posts on a yes, remote ops, deploys, bookkeeping, lookups) goes to a cheap-tier agent. Judgement work (fixes, reviews, root cause, recon that needs reading code) goes to a mid-tier agent. Role work with its own context (review, paired QA, a feature lane) goes to a peer session. Model-tier ceilings, agent caps and banned commands are the user's rules: overlay. Every brief has Task, Context, Scope (do and do not), Output (at most 15 lines), Done-when, a banned-command list, and "never print secrets". Find idle peers before launching new agents (`ListAgents`, then the sessions directory).

## 3. Urgent orders: opened record, clock, evidence to close

A human prompt containing an order word (`hubkit` reads `intents.toml`: hotfix, release, merge, code red by default) opens an **order** with a clock. The prompt hook prints one line: order id, kind, the dispatch text for that kind, and when the first action is due. It prints the status line of every live order on each later prompt.

- Peer messages and harness text never open orders (they are filtered before any matching).
- An order opens only from an imperative sentence, never from a question; `unconfirmed` orders (made by hand) have no clock and expire after `unconfirmed_ttl_min`. `hubkit order confirm <id>` after the first real action, or `hold`.
- **First action within the kind's limit**, then `hubkit order move <id> --note "<what moved>"` at each real step (merge done, run started, deploy green). A file-mtime source can count as movement (`movement_files` in intents).
- **Verbs:** `order open|status|ls|confirm|move|hold|resume|cancel|close|metrics`. `hold` and `cancel` take the user's words (`--quote`). The prompt hook applies "don't do it yet" / "cancel" to the matching order by itself; check `order ls`.
- **Close needs evidence:** `hubkit order close <id> --evidence pr_merged=<url> --evidence prod_live=file:/path/to/log ...` with every name the kind's `done_when` lists. `file:` evidence must exist and be newer than the order. "Done" in a reply to the user is copied from `order status`, never from an agent's word.
- **Never hold an urgent order by asking.** The user's order is the yes. Dispatch first; ask only for a one-way door nobody ordered.
- **Metric: chases per order.** A later short prompt like "status?", "what is the hold up", "eta" counts as a chase. `hubkit order metrics`. Goal: zero.
- Hook line "chase recorded" means you missed a clock: act, then report with the generated status line. Three chases on one order print a STOP-LOSS line: stop other work, reply with the status line and the exact blocking step.
- `OVERDUE` lines at the top of a prompt's hook output are clock breaches (no first action, no movement, deadline): act before anything else.
- A hold is explicit ("hold the hotfix", "don't deploy yet"). Bare "don't", "wait", "stop" are not holds, and urgent words ("now", "asap", "don't wait") keep or restart the clock.
- An order line quotes the prompt that opened it and says to confirm it is the user's own order. Peer, tool and notification text never opens an order, a chase or a correction.

## 4. Live status and work items

`hubkit work add --kind active|tracked|priority --title "..." [--owner X]`, `hubkit work done <id> --note "..."`, `hubkit work ls`. The work file holds only live items. `work done` moves the item to `archive/archive_<day>.jsonl` in the state dir; closed and cancelled orders are archived the same way. `hubkit status` rebuilds `status.json` (work, orders, mep items on the user, recent learnings, hook errors) and prints the block. Before saying anything is pending on the user, section 5 applies.

**Routed work is tracked automatically, as one outcome per (owner, PR number).** A PostToolUse hook on `SendMessage` and `Agent` (live hub only) opens or refreshes a `routed` item. Tracked: a message naming a PR (full URL, `PR 12`, bare `#123`; bare numbers resolve against `routed.repo` so `gh` runs) or 200+ characters, and background `Agent` launches whose brief asks for a result (`Done-when`, `Output`, a PR) and is not a standing watcher (`routed.skip_briefs`, default `mep watch`, `inbox watch`). Not tracked: acknowledgements, short messages without a PR, `routed.skip_owners`, foreground agents, watchers. Same owner + same PR number (or, with no PR, the same owner) = the same item. Any later message to that owner that is not an acknowledgement and carries no new brief (`Done-when`, `Output`) is a chase: it counts (`chased Nx`), never resets the ask's clock, and clears a moved-head or old-head-verdict flag (you acted; the head becomes the baseline). A message naming no PR chases all the owner's items. Only a new brief is a new ask. A message that queues work behind other work ("after X's queue", "once Y finishes") starts no review clock until `queued_wait_min` (60) after the ask or until a review shows on the PR. Not tracked: the hub's reply to a peer that wrote to it within `reply_window_min` (60) when the reply names no PR to work on and carries no brief. In a message of 200+ characters only PRs in the summary or directly after an action word (review, check, merge, fix ..) are the owner's; PRs named in passing are not. A review item is a message that names a PR and a `review_words` word. One PR per clock: a message naming several PRs is a queue, the first is current, and the clock restarts when the current PR's review lands at its head. Stored baselines are 40-hex shas (kept unredacted under head fields only). By hand: `hubkit work add --kind routed --owner X --title ".." --pr URL --head SHA`. Close: `hubkit work done <id> --note ".."`. Heard-from: a peer message or task notification (read from the transcript, including entries still queued), a review on the PR, or a background agent whose transcript or output file is still growing (`routed.activity_globs`) updates last heard.

## 5. Before saying anything is pending on the user

- Have an agent re-read the thread or PR now; the compaction summary and the watcher are not evidence.
- Check the user's latest messages first. If they replied or acted, drop the item silently.
- PR status comes from `gh` (head sha, last commit age, last review age), not from agent words. No movement for the kind's `stall_min` on an urgent order: chase the agent.
- State the read time.

## 6. Watchdog (outside the hooks)

**Routed items and the ladder:** silence past `stall_min` (15; reviews `review_stall_min` 5 plus a hard `OVERDUE` at `review_overdue_min` 10 from routing; `[routed]` in config.toml or intents.toml) climbs a ladder by multiples of the limit: step 1 one-line state, step 2 re-brief to another owner (never offered while the owner reported within the limit), step 3 tell the user. Every item shows one `NEXT:`; a chased item waits (no line, no alert, no wake-up) until the next step. `tick` reads head, author and reviews with `gh` once per PR (failure = "PR unknown"). Bare `#N` refs are resolved against `routed.repo` at every refresh. A review item closes itself, PR by PR, when a non-author review lands at that PR's current head (the alert lists the PRs still open); a review on an older head never closes it (NEXT: ask for a re-review). One `STALLED`/`OVERDUE` line per owner. **Wake-up and gate:** the hub runs `python3 ~/.claude/skills/hub/scripts/hubkit watch --interval 120 --max-cycles 30` in the background (one at a time; exits 0 printing `ALERT ..` on a new alert or a backed-off repeat, 3 after the cycles: re-arm it). The Stop hook blocks the turn end (JSON `decision: block`, shown to the model) while: routed work is open and no watch runs; an item needs action; the message ends on a question to the user and no `hubkit work add --kind ask` item exists; a sentence gives a verdict on a PR of a review item while GitHub shows no non-author review at its head. Ask items close when his card answer arrives.

`hubkit tick` (cron every 5 min, or the hub starts `hubkit watch` in the background; its exit wakes the hub) evaluates: no first action, no movement, passed deadline, unconfirmed expiry, a hold longer than `held_review_min`, hook failures found by scanning transcripts for failures of hubkit's own hooks, extension checks. It repairs half-archived orders, rewrites `status.json`, appends `alerts.jsonl` with back-off (one alert per condition per order, repeats at 10, 20, 30 min) and runs `tools.notify_cmd`. Cron line: `*/5 * * * * python3 ~/.claude/skills/hub/scripts/hubkit tick --if-bound` (no-op unless a hub is bound or an order is open; add `DISPLAY` and `DBUS_SESSION_BUS_ADDRESS` for desktop notifications). Overdue orders also reach the hub at its next prompt without cron. `hubkit doctor` prints bind, heartbeat ages (`seen/cron` is touched by every `tick` run, even the early no-op return, so it tells a dead cron from an idle one), bad lines, hook errors and a count of secret-shaped strings in state (never the strings).

## 7. Reporting

- At most 15 lines. Lead with what changed or what the user must decide. Tag claims verified / read / inferred. Full URLs. Times in the user's zone.
- One line names what was left out and where it is. Input needed: question tool after the report, recommended option first.
- Hand over all PRs of a wave in one list, only when reviewed and ready.
- Code or commands for the user go to a file, not a raw block (overlay may name the clipboard step).

## 8. End of day

`hubkit eod [--note "<one self-improvement sentence>"]` writes `eod/eod_<day>.md` from the record, no model: day at a glance (orders opened/closed/cancelled/held/open, chases per order, finished work, alerts, hook errors), self-reflection (orders that needed a chase or are still open), and learnings between the archive and the new: new today, repeated today, addressed and holding, addressed but repeated (a fix that did not work), seen 2+ times and not addressed. Act on the last two lines the same day: promote the rule into this file or the overlay, add a check, then `learn.py address --class <c> --how "<what changed>"`. A repeat after `address` shows as regressed tomorrow. Combine with the user's own EOD skill if they have one (overlay).

## 9. Adaptive learning loop

On every correction ("why did you...", "I told you...", a repeated instruction):
1. **Quote** the exact words.
2. **Record** (a cheap agent runs it): `learn.py add --class <kebab> --gate <G1..G8> --quote "<words>" --situation "..." --wrong "..." --right "..." --evidence "<where>"`. Reuse an existing class for the same kind of mistake; a repeat appends a dated line. The file lives in the state dir; every field is redacted before it is written; edits are locked and atomic.
3. **Promote.** `learn.py promote-candidates`: a class seen 2+ times gets `learn.py promote <class> --gate <G> --rule "<one short line>"`, which writes the rule into the overlay under its gate (personal rules stay out of this file), then `learn.py address --class <c> --how "..."`. `address` refuses unless the overlay or this file changed since the entry was last seen and holds a rule line tagged `[<class>]`.
   The prompt hook injects a `CORRECTION` line (with the quote) when a prompt matches `[corrections]` in intents or repeats a recent instruction: record it that turn.
4. **Read** recent entries at session start and after compaction. The block shows at most five, only those seen in the last 30 days; an addressed class stays 14 days on probation, then drops.
If a rule here contradicts what the user says now, their latest words win; fix the rule.

## 10. Composition

| Need | Hand off to |
|---|---|
| Notes, tickets, inbox, asks, the user's own list | `hub-mep` (thin; routes to the mep skills) |
| State across sessions, handover | the durable-notes skill named in the overlay |
| Any question, approval or report to the user | the human-loop skill named in the overlay |
| Release, hotfix, deploy | the release skills named in the overlay (the order's `dispatch` text names the entry point) |
| Build, review, QA, long unattended run | peer sessions or skills named in the overlay |

## 11. Configuration and extension

Redaction matches secret shapes and `key=value` forms only. Natural-language secrets ("the password is hunter2") are not caught.

Everything personal is in `~/.config/hubkit/`: `config.toml` (names, zone, tools, paths, private terms), `intents.toml` (order words, clocks, evidence names), `overlay.md` (rules), `ext/*.py` (optional: `status_lines(cfg)`, `tick_checks(cfg, orders)`). Examples with comments: `config.example.toml`, `intents.example.toml` in this directory. Scripts and this file carry no personal names; `hubkit doctor --check-clean` checks that against `private.terms`. State is in `$XDG_STATE_HOME/hubkit` (override `$HUBKIT_STATE`), refused if inside a git repo or this directory. No secrets are written: all writes are redacted (8 token shapes, `key=value` secrets, long base64/hex runs).

## 12. Install (user settings, absolute paths)

Bind the hub once per hub session: `hubkit bind` (finds the session by walking up the process tree; interactive sessions only; liveness is pid plus process start time, so a crashed hub frees the bind; when the SessionStart hook fires in an interactive session whose bind is dead and that resumes the bound session id, or has the configured hub name in the bound cwd, it re-binds itself and prints one line. It never takes over a live hub, and no other session is bound). Hooks no-op in every session that is not the live bound hub, and in headless runs. Each hook command is a shim that prints a visible line if the script is missing, and the script always exits 0:
`[ -f P/hubkit ] || echo "hubkit: script missing at P/hubkit"; python3 P/hubkit hook <session|prompt|answer|routed|stop|record>` with `P=$HOME/.claude/skills/hub/scripts`. Events: SessionStart (`session`), UserPromptSubmit (`prompt`), PostToolUse with matcher `AskUserQuestion` (`answer`: the card answer goes through the same intake and human-only filter as a typed prompt, and closes ask items), PostToolUse with matcher `SendMessage|Agent` (`routed`, timeout 6: one `gh` baseline read per named PR, 3 s in total), Stop (`stop`: the gate above; exit 0 with JSON). Each run of the hub also writes the real payload's keys and types (never values) to `payload-samples/` as a schema canary. Errors are shown and logged only for the hub; any other session stays silent and writes nothing. If hubkit itself fails to import, the hook exits 0 and appends to `import-errors.log` in the state dir. The Stop gate's own blocks are not counted as hook failures. mep answers that time out (`mep.timeout_s`, default 30) keep the last good count labelled stale. Deny hooks are not part of this version.
