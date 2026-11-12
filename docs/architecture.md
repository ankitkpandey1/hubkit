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
| PreToolUse `Bash\|TaskStop\|AskUserQuestion\|CronCreate\|ScheduleWakeup\|Edit\|Write\|NotebookEdit` | `pretool` | Warn-only guard; the warning goes in as additional context and nothing is ever denied. It warns on: `gh pr merge` with no live order and none of the user's typed words naming the PR; a post to a person or thread (`mep send|reply|post`, a chat send, `gh pr|issue comment|review`, a `gh api` comments or reviews call, or a Slack post or send) with no typed yes from the user in the last 10 minutes; a banned command (`rm -rf`, `pkill`, `git reset --hard`, `git stash`, `git clean`, a forced push, `git checkout <ref> -- <path>`); the same command run 3 times in 10 minutes; `pytest` with no path, `-k` or `-m`; `TaskStop` without a stop word from the user in the last 30 minutes; a decision card whose first option is not marked (Recommended); a Bash command matching `guards.needs_order` (`NEEDS-ORDER`), or a tool in `guards.schedule_tools` / a command matching `guards.schedule_regexes` (`UNORDERED-SCHEDULE`), with no typed ask in the last `guards.window_min` minutes (`guardwatch.py`; `hubkit watch` is exempt); an `Edit`, `Write` or `NotebookEdit` by the hub itself outside `guards.inline_ok_paths`, once per turn (`INLINE-EDIT`); a Bash command matching an `[[accounts.rules]]` `match` without its `require` (`WRONG-ACCOUNT`); a post whose typed yes came before the open `draft:` item was opened or changed. The routed hook also warns when a review brief names a PR nobody asked about (`NO-ASK-REVIEW`) and when a brief names a `guards.hosts` host another live item holds (`HOST-BUSY`). Register it with `hubkit hooks --merge-guard`, or add it to `settings.json` yourself. |
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

## Peer roster and pre-route checks (`roster.py`, `preroute.py`)

- **Roster.** `routed.heard` passes each peer message to `roster.learn`: the opening of the message sets the peer's role (reviewer, worker) and free/busy state, from the words in `[roster]`, stamped with the time. A peer holding a live routed item is busy whatever it said; a state older than `roster.stale_min` is shown as stale and not trusted. `hubkit peers set|forget` edit it by hand (a role set by hand is kept). Stored in `roster.json`.
- **Record.** When a routed item finishes (`_archive`, `work done`), the owner's row gets the job kind, its estimate, the minutes taken and whether it was late. `peers ls` shows done, median and late per peer; the free-peer suggestion ranks by late rate then median for that job kind.
- **At routing.** The routed hook warns on a new ask when: a review ask goes to a non-reviewer while a reviewer is free; non-review work goes to a reviewer-only peer; the owner is busy and a fitting peer is free (with or without an open order). It also warns when `[roles.<role>] brief_must_include` has a phrase the brief lacks, and notes the brief against its role: a correction by the user within `roster.correction_window_min` is counted for that role.
- **Already done.** `preroute.check` reads each named PR (at most two, 2 s in total) with one `gh pr view`; a MERGED or CLOSED PR, or a review ask when a counting review already stands at the current head, gets a warning with the time and a NEXT. No `gh` or a failed read means no warning.

## Requests and the `mine` view (`owed.py`)

A typed imperative statement that opened no order (it starts with a `[requests] verbs` word, or holds an `[intake] imperative` word) opens a request: a `work.json` item of kind `request` holding the user's quote. A question, pasted text and peer text never open one. A short prompt that matches an open request's key words and the chase pattern, or a new ask that repeats an open one, counts a chase and prints the request's line. `req done` needs evidence and archives the item; its time-to-done feeds the learned stall and overdue limits (`owed.limits`, the median via `slow.usual`). The status block lists open requests oldest first; the Stop gate holds a done-claim while a request older than the stall limit has no note within it; `tick` alerts once per request past the overdue limit (normal back-off); `eod` counts requests and chases. Intake also reads a question-form ask whose verb is a request verb ("can you delete the old branch?"; `owed._polite`) but never an information ask ("can you tell me why", `info_verbs`). A numbered or bulleted list of at least two asks, or two or more clauses after a colon that all start with a request verb, becomes `parts` (`{n, text, state, evidence}`) on the one request (`owed.detect_parts`; unsure, or more than `max_parts`, keeps one item): `req done ID --part N` closes a part and notes it, `req done ID` is refused while a part is open, and the last part closes the request with the parts' evidence joined. The request keeps the full prompt (`text`, `text_chars`) and its notes append with a time (`req show`). `req drop ID --not-a-request` logs a false intake against the opening verb in `intake-learn.jsonl`; a verb with `false_min` of them, at least `false_ratio` of its opens in `window_days`, stops opening requests on its own lead (a sentence that also holds an `[intake] imperative` word still opens one, under the key `*imperative`) until `req relearn`; `req add` logs a miss, counted in `eod`. `hubkit mine` is read only and groups everything open: on the user (`ask` items, unconfirmed orders, mep titles), on the hub (requests, open orders, active work), on peers (routed items), on other people (`reply` and `tracked` items).

