# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.4.1] - 2026-11-12

First release since 0.2.4; versions 0.3.0 and 0.4.0 were not published separately. This one holds both: the new trackers and checks, and the lifecycle table with the agent-friendly CLI.

### Added
- Threads (`threads.py`): every record may carry a `thread` (the root ask's id), set automatically (a `For: <ID>` line in a brief; the same PR; the only open ask or shared key words, never a guess between two; what a standing rule opens) or by hand (`hubkit thread link|unlink`). `hubkit mine` prints one entry per thread with its current step, whose court it is in and an indented line per member; `hubkit thread ls|show [--json]`; `mine --json` gains `threads`. When the last step under a root closes the tick alerts with the exact `req done` / `order close` command (hubkit never closes the root); closing a root with open steps warns. New `[threads]` keys.
- Declared check for work with no PR: a `Done-when:` line in a brief (`routed.done_when_markers`) is stored, the done-report nudge names it, `work done` on that item needs `--note` (exit 3 without), and a non-review brief with no PR and no such line gets a warning (`routed.require_done_when`, `"warn"` or `"off"`).
- Request intake: a question-form ask with a request verb ("can you delete the old branch?", "please restart X") opens a request (`[requests] polite_ask`; information asks such as "can you tell me why" stay untracked, `info_verbs`); a list of asks (numbered, bulleted, or after a colon) becomes one request with `parts` closed by `req done|drop ID --part N` (`max_parts`); the request keeps the full prompt (`text_chars`) and `req show ID` prints it; `req drop ID --not-a-request` teaches intake and a verb with `false_min` false intakes at `false_ratio` of its opens in `window_days` is muted (`req ls --learned`, `req relearn VERB`); `req add "text"` opens one by hand and counts a miss in `eod`. "fix" and "tidy" are now default request verbs. `req done ID` on a request with open parts is refused (exit 3).
- Agent-friendly CLI (`agentio.py`): `--json` on `order ls|status`, `work ls`, `req ls`, `mine`, `peers ls`, `inbound ls`, `find`, `digest`, `gate ls`, `standing ls`, `how ls`, `doctor` and the new `next` (one document on stdout, nothing else); `hubkit next` names the single most urgent action with its exact command; every error ends with `NEXT: <command>` (and a `{"error","next","code"}` document under `--json`); `--help` shows examples and the exit codes; README "For agents", `llms.txt`, a verb table in docs/commands.md, and the skills rewritten as command-then-expected-result steps.
- Exit codes are now stable: 0 ok, 2 bad usage, 3 refused (`order close` without evidence or in a state that forbids it, `work done` on a request), 4 not found (an unknown order, work, request, rule or playbook id). These were exit 2 before; scripts that tested for 2 on a refusal or an unknown id must accept 3 and 4. `order close`, `work done` and the like without an id are now usage errors (2) instead of "no order None".
- One lifecycle table (`lifecycle.py`) now defines the states, transitions, required proof and clocks of orders, routed work, requests and replies. The four trackers ask it before a write (new refusals: confirm, hold or resume an order in a state that cannot, a chase of a request or routed item in a final state), `hubkit doctor` audits the state files against it, and docs/architecture.md carries it.
- Standing rules (`hubkit standing ls|add|drop`): the user's instruction saved with their quote, an `approved` trigger (a counting non-author approval at the current head of a tracked PR, optionally per repo or PR) or an `inbound` trigger (a new ask, optionally per person and word), and an action. A firing, once per PR head or message, opens an order or an `active` work item and prints the exact NEXT (a merge carries `--match-head-commit <sha>`; red CI turns it into a hold). hubkit never runs the action. `--until` expires a rule. One status line counts active rules; `mine` lists fired ones not yet acted on. New `[standing]` keys.
- The user's own review debt (`[review_debt]`): a rate-limited sweep (`gh search prs --review-requested`) finds PRs they reviewed before whose head moved; shown under ON THE USER in `mine` and in the digest, one alert per PR per head, read only.
- End of day on a clock: `report.eod_time` ("HH:MM") makes the tick write the eod once for the day; `report.eod_check_min` alerts when the file is still missing or writing failed; `hubkit doctor` says whether the clock is on.
- Stale tracked items: `mine` and the digest mark an open item whose PRs are all merged or closed ("probably stale", with days since last activity); `[stale]` caps the gh reads. Warn only.
- A routed non-review item with no PR whose owner reported done is nudged (status block, tick alert, once per `routed.stall_min` in the prompt): check the result, then `hubkit work done ID --note ".."`.
- Adaptive Stop-gate strength: every gate reason has a check id and ends `[check: ID; wrong? hubkit gate false-alarm ID --why "..."]`; `hubkit gate ls|false-alarm|restore|set`; a check with `gate.false_alarm_min` false alarms at `gate.false_alarm_ratio` of its fires in `gate.window_days` downgrades itself to a next-prompt `hubkit WARNING`, a correction within `gate.restore_window_min` restores it, `gate.never_downgrade` (verdict, ci, merge-state) is exempt, and `hubkit doctor` names downgraded checks.
- Urgent orders held by asking: intents kind flag `no_questions` (hotfix, release in the example) and `[stop_gate] permission_words`; the Stop gate holds a question or "waiting for your go" while such an order is open and not held (`urgent-ask`).
- Report form: `report.max_lines` and `report.max_code_lines` (off by default) raise warn-only `report-length` and `report-code`.
- Stated rules: `[corrections] rule_patterns` and `rule_max_chars`; the prompt hook adds a `hubkit RULE` line with the `learn.py add --class standing-rule` command.
- Tool and routing guards (all warn-only, config-driven): `INLINE-EDIT` (the hub's own Edit/Write/NotebookEdit outside `guards.inline_ok_paths`, once per turn; the matcher now includes `Edit|Write|NotebookEdit`), `NO-ASK-REVIEW` (a review brief on a PR nobody asked about), `WRONG-ACCOUNT` (`[[accounts.rules]]`), `HOST-BUSY` (`guards.hosts`, shown in `hubkit peers ls`), and `REPORT-INCOMPLETE` (`[roles.<role>] report_must_include`).
- `tools.draft_regex` and `tools.discard_regex` replace the built-in draft words when set.
- Redaction replaces each secret in place with `[REDACTED]` and keeps the text around it; key names match whole segments (`api_token`, not `author` or `tokenizer`); a bare lowercase 40-hex commit id (or 32-hex id) is kept.
- Stop gate `hubkit UTC-TIME` (`[report]`): a time stated in UTC while the user's zone is another is held.
- Pre-tool warnings `NEEDS-ORDER` (`guards.needs_order`) and `UNORDERED-SCHEDULE` (CronCreate, ScheduleWakeup, cron/loop commands) when no typed prompt asks for them; the hook matcher now includes `CronCreate|ScheduleWakeup`.
- `learn.overlay_max_lines` caps `promote` with a list of rules to merge; `hubkit doctor` prints overlay lines; `hubkit REGRESSED <class>` leads the status block and `learn.py address` on a regressed class needs `--check`.
- CI workflow (`.github/workflows/ci.yml`).
- Reply loop: a `reply` item whose PRs are all merged, closed or reviewed at their head is "answer due now" (one alert, one gh read per PR per tick); `hubkit inbound ls` and `inbound replied`; a chat-send command naming the person or message id marks the ask replied (`tools.send_regex`); the age alert learns each person's own median wait (`watcher.reply_floor_min`, `reply_min_samples`); the Stop gate holds a merged/approved/reviewed claim while a reply is owed.
- Merge state: `routed.pr_info` reads `reviewDecision`, `mergeStateStatus` and review requests; `routed.court(info)`; the Stop gate holds "ready to merge" while GitHub shows changes requested, a review required, a blocked, behind or conflicting branch, or failing CI.
- Requests: the user's plain asks ("delete the stale branches") open a tracked record, closed only by `hubkit req done ID --evidence ..`; chases are counted and answered from the record, the Stop gate holds a done-claim while an old request has no note, and the overdue limit learns from the time-to-done of past requests. `hubkit mine` lists everything open by whose court it is in.
- Peer roster (`hubkit peers ls|set|forget`): role and free/busy learned from peers' messages, a per-peer record of jobs, median time and late count, a one-line roster in the status block, and routing warnings for a busy or wrong-role peer while a fitting one is free.
- Routed hook checks the named PR before more work goes out: already merged, closed, or reviewed at its head.
- `[roles.<role>] brief_must_include`: the routed hook names a required phrase a brief to that role lacks; `peers ls` counts corrections after briefs to each role.

### Changed
- `hubkit doctor` no longer warns that no scheduler entry runs `hubkit eod` while `report.eod_time` is set (the tick does it).
- A typed yes approves a post only when typed after the open `draft:` item was opened or last asked about again (no draft item: unchanged).
- `hubkit work done` on a `request` item is refused (exit 2); use `hubkit req done ID --evidence`.

## [0.2.4] - 2026-11-09

### Added
- `hubkit how ls|show|save|match`: playbooks (command -> how -> do). Matching steps are injected on a matching prompt; a NO PLAYBOOK flag and a Stop hold until `how save` runs.

### Fixed
- Text inside `<pasted_content>` is stripped before order, playbook and draft matching, so pasted text opens no order.

## [0.2.3] - 2026-11-06

### Fixed
- A job whose title leads with a change verb ("Fix #7 review findings") is not a review item, unless one of its sentences naming the PR leads with a review.
- The review hard limit (`routed.review_overdue_min`) is raised to the item's own stated `Estimate:` when that is longer.

## [0.2.2] - 2026-11-06

### Fixed
- A fix job whose change verb and PR number are in different sentences ("Fix the gaps. ... PR #7") is still recognised as a fix job, so its owner pushing to the PR is not counted as a stalled or moved head.

## [0.2.1] - 2026-11-06

### Added
- `hubkit find WORD [--since]` and, in `digest` and `eod`, the PRs the user's account opened plus what each peer session was asked (`hubcore/ledger.py`; read only on peers, prompts redacted, `gh` failures shown).

### Changed
- The unpushed-work Stop reason only counts files changed, and commits made, since the turn began; older dirt in the checkout no longer holds a report.

## [0.2.0] - 2026-11-06

### Added
- Commands: `hubkit digest [--since]`, `hubkit trust record|ls`, `hubkit inbound ingest`, `hubkit work verify`, `learn.py address --check CMD`.
- The warn-only PreToolUse hook (`hubkit hooks --merge-guard`, matcher `Bash|TaskStop|AskUserQuestion`) now also warns on posts without a typed yes, banned commands, repeated commands, a whole-suite `pytest`, `TaskStop` without a stop word, and a decision card whose first option is not marked (Recommended). The routed hook warns on a brief with no done-when, output or estimate line, on a brief that forbids the fix a blocker needs, and when more than `routed.max_agents` agents hold work.
- Stop gate reasons: a "tests pass" or "it works" claim in a turn that ran no command, a PR or issue number with no link, an approval while CI at the head is not green, built work not pushed, an ask asked 3 times with no answer, and an unanswered prompt not re-read before "done".
- Order close: evidence names in a kind's `sha_evidence` must hold a commit sha.
- Watcher: stale finished agents (`watcher.stale_agent_min`), replies owed to other people (`watcher.reply_owed_min`), and own work not verified for `watcher.verify_stale_min`.
- `doctor` warns on a hubkit hook registered twice in `settings.json` and on no scheduled `hubkit eod`.
- Learning loop: `address --check`, an overlay cap of 200 lines, expiry and prose-only lists in `review`, and an ineffective mark on a class that repeats after it was addressed.
- New tests (336 pass on Linux) and revert entries for each guard.

### Changed
- Correction patterns in `intents.example.toml` match insults aimed at the hub and no longer fire on one aimed at a tool.
- A routed item no longer closes on the peer's own "done"; it closes on an artifact (merged or closed PR, or a moved head with CI not failing). A merged or closed PR closes a review item.
- A one-line resume command may be given in the message as typed (`SKILL.md`).

## [0.1.0]

### Added
- Install like miseenplace: `uv tool install hubkit-cli` (published on PyPI as 0.1.0), `hubkit skills --install DIR` (hub, hub-mep, hubkit-setup), `hubkit hooks [--skills-dir DIR] [--merge-guard]` prints the settings.json hooks block, and a `hubkit-setup` skill.
- macOS and Windows support through one platform module (`hubcore/compat.py`); only Linux has been run so far.
- Blockers (`order block|result`), watcher fixes, peer-report closing, `asks ls`, and warn-only merge guard.
- Apache-2.0 license, `README.md` rewrite, `docs/` (architecture, configuration, commands, troubleshooting), `CONTRIBUTING.md`, `SECURITY.md`.
- `pyproject.toml` with package metadata and ruff, mypy and pytest settings.
- Tests for `learn.py promote-candidates`, `review` and the `address` error path, the entry-point import-failure path, and `doctor --check-clean`.

### Fixed
- Stop gate and review close: on a PR written by `user.github_login`, a COMMENT review at the head counts (verdict check, item close, queue advance). Other authors' PRs keep the non-author rule.
- A brief is a review ask only when its first action on the PR is a review; "fix the findings in his review of #N" is not. A head moved by an owner asked to fix the PR raises no re-check alert; a review item still does.
- PR numbers in examples, quotes and code spans of a brief are not tracked. Acknowledgements that name a PR are not tracked.
- "waiting on the verdict" and similar awaiting wording no longer trips the verdict check.
- `order open` never fires from a question, urgent word or not; the prompt hook says so and shows `hubkit order open` for doing it by hand.
- `order close` requires evidence for every PR named in the order and names the missing ones.
- A completed agent's task notification closes its routed item. Work ids are never reused after archive (`work.seq`).
- `doctor` flags a stale or missing `seen/cron` when the crontab runs hubkit (`watchdog.cron_stale_min`).

### Changed
- `mep: UNVERIFIED` now says that the optional `mep` CLI was not found and how to turn the check off (`[mep] enabled = false`).
- `doctor --check-clean` no longer scans `.git/` and also scans `.yml` and `.yaml` files.
- Docstrings and type annotations on public functions; `SKILL.md` and `skills/` reworded for clarity.
- The learnings file preamble says promoted rules go to the overlay.

### Removed
- The unused `tools.mep_cursor` config key.

## [0.1.0] - 2026-11-04

Initial private version: order clock, routed-work tracking, watchdog, stop gate, status block, end-of-day review and the learn loop.
