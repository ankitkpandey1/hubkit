# Architecture

hubkit is a set of Claude Code hooks plus a small CLI, all standard-library Python. Everything it knows is on disk in a state directory; the model is not trusted to remember.

```
 you (typed prompt, card answer)           peers / agents (SendMessage, Agent)
          |                                              |
          v                                              v
   UserPromptSubmit hook                       PostToolUse hook "routed"
   (prompt)  PostToolUse "answer"              (SendMessage|Agent)
          |                                              |
          v                                              v
  +----------------+    opens/chases     +---------------------------+
  |  orders.jsonl  |<--------------------|  work.json  (routed items) |
  |  order ledger  |                     |  one per (owner, PR)       |
  +-------+--------+                     +-------------+-------------+
          |                                            |
          |      +-------------------------------+     | gh reads: head, reviews
          +----->|  tick / watch  (watchdog)     |<----+ transcript: last heard-from
                 |  alerts.jsonl, status.json    |
                 +---------------+---------------+
                                 |
          +----------------------+------------------------+
          v                                               v
   SessionStart / prompt hooks print                Stop hook = gate
   the status block + OVERDUE / CORRECTION          blocks the turn end while the hub owes
          |
          v
   hub session  --(learn.py add / promote / address)-->  learnings.md  --> overlay.md
        ^                                                                       |
        +----------------------- rules read at the next session ----------------+
```

## State and the bound hub

State lives in `$HUBKIT_STATE`, else `$XDG_STATE_HOME/hubkit` (default `~/.local/state/hubkit`). It is refused when inside a git repository or the skill directory, so it is never committed. Files are append-only JSONL or written by atomic replace under an `flock`; every write goes through the redactor (`scripts/hubcore/redact.py`).

`hubkit bind` records the hub as session id + pid + process start time (read from `/proc`), so a crashed hub frees the bind by itself. Every hook first asks `is_hub`: it is true only for the live, bound, interactive session. In any other session a hook prints nothing and exits 0. If hubkit fails to import, the hook exits 0 and appends to `import-errors.log`.

## Hooks (`scripts/hubcore/hooks.py`)

| Event | Hook | What it does |
|---|---|---|
| SessionStart | `session` | Prints the `hubkit:` status block. If the bind is dead and this session resumed the bound session id (or has the configured hub name in the bound cwd), re-binds and prints one line. Never takes over a live hub. Also sets the `needs_reload` marker in the state dir; the prompt hook prints, and the Stop hook blocks on, "hub skill not loaded since compaction" until the `skill` hook sees the `hub` skill load. |
| UserPromptSubmit | `prompt` | Runs the intake on the typed prompt (see Orders), prints the order lines, `OVERDUE` clock breaches, the status block, and a `CORRECTION` line when the learn loop applies. |
| PostToolUse `AskUserQuestion` | `answer` | A card answer goes through the same intake and human-only filter as a typed prompt, and closes `ask` work items. |
| PostToolUse `SendMessage\|Agent` | `routed` | Records or refreshes a routed-work item (see Routed work). One `gh` baseline read per named PR, 3 s in total. |
| PostToolUse `Skill` | `skill` | Loading the `hub` skill clears the `needs_reload` marker. Any other skill leaves it. |
| Stop | `stop` | The gate. Prints JSON `{"decision": "block", "reason": ...}` while the hub owes something. |
| PreToolUse `Bash\|TaskStop\|AskUserQuestion` | `pretool` | Warn-only guard; the warning goes in as additional context and nothing is ever denied. It warns on: `gh pr merge` with no live order and none of his typed words naming the PR; a post to a person or thread (`mep send|reply|post`, a chat send, `gh pr|issue comment|review`, a `gh api` comments or reviews call, or a Slack post or send) with no typed yes of his in the last 10 minutes; a banned command (`rm -rf`, `pkill`, `git reset --hard`, `git stash`, `git clean`, a forced push, `git checkout <ref> -- <path>`); the same command run 3 times in 10 minutes; `pytest` with no path, `-k` or `-m`; `TaskStop` without a stop word of his in the last 30 minutes; a decision card whose first option is not marked (Recommended). Register it with `hubkit hooks --merge-guard`, or add it to `settings.json` yourself. |
| any | `record` | Schema canary. Every hub run writes the real payload's keys and value types (never values) to `payload-samples/`. |

A hook failure is shown and logged (`hook-errors.jsonl`) only for the hub. The stop gate's own blocks are not counted as failures.

## Orders (`orders.py`)