## Standing rules, review debt, stale items, the eod clock (`standrules.py`, `reviewdebt.py`, `stale.py`, `nopr.py`, `eodclock.py`, `sweeps.py`)

`sweeps.py` is the one seam: the tick, the status block, the prompt hook, `mine` and the digest call it, and each part fails alone (logged to `hook-errors.jsonl`).

- **Standing rules** (`standing.json`: rules, and the fired record). Each rule keeps the user's quote, a trigger and an action. `approved` is checked in the tick before the routed refresh may close a review item: every tracked PR a rule can apply to is read once (`routed.pr_info`, memoized with the refresh), and a reviewer whose latest counting review (`routed.counts_as_review`) is an approval at the current head fires the rule, keyed by rule and PR head. `inbound` is checked by `inbound.ingest` for each new message, keyed by message id. A firing opens an order (`orders.match_kind` on the action, or the rule's `--kind`) or an `active` work item with an estimate, prints one line with the quote and the NEXT, and is shown once in the next prompt. hubkit never runs the action. A rule is unacted while its order or work item is still open; those are listed in `mine` and counted in the status block.
- **Review debt** (`review_debt.json`). A rate-limited sweep reads the open PRs that request the user's review, keeps those the user reviewed before whose last review commit is not the head, and alerts once per PR per head. Read only; a missing or failing gh keeps the last result.
- **Stale items.** `mine` and the digest read the PRs named by open work items (`routed.gh_view`, capped by `stale.max_reads`) and mark an item whose PRs are all merged or closed. Nothing is closed.
- **Routed item with no PR.** After the owner reports done on a non-review item that names no PR, there is nothing to verify; the status block, the tick and (once per `routed.stall_min`) the prompt say to check the result and close it with `hubkit work done`.
- **End-of-day clock** (`eodclock.py`, `eodclock.json`). With `report.eod_time` set, the first tick after that time calls `status.eod` once for the day unless the file exists, and alerts once if that fails or the file is still missing `report.eod_check_min` later.

## Threads (`threads.py`)

One piece of work is up to four records: the user's ask (an order or a request), the routed job, the PR and the reply owed to a person. Each record may carry an optional `thread` field, the id of the root ask (work items in `work.json`; orders, a folded ledger, in `thread-links.json`; a root has none). hubkit never closes a root: the hub confirms.

- **Linking, best match first.** A `For: <ID>` line in a brief links explicitly and wins. Otherwise a record that names the same PR as a thread joins that thread (orders and requests are roots; with none, an inbound ask is the root and adopts routed jobs on its PR). A routed job (at routing time only) links to the only open ask, unless both name PRs and none is shared, else to the one open ask whose key words it shares (`threads.min_shared` words and `threads.root_share` of the ask's own, the `owed.stems` words). Two candidates are ambiguous and link nothing; hubkit never guesses between two. What a standing rule opens links to its PR's thread; the tick links unthreaded records that name a PR of a thread. By hand: `hubkit thread link ID ROOT`, `hubkit thread unlink ID`.
- **View.** `hubkit mine` prints one entry per thread in the group of its current step (a due reply or a report to check: the hub; a routed job: peers; a reply not yet due: other people), the root's title and age, the step and whose court it is in, and an indented line per member. `hubkit thread ls|show [--json]` list threads and print the chain with times (asked, routed, PR state, reply owed to whom). The status block keeps its own per-kind lines; they are not folded by thread.
- **Closing.** When the last member of a live root is closed (a routed item archived on evidence, a reply sent), the tick alerts `<root> "<title>": every step under it is closed (...). NEXT: hubkit req done|order close ...` (the order form prefilled with the evidence names, the PR URL and the sha found in the members' notes), and `work done` prints it at once. Closing a root with members still open prints a warning naming them; it does not refuse. A reply is marked due by `replies.note_states` as before; the thread shows it as owed to the person.
- **Declared check for work with no PR.** A line `Done-when: <text>` in a non-review brief (`routed.done_when_markers`) is stored as `done_when`. When the owner reports done the nudge names it, and `work done` on that item without `--note` is refused (exit 3). A non-review brief with no PR and no such line gets a warn-only hook line (`routed.require_done_when`).

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
- **Reply loop.** `hubcore/replies.py`. During the tick, `routed.refresh_prs` reads each PR once, including PRs named by `reply` items; an item whose every PR is merged, closed or reviewed at its current head is marked `due` and raises one "answer due now" alert with the state and link. The Stop gate holds a final message that says a PR is merged, approved or reviewed (`[stop_gate] reply_claim_words`) while an open reply item names it. `hubkit inbound replied` or a chat-send command naming the person or message id closes the item. The age alert uses the person's own median time to reply from closed reply items (between `reply_floor_min` and `reply_owed_min`, with at least `reply_min_samples` samples).
- **Merge state.** `routed.pr_info` also reads `reviewDecision`, `mergeStateStatus` and review requests (`decision`, `merge_state`, `requested`); `routed.court(info)` returns whose court a PR is in. `hubcore/mergestate.py` holds the Stop reason for a sentence that says a PR is ready to merge or waits on the user's merge (`[stop_gate] ready_words`) while GitHub shows changes requested, a review required, a blocked, behind or conflicting branch, or failing CI. Unreadable `gh` proves nothing.
- **Inbound asks.** `hubkit inbound ingest` opens one `reply` item per message id from the inbox watcher's lines; one owed longer than `watcher.reply_owed_min` raises one alert.
- **PRs are keyed by repo and number.** `owner/repo#N`, a full URL, or `<alias> N` for a `routed.repo_aliases` entry name their repo; only a bare `#N` falls back to `routed.repo`.

## Stop gate (`gate.py`)

A review item's report that lacks a `[roles.<role>] report_must_include` step is flagged by `reviewreport.py` (`REPORT-INCOMPLETE`: a prompt line and a tick alert until a later report shows it); it never changes how the review closes.

The Stop hook blocks the end of the turn, with the exact next action, while any of these holds. Every Stop-gate rule holds the turn once per stop: when the harness has already sent the hub back (`stop_hook_active`), the turn is allowed to end. A final message that states a time marked UTC/GMT/Z outside code, while `user.timezone` is another zone, is held with `hubkit UTC-TIME` (`reportcheck.py`; `[report] zone_check`, `utc_markers`).

1. Routed work or an open order exists and no watch is running.
2. A routed item needs action now (stalled, overdue, moved head).
3. The final message gives a verdict on a PR of a review item while GitHub shows no counting review at its current head (a non-author review, or a COMMENT review with a body on a PR written by `user.github_login`). Awaiting wording ("waiting on the verdict") is not a verdict. Agent words are not evidence.
4. A job with an estimate on record is past it and not yet addressed (see the slowness check).
5. A blocker is not being solved (no fix running, fix past its limit), a STOP-LOSS has no result since, or the final message calls an open order blocked/down/failed/waiting while no order has a fix on record.
6. The final message says done while the user's newest prompt is newer than the last `work done` and the last `hubkit asks ls`; or it calls something gone/lost/absent, or "a person's call", with no Read/Grep/Glob/Bash call in the turn (transcript read).
7. The final message ends on a question (or holds a `stop_guard.phrases` phrase) and no `ask` work item says anything really waits on the user. A question that is really blocked on the user must use the card tool, not prose. An `ask` item asked 3 times with no answer also holds the turn: take the safest default and record it.
8. The final message says "tests pass", "it works" or "is deployed" (`[stop_gate] verified_words`) but the turn ran no Bash command; names a PR or issue number with no link (`[stop_gate] full_links`); approves a PR while CI at its head is not green; or reports built work while the repo at the hook's `cwd` has uncommitted changes or unpushed commits made since the turn began (older dirt in the checkout does not count).
9. An order of a `no_questions` kind (example: hotfix, release) is open and not held, and the final message asks permission, waits for a go, or ends in a question (`urgent-ask`). The `ask` exemption of rule 7 does not apply, and `USER_WAIT` ("waiting for your go") does not excuse it; a blocker recorded as waiting on the user does.

### Check ids and adaptive strength (`gatectl.py`)

`gate.stop_checks` yields `(check id, reason)` pairs; `gate.stop_reasons` passes them through `gatectl.apply`, which logs each fire (`gate-events.jsonl`) and returns only the reasons of checks in hold mode, each ending `[check: ID; wrong? hubkit gate false-alarm ID --why "..."]`. A check in warn mode is queued (`gate-warnings.jsonl`) and shown once as a `hubkit WARNING` line by the next prompt hook (the Stop hook prints nothing when it does not block). A check with at least `gate.false_alarm_min` false alarms making up at least `gate.false_alarm_ratio` of its fires in `gate.window_days` downgrades itself to warn (`gate-modes.json`), except `gate.never_downgrade` and checks set by hand. A correction (`hooks.correction_line`) within `gate.restore_window_min` after it warned restores it and resets its count. `report-length` and `report-code` (`reportcheck.form`, `[report] max_lines`, `max_code_lines`) are warn mode by default. A typed standing rule (`standing.py`, `[corrections] rule_patterns`) adds a `hubkit RULE` line to the prompt output.

## Digest and trust

`hubkit digest [--since 12h]` prints what finished, which orders closed, the latest alerts, what waits on the user, the PRs the user's account opened and what each peer session was asked (`ledger.py`: peer transcripts are read only and prompts are redacted; a `gh` failure is shown, not hidden). `hubkit find WORD` searches the same record plus orders, work, the archive and his asks. `hubkit trust record --class C --outcome accept|edit|reject` appends to `trust.jsonl`; `trust ls` shows each class as `auto` (enough accepts and nothing else) or `ask` (propose first), and the session status block names the classes that earned `auto` and those he rejected.

## The learn loop

This is the adaptive part. The mechanism, in the order it runs (`hooks.py`, `learnings.py`, `scripts/learn.py`):

1. **Detect.** The prompt hook injects a `CORRECTION` line, with the quote, when a typed prompt matches a `[corrections] patterns` regex or repeats a recent prompt (word-set similarity at least `repeat_similarity` within `repeat_window_min`).
2. **Record** (`learn.py add`). The hub records a class of mistake: kebab-case class, the gate it broke (G1 to G8 in SKILL.md), the exact quote, situation, wrong, right, evidence. A repeat of an existing class appends a dated `seen` line. Each entry stores a `basis` hash of the rule texts (overlay plus SKILL.md) at the time. Every field is redacted; edits are locked and atomic.
3. **Promote** (`learn.py promote-candidates`, then `learn.py promote <class> --gate G --rule "..."`). A class seen two or more times is a candidate. Promoting writes one short rule line tagged `[<class>]` into your overlay under its gate. Personal rules stay out of the shared `SKILL.md`.
4. **Address** (`learn.py address --class <c> --how "..."`). The class is marked fixed only if the rules changed since the entry was last seen **and** a rule line tagged `[<class>]` exists. Otherwise it is refused: a fix without a rule is not a fix. With `--check CMD` the command must also exit 0, and every earlier check must still pass. `promote` is refused when the overlay is at or would pass `learn.overlay_max_lines` (default 200) lines, and the refusal lists expired, prose-only and oldest rules to merge or drop; `hubkit doctor` prints the overlay lines against the cap. A class seen again after it was addressed is marked ineffective, shown as `hubkit REGRESSED <class>` at the top of the session status block, and cannot be addressed again without `--check`.
5. **Probation.** The status block shows at most five learnings seen in the last 30 days; an addressed class stays visible for 14 days as "probation". If the class is seen again after it was addressed, `hubkit eod` and `learn.py review` list it as regressed (a fix that did not work), and it is to be fixed the same day.

hubkit does the detecting, storing, refusing and reporting; the hub model does the recording and the rule writing, so the loop only adapts as far as the hub follows it.

## End of day (`hubkit eod`)

Writes `eod/eod_<day>.md` from the record, with no model: orders opened, closed, cancelled, held and open; chases per order; finished work; alerts; hook errors; and the learnings between the archive and the new (new today, repeated, addressed and holding, addressed but repeated, seen twice and not addressed).

## Lifecycle (`lifecycle.py`)

One table defines the states, transitions, required proof and clocks of the four things hubkit tracks: orders, routed work, requests and replies. `orders.py`, `routed.py`, `owed.py` and `replies.py` ask it (`lifecycle.check`) before they write, so a transition the table lacks (closing a cancelled order, `req done` twice, `replied` on a closed ask) is refused with a `NEXT:` hint. The table only names the clocks (`cfg:` a config.toml key, `intents:` an intents.toml key, `fn:` a function that learns the limit); the modules keep computing the minutes and the state strings on disk are unchanged. A transition that lists several proofs needs any one of them. `hubkit doctor` prints `lifecycle: ok` or the problems `lifecycle.audit` found in the state files. A test keeps the block below equal to `lifecycle.describe()`.

<!-- lifecycle:begin -->
```text
order
  state unconfirmed (live): clocks intents:intake.unconfirmed_ttl_min
  state open (live): clocks intents:kinds.*.first_action_min, intents:kinds.*.stall_min
  state held (live): clocks intents:intake.held_review_min
  state closed (final): clocks none
  state cancelled (final): clocks none
  state expired (final): clocks none
  new --open--> open
  new --open--> unconfirmed
  unconfirmed --confirm--> open
  unconfirmed --hold--> held [needs quote]
  open --hold--> held [needs quote]
  held --hold--> held [needs quote]
  held --resume--> open
  unconfirmed --cancel--> cancelled [needs quote]
  open --cancel--> cancelled [needs quote]
  held --cancel--> cancelled [needs quote]
  unconfirmed --expire--> expired [needs note]
  open --expire--> expired [needs note]
  held --expire--> expired [needs note]
  open --close--> closed [needs evidence]
  held --close--> closed [needs evidence]
routed
  state routed (live): clocks cfg:routed.stall_min, cfg:routed.review_stall_min
  state chased (live): clocks cfg:routed.stall_min, cfg:routed.review_stall_min
  state reported (live): clocks cfg:routed.stall_min, cfg:routed.review_overdue_min
  state answered (live): clocks none
  state closed (final): clocks none
  new --route--> routed
  routed --chase--> chased
  chased --chase--> chased
  reported --chase--> reported
  routed --report--> reported
  chased --report--> reported
  reported --report--> reported
  routed --answer--> answered
  chased --answer--> answered
  reported --answer--> answered
  routed --close--> closed [needs github or evidence or note]
  chased --close--> closed [needs github or evidence or note]
  reported --close--> closed [needs github or evidence or note]
  answered --close--> closed [needs github or evidence or note]
request
  state open (live): clocks fn:owed.limits
  state noted (live): clocks fn:owed.limits
  state chased (live): clocks fn:owed.limits
  state done (final): clocks none
  state dropped (final): clocks none
  new --add--> open
  open --note--> noted [needs note]
  noted --note--> noted [needs note]
  chased --note--> chased [needs note]
  open --chase--> chased
  noted --chase--> chased
  chased --chase--> chased
  open --done--> done [needs evidence]
  noted --done--> done [needs evidence]
  chased --done--> done [needs evidence]
  open --drop--> dropped [needs quote]
  noted --drop--> dropped [needs quote]
  chased --drop--> dropped [needs quote]
reply
  state open (live): clocks cfg:watcher.reply_owed_min, fn:replies.limit
  state due (live): clocks none
  state replied (final): clocks none
  new --ingest--> open
  open --settle--> due
  due --unsettle--> open
  open --replied--> replied [needs note]
  due --replied--> replied [needs note]
```
<!-- lifecycle:end -->

## Extension points

`<config dir>/ext/*.py` modules may define `status_lines(cfg) -> list[str]` (extra lines in the status block) and `tick_checks(cfg, live_orders) -> list[{"key", "msg"}]` (extra watchdog alerts). A broken extension is logged to `hook-errors.jsonl` and skipped.