An order is a row family in `orders.jsonl` (`open`, `move`, `hold`, `resume`, `cancel`, `chase`, `close`, `expire`), folded into one record per id. The kinds, their words, limits and `done_when` evidence names come from `intents.toml`.

- **Intake.** Only human text counts: peer messages, task notifications and harness wrappers are filtered first (denylist of markers plus an `origin` check). A kind whose words match opens an order only from an imperative sentence (an `imperative` word present, not a question, not a `not_order_phrases` phrase). Otherwise the order is `unconfirmed`: no clock, expires after `unconfirmed_ttl_min` unless `hubkit order confirm`.
- **Clock.** A first action is due within the kind's `first_action_min`; after that, movement (`order move`, or a touched `movement_files` glob) is due every `stall_min`. A deadline in the prompt ("in 30 min", "by 17:00") is parsed too.
- **Holds.** Only explicit `hold_words` hold an order; `urgent_words` keep or restart the clock; `cancel_words` cancel. Long prompts (`verb_max_chars`) never hold or cancel.
- **Chases.** A short prompt matching `intake.chase` while an order is open is recorded as a chase. Three chases print a stop-loss line.
- **Close.** `order close` requires every evidence name the kind's `done_when` lists; `file:<path>` evidence must exist and be newer than the order. Evidence names in the kind's `sha_evidence` (the merge kind's `merged`) must hold a commit sha. A closed order is archived under `archive/archive_<day>.jsonl`.

## Routed work (`routed.py`)

A routed item is one outcome per (owner, PR number), or per owner when no PR is named, in `work.json`.

- **Tracked:** a message naming a PR (URL, `PR 12`, bare `#123` resolved against `routed.repo`) or at least `min_chars` characters, and background `Agent` launches whose brief asks for a result (`Done-when`, `Output`, a PR). **Not tracked:** acknowledgements, short messages without a PR, `skip_owners`, foreground agents, standing watchers (`skip_briefs`), and a reply to a peer who wrote to the hub within `reply_window_min`.
- **Asks and chases.** Only a new brief is a new ask. Any later non-acknowledgement message to the same owner is a chase: it is counted, never resets the ask's clock, and clears a moved-head or old-head-verdict flag.
- **Reviews.** A message naming a PR and a `review_words` word ("review #N", "needs a review", "can I get a review on #N") makes a review item, unless it opens with a change verb (fix, address, resolve, apply, ...) and the review word names an existing review ("fix the findings in his review of #N"), on a short clock (`review_stall_min`, hard `OVERDUE` at `review_overdue_min`, or at the brief's own `Estimate:` when that is longer). A job whose title leads with a change verb is not a review item unless a sentence naming the PR leads with a review. It closes itself, PR by PR, when GitHub shows a review by someone other than the author at the PR's current head (on a PR written by `user.github_login`, a COMMENT review with a body counts too, because the reviewer peer posts as that account; an empty-body COMMENT is an inline-thread reply and does not). A review on an older head never closes it. One PR per clock: a message naming several PRs is a queue; "after X's queue" phrasing delays the clock by `queued_wait_min`.
- **Heard-from.** A peer message or task notification in the hub transcript, a review on the PR, or a background agent whose transcript or output file is still growing (`routed.activity_globs`) updates the last-heard time.
- **Ladder.** Silence past `stall_min` climbs: step 1 a one-line state ask, step 2 a re-brief to another owner (not while the owner reported within the limit), step 3 tell the user. Every item shows one `NEXT:` action.

## Watchdog (`status.py`)

`hubkit tick` evaluates: no first action, no movement, passed deadline, unconfirmed expiry, a hold longer than `held_review_min`, routed-item ladders, hook failures found by scanning transcripts for failures of hubkit's own hooks, and extension checks. It repairs half-archived orders, rewrites `status.json`, appends `alerts.jsonl` with back-off (one alert per condition per order, repeats at 10, 20, 30 minutes) and runs `tools.notify_cmd`. It runs from cron (`--if-bound`) and inside `hubkit watch`.

`hubkit watch` is the wake-up: the hub starts it in the background; it exits 0 printing `ALERT ...` on a new alert or backed-off repeat (which wakes an idle hub), and exits 3 after `--max-cycles` (re-arm it). A pidfile allows only one; a second exits 4.

## Job records, estimates and the slowness check (`slow.py`)

Internal to the hub; there is no report or verb for the user. Every live order and routed item has one record, rebuilt each tick into `status.json` and printed as one short `job ...` line in the `hubkit:` block (id, status, minutes in status / estimate, holder, and `SLOW: <why>` when over): the job, its status and when that status began, when it started, the estimate, the holder, and that it is not done.

- **Estimate.** A routed item's brief may carry a line `Estimate: <n>m`. Without it the hook fills in the usual time for the item's step (review, fix push, work) and says `DEFAULTED` in its output. An order gets its estimate at its first `order move`. Estimates are kept as a list on the record (`estimates`); a new one is added beside the old (`hubkit work estimate`), so end of day can show estimate vs actual and how often a job was re-estimated.
- **Usual time (baseline).** The median minutes per step over archived routed items and closed orders (`archive_*.jsonl`). With fewer than `slow.min_samples` samples it falls back to the configured stall limit (or `slow.order_default_min` for an order) and the line says `limit, N samples`.
- **Overrun.** Time on the job (an order from its opening, a routed item from its start, a "hub pickup" from the owner's report) is past the estimate and past `slow.floor_min`. The tick adds one `SLOW <id> <step> <m>m, estimate <e>m; holder <h>; why: <signals>. NEXT: <action>` alert to the existing wake-up path. The why uses only real signals: the holder's session `status` and `waitingFor`, the oldest tool started and not yet answered and for how long, queued messages not yet taken, PR head movement, CI state (`gh`), and whether the session or agent transcript is alive. Only tool names, ids, times and queue operations are read from a transcript, never its text. With no signal the why says `unknown`. The alert key carries the cause, so it repeats only with the usual back-off or when the cause changes.
- **Stop gate (the one exception to "the gate is unchanged").** A routed item or order with an estimate on record that is over it blocks the stop (`hubkit OVERRUN: SLOW ...`) until it is addressed: a chase to the holder, a re-brief of the same PRs to another owner, or `hubkit work estimate`. Items from before estimates existed alert but never block. A re-brief of an owner who reported within the limit is still advised against.
- **End of day.** The self-reflection lists estimate vs actual per step and kind: count, mean estimate, mean actual, over-estimate count, re-estimates.

## Watcher checks (`watcher.py`)

Every tick (cron with `--if-bound`, or inside `hubkit watch`) also checks things outside the hub session, with live jobs as the trigger:

- **Hub gone, stuck or not acting.** `hub:dead` (the bind is not a live process), `hub:stuck` (session `busy` longer than `watcher.stuck_min`), `hub:unhandled` (an alert older than `watcher.unhandled_min` and no hub heartbeat under `seen/` since).
- **Peer waiting.** A session holding a live item whose `waitingFor` is set (or whose status is a waiting state) for `watcher.waiting_min` raises `peer:<name>:waiting` with the holder and the minutes.
- **Watch and cron check each other.** A live job with no running watch raises `watch:dead` (the status block also tells the hub to re-arm it); a watch that finds `seen/cron` stale while the crontab runs hubkit raises `cron:dead`.
- **Blockers.** An order's `block` record (on, fix, holder) turns a report into work: no fix or a stale fix alerts and gates the stop; only an `order result` clears it, and `order move` notes do not reset the idle clock of a blocked order. A new job sent to an owner who already holds one, while an order is open, prints a queue warning.
- **Notification.** New alerts go to `tools.notify_cmd` when set. With none set, only `hub:*` and `peer:*` alerts use the platform default (`notify-send` on Linux, `osascript` on macOS, when installed). Nothing here ever posts to chat.
- **False alarms.** A silent item is not a stall while it is `blocked_on` another live job (`hubkit work block`), or while its holder's session is `busy` and its transcript is still growing (for up to 3x the limit). A head moved by the owner's own newest commit is progress, not a moved head. Every stall alert names the holder's state (`holder: ...`).
- **Short asks.** A short message to a peer is tracked when a request verb opens a sentence or it ends in a question; acknowledgements, status questions and thanks are not.
- **Own work.** `active` and `priority` items need an estimate within 5 minutes, say so out loud when over it, and raise a stale-verification alert when not stamped with `hubkit work verify` for `watcher.verify_stale_min`.
- **Finished agents.** A background agent that reported finished more than `watcher.stale_agent_min` ago while its item is still listed raises `agent:<owner>:stale`.
- **Inbound asks.** `hubkit inbound ingest` opens one `reply` item per message id from the inbox watcher's lines; one owed longer than `watcher.reply_owed_min` raises one alert.
- **PRs are keyed by repo and number.** `owner/repo#N`, a full URL, or `<alias> N` for a `routed.repo_aliases` entry name their repo; only a bare `#N` falls back to `routed.repo`.

## Stop gate (`gate.py`)

The Stop hook blocks the end of the turn, with the exact next action, while any of these holds:

1. Routed work or an open order exists and no watch is running.
2. A routed item needs action now (stalled, overdue, moved head).
3. The final message gives a verdict on a PR of a review item while GitHub shows no counting review at its current head (a non-author review, or a COMMENT review with a body on a PR written by `user.github_login`). Awaiting wording ("waiting on the verdict") is not a verdict. Agent words are not evidence.
4. A job with an estimate on record is past it and not yet addressed (see the slowness check).
5. A blocker is not being solved (no fix running, fix past its limit), a STOP-LOSS has no result since, or the final message calls an open order blocked/down/failed/waiting while no order has a fix on record.
6. The final message says done while his newest prompt is newer than the last `work done` and the last `hubkit asks ls`; or it calls something gone/lost/absent, or "a person's call", with no Read/Grep/Glob/Bash call in the turn (transcript read).
7. The final message ends on a question (or holds a `stop_guard.phrases` phrase) and no `ask` work item says anything really waits on the user. A question that is really blocked on him must use the card tool, not prose. An `ask` item asked 3 times with no answer also holds the turn: take the safest default and record it.
8. The final message says "tests pass", "it works" or "is deployed" (`[stop_gate] verified_words`) but the turn ran no Bash command; names a PR or issue number with no link (`[stop_gate] full_links`); approves a PR while CI at its head is not green; or reports built work while the repo at the hook's `cwd` has uncommitted changes or unpushed commits made since the turn began (older dirt in the checkout does not count).

## Digest and trust

`hubkit digest [--since 12h]` prints what finished, which orders closed, the latest alerts, what waits on the user, the PRs his account opened and what each peer session was asked (`ledger.py`: peer transcripts are read only and prompts are redacted; a `gh` failure is shown, not hidden). `hubkit find WORD` searches the same record plus orders, work, the archive and his asks. `hubkit trust record --class C --outcome accept|edit|reject` appends to `trust.jsonl`; `trust ls` shows each class as `auto` (enough accepts and nothing else) or `ask` (propose first), and the session status block names the classes that earned `auto` and those he rejected.

## The learn loop

This is the adaptive part. The mechanism, in the order it runs (`hooks.py`, `learnings.py`, `scripts/learn.py`):

1. **Detect.** The prompt hook injects a `CORRECTION` line, with the quote, when a typed prompt matches a `[corrections] patterns` regex or repeats a recent prompt (word-set similarity at least `repeat_similarity` within `repeat_window_min`).
2. **Record** (`learn.py add`). The hub records a class of mistake: kebab-case class, the gate it broke (G1 to G8 in SKILL.md), the exact quote, situation, wrong, right, evidence. A repeat of an existing class appends a dated `seen` line. Each entry stores a `basis` hash of the rule texts (overlay plus SKILL.md) at the time. Every field is redacted; edits are locked and atomic.
3. **Promote** (`learn.py promote-candidates`, then `learn.py promote <class> --gate G --rule "..."`). A class seen two or more times is a candidate. Promoting writes one short rule line tagged `[<class>]` into your overlay under its gate. Personal rules stay out of the shared `SKILL.md`.
4. **Address** (`learn.py address --class <c> --how "..."`). The class is marked fixed only if the rules changed since the entry was last seen **and** a rule line tagged `[<class>]` exists. Otherwise it is refused: a fix without a rule is not a fix. With `--check CMD` the command must also exit 0, and every earlier check must still pass. `promote` is refused when the overlay would pass 200 lines. A class seen again after it was addressed is marked ineffective.
5. **Probation.** The status block shows at most five learnings seen in the last 30 days; an addressed class stays visible for 14 days as "probation". If the class is seen again after it was addressed, `hubkit eod` and `learn.py review` list it as regressed (a fix that did not work), and it is to be fixed the same day.

hubkit does the detecting, storing, refusing and reporting; the hub model does the recording and the rule writing, so the loop only adapts as far as the hub follows it.

## End of day (`hubkit eod`)

Writes `eod/eod_<day>.md` from the record, with no model: orders opened, closed, cancelled, held and open; chases per order; finished work; alerts; hook errors; and the learnings between the archive and the new (new today, repeated, addressed and holding, addressed but repeated, seen twice and not addressed).

## Extension points

`<config dir>/ext/*.py` modules may define `status_lines(cfg) -> list[str]` (extra lines in the status block) and `tick_checks(cfg, live_orders) -> list[{"key", "msg"}]` (extra watchdog alerts). A broken extension is logged to `hook-errors.jsonl` and skipped.
