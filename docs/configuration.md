# Configuration reference

All user-specific values live in `~/.config/hubkit/` (or `$XDG_CONFIG_HOME/hubkit/`). Nothing personal belongs in the checkout. Every key is optional; a missing key uses the default in `scripts/hubcore/config.py` (`DEFAULTS`). Templates: [`config.example.toml`](../config.example.toml) and [`intents.example.toml`](../intents.example.toml).

## Files and environment

| What | Where | Override |
|---|---|---|
| Settings | `<config dir>/config.toml` | `$HUBKIT_CONFIG` (full path) |
| Intents | `<config dir>/intents.toml`, else the `intents.example.toml` shipped in the checkout | `paths.intents` |
| Overlay (your rules) | `<config dir>/overlay.md` | `paths.overlay` |
| Extensions | `<config dir>/ext/*.py` | `paths.extensions_dir` |
| State | `$XDG_STATE_HOME/hubkit` (default `~/.local/state/hubkit`) | `$HUBKIT_STATE`, `paths.state_dir` |
| Learnings | `<state dir>/learnings.md` | `paths.learnings` |

Other environment variables: `HUBKIT_SESSIONS` (directory of Claude Code session files, overrides `paths.sessions_dir`), `HUBKIT_DISABLE` (any value: every hook is a no-op), `HUBKIT_DIE_AFTER` (test hook for crash recovery; do not set).

## config.toml

### `[user]`
| Key | Default | Meaning |
|---|---|---|
| `name` | `"the user"` | How the status block names you. |
| `timezone` | `""` (system zone) | IANA name, for example `Europe/Berlin`. Times are stored as epoch and shown in this zone; days roll over in it. |
| `github_login` | `""` | Your GitHub login. On a PR you wrote, the reviewer peer posts from your account and GitHub allows only a COMMENT there, so a COMMENT review with a body (not an empty-body inline-thread reply) at the PR head counts as the review (verdict check, item close, queue advance). Empty keeps the non-author rule everywhere. |

### `[hub]`
| Key | Default | Meaning |
|---|---|---|
| `name` | `"hub"` | Names the hub in the status block and is the session name matched when the SessionStart hook re-binds a resumed hub. |

### `[paths]`
| Key | Default | Meaning |
|---|---|---|
| `state_dir` | `""` | State directory. Must not be inside a git repository or the skill directory. |
| `intents` | `""` | Path of `intents.toml`. |
| `overlay` | `""` | Path of `overlay.md`. |
| `learnings` | `""` | Path of the learnings file. |
| `sessions_dir` | `~/.claude/sessions` | Where Claude Code writes one `<pid>.json` per running session; used to find and verify the hub. |
| `transcripts_glob` | `~/.claude/projects/*/*.jsonl` | Transcripts scanned for failures of hubkit's own hooks and for heard-from signals. |
| `extensions_dir` | `""` | Directory of extension modules. |

### `[tools]`
| Key | Default | Meaning |
|---|---|---|
| `mep` | `"mep"` | Name or path of the optional `mep` CLI. |
| `gh` | `"gh"` | Name or path of the GitHub CLI. |
| `send_regex` | `""` | Regex of your chat-send command in a Bash call (PreToolUse). A match closes `draft:` items and marks reply items for the person or message id it names as replied. Empty means `mep send`. |
| `draft_regex` | `""` | Regex of a typed ask for a draft; a match opens a `draft:` work item. Empty means the word `draft`. A typed yes approves a post only when typed after that item was opened or last asked about again. |
| `discard_regex` | `""` | Regex of a typed discard of the draft (closes the item). Empty means `discard/drop/scrap/forget ... draft`. |
| `notify_cmd` | `""` | Command run with the alert message as its last argument (for example `notify-send -u critical -a hubkit`). Empty means no desktop notification. |

### `[mep]`
| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Ask `mep status --json` for the items on the user. Set `false` if you do not use mep. When enabled and `mep` is missing, the status block shows `mep: UNVERIFIED (...)`. |
| `timeout_s` | `30` | Seconds before a slow `mep` call is abandoned (the last good answer is kept, labelled stale). |
| `data_dir` | unset | Exported as `MISEENPLACE_DIR` for the call. |

### `[watchdog]`
| Key | Default | Meaning |
|---|---|---|
| `transcript_scan_hours` | `24` | How far back to scan transcripts for hubkit hook failures. |
| `cron_stale_min` | `15` | `doctor` flags `seen/cron` as stale when the crontab runs hubkit and the file is older than this. |

### `[requests]`

The user's plain asks, tracked until `hubkit req done`. `stall_min` (60) and `overdue_min` (120) apply until `[slow] min_samples` requests were finished; then the stall limit is the median time-to-done and the overdue limit is `overdue_factor` (2.0) times it, never under `floor_min` (10; overdue never under twice that). `similarity` (0.6) is the word overlap that makes a new ask a repeat (a chase) of an open one; `max_open` (20); `title_chars` (120); `block_lines` (5, open requests listed in the status block). The words are in intents.toml `[requests]`: `verbs` (a statement starting with one opens a request), `min_words` (for statements that only hold an `[intake] imperative` word), `chase` (regex; default is `[intake] chase`), `polite_ask` (regex with a `verb` group: a question-form or "please X" ask such as "can you delete the old branch?" opens a request when that verb is a request verb or an `[intake] imperative` word) and `info_verbs` (tell, explain, show, say, describe, clarify, confirm, remind, think, see, check if: a polite ask starting with one opens nothing).

More config.toml keys: `text_chars` (600, the full prompt kept on the request), `max_parts` (8; a longer list stays one request without parts), and the intake learning: `false_min` (3) false intakes (`req drop ID --not-a-request`) that are at least `false_ratio` (0.5) of a verb's opens in the last `window_days` (14) mute that verb (`req ls --learned` lists them, `req relearn VERB` restores one).

### `[slow]`

Estimates and the slowness check (hub-internal, see architecture).

| Key | Default | Meaning |
|---|---|---|
| `min_samples` | `3` | Archived samples needed before the usual time replaces the stall limit. |
| `order_default_min` | `30` | Default order estimate while there is no history. |
| `floor_min` | `2` | A job is never SLOW under this many minutes. |
| `tool_min` | `2` | A tool running this long is named in the why. |
| `scan_kb` | `2048` | Tail of a transcript read for the why (tool names and queue operations only). |

### `[watcher]`

| Key | Default | Meaning |
|---|---|---|
| `stuck_min` | `30` | Hub session `busy` this long with live jobs raises `hub:stuck`. |
| `unhandled_min` | `10` | An alert this old with no hub heartbeat since raises `hub:unhandled`. |
| `waiting_min` | `3` | A peer waiting on a prompt this long raises `peer:<name>:waiting`. |
| `active_s` | `120` | A busy holder whose transcript grew this recently is working, not stalled. |
| `watch_grace_min` | `5` | Live work with no running `hubkit watch` this long raises `watch:dead`. |
| `stale_agent_min` | `30` | A background agent that reported finished this long ago while its item is still listed raises `agent:<owner>:stale`. |
| `reply_owed_min` | `60` | A `reply` item (from `hubkit inbound ingest`) open this long raises one alert. It is the ceiling for a person with enough history and the limit for everyone else. |
| `reply_floor_min` | `10` | The learned per-person limit (their median time to reply) is never lower than this. |
| `reply_min_samples` | `3` | Closed reply items needed for a person before their own median replaces `reply_owed_min`. |
| `verify_stale_min` | `180` | An `active` or `priority` item not stamped with `hubkit work verify` for this long raises an alert. |
| `notify_user` | `true` | Use the platform default desktop notification for hub and peer alerts when `tools.notify_cmd` is empty. |

### `[stop_guard]`
| Key | Default | Meaning |
|---|---|---|
| `phrases` | `["waits for your go", "say go", "want me to", "shall i"]` | A final message ending in `?` or containing one of these, while no `ask` item exists, is blocked by the stop gate. |

### `[report]`
| Key | Default | Meaning |
|---|---|---|
| `zone_check` | `true` | Hold a final message that states a time in UTC while `user.timezone` is another zone. |
| `utc_markers` | `["UTC", "GMT"]` | Words that mark a clock time as UTC (`14:32 UTC`). ISO `Z` and `+00:00` forms are always checked. |
| `max_lines` | `0` (off) | A final message with more non-blank lines than this raises the `report-length` check. Warn only by default; a one-line message is exempt. |
| `max_code_lines` | `0` (off) | A fenced code block with more non-blank lines than this raises the `report-code` check (warn only by default). |
| `eod_time` | `""` (off) | `"HH:MM"` in `user.timezone`. After it the tick writes the day's end-of-day review once (unless the file exists); `hubkit doctor` says whether the clock is on. |
| `eod_check_min` | `30` | One alert when there is still no eod file this long after `eod_time`, or at once when writing it failed. |

### `[standing]` (standing rules, `hubkit standing`)
| Key | Default | Meaning |
|---|---|---|
| `approved_cmd` | `"gh pr merge {pr} --squash --match-head-commit {sha}"` | The NEXT command of an `approved` rule whose action says merge (`{pr}`, `{sha}`, `{repo}`). A rule's own `--cmd` wins. |
| `estimate_min` | `10` | Minutes a work item opened by a rule is expected to take (when the action names no order kind). |
| `gh_timeout_s` | `10` | Timeout of each gh read. |
| `max_reads` | `20` | Tracked PRs read per tick for `approved` rules. |

### `[review_debt]` (the user's own re-reviews)
| Key | Default | Meaning |
|---|---|---|
| `enabled` | `"auto"` | `"auto"` is on only when `user.github_login` is set; `true` / `false` force it. |
| `every_min` | `30` | The sweep (`gh search prs --review-requested <login> --state open`) runs at most this often, inside the tick. |
| `gh_timeout_s` | `15` | Timeout of each gh call. |
| `max_prs` | `30` | PRs fetched by the search. |
| `max_reads` | `10` | PRs read per sweep to compare the user's last review commit with the head. |

### `[stale]` (open items whose PR is merged or closed)
| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Mark such items "probably stale" in `hubkit mine` and the digest. |
| `max_reads` | `8` | gh reads per run. |
| `gh_timeout_s` | `5` | Timeout of each read. |

### `[gate]`
| Key | Default | Meaning |
|---|---|---|
| `false_alarm_min` | `3` | A Stop-gate check with at least this many false alarms (`hubkit gate false-alarm ID --why ..`) ... |
| `false_alarm_ratio` | `0.3` | ... that are at least this share of its fires ... |
| `window_days` | `14` | ... in this many days downgrades itself from hold to warn. A warn-mode check shows as a `hubkit WARNING` line in the next prompt and no longer blocks. |
| `restore_window_min` | `30` | A user correction within this many minutes after a downgraded check warned puts it back to hold and resets its count. |
| `never_downgrade` | `["verdict", "ci", "merge-state"]` | Checks that guard against relaying false verdicts; they never downgrade themselves (`gate set` can still change them by hand). |

### `[learn]`
| Key | Default | Meaning |
|---|---|---|
| `overlay_max_lines` | `200` | `learn.py promote` is refused at or over this many overlay lines; `hubkit doctor` prints the count. |

### `[guards]` (warn-only pre-tool checks)
| Key | Default | Meaning |
|---|---|---|
| `needs_order` | `[]` | Regexes of Bash commands that create something only on the user's ask (a ticket, an issue). |
| `needs_order_words` | `["ticket", "issue", "jira", "file a", "open a", "create a"]` | A recent typed prompt with one of these counts as the ask. |
| `schedule_tools` | `["CronCreate", "ScheduleWakeup"]` | Tools that set up recurring work. |
| `schedule_regexes` | crontab, `while true ... sleep`, `watch -n` | Bash commands that set up recurring work. |
| `schedule_words` | `["schedule", "loop", "watch", "every", ...]` | A recent typed prompt with one of these counts as the ask. |
| `window_min` | `60` | How far back typed prompts count. |
| `inline_edits` | `"warn"` | `"warn"` or `"off"`. The hub's own `Edit`/`Write`/`NotebookEdit` outside `inline_ok_paths` is warned once per turn (a turn starts at the user's last typed prompt). |
| `inline_ok_paths` | `["/tmp/*", "*.md"]` | Globs on the full path the hub may edit itself: scratch files and notes. Its own state and config directories and the temp directory are always allowed. |
| `hosts` | `[]` | Host names that hold one agent at a time. A routed brief naming one records it on the item; a brief to another owner naming a host a live routed item holds is warned. `hubkit peers ls` shows hosts in use. |
| `review_ask_window_min` | `240` | A review brief on a PR that no open order, reply, request, work item or typed prompt in this window names is warned (`NO-ASK-REVIEW`). |

### `[accounts]` (warn-only)
| Key | Default | Meaning |
|---|---|---|
| `rules` | `[]` | Array of tables `[[accounts.rules]]` with `match` (regex on a Bash command), `require` (regex that must also be present, for example the flag that picks the account) and `hint` (the NEXT action). A command matching `match` without `require` is warned: `WRONG-ACCOUNT`. |

### `[private]`
| Key | Default | Meaning |
|---|---|---|
| `terms` | `[]` | Words that must never appear in the checkout's own files. `hubkit doctor --check-clean` lists the files that contain one (never the match). Keep this file on your machine. |

### `[routed]` (also readable from `intents.toml`; intents win)
| Key | Default | Meaning |
|---|---|---|
| `stall_min` | `15` | Silence from the owner this long is STALLED; the ladder steps at multiples of it. |
| `review_words` | `["review"]` | A message naming a PR and one of these is a review item. |
| `review_stall_min` | `5` | Reviews: chase after this. |
| `review_overdue_min` | `10` | Reviews: hard OVERDUE this long after routing, whatever the owner said. A review brief with its own `Estimate: Nm` line (not a defaulted one) gets the larger of this and N. |
| `skip_owners` | `["inbox"]` | Owners that are never tracked. |
| `max_agents` | `4` | More distinct agents than this holding live routed work makes the routed hook warn. |
| `min_chars` | `200` | A message without a PR is work only from this length. |
| `gh_timeout_s` | `10` | Timeout for one `gh` read. |
| `queued_wait_min` | `60` | Work queued "after X" gets no review clock until this long after the ask (or until a review shows). |
| `reply_window_min` | `60` | A hub reply to a peer who wrote this recently is not an ask. |
| `repo` | `""` | `owner/name`; a bare `#N` resolves against it so `gh` can read the PR. |
| `repo_aliases` | `{}` | Table of alias to `owner/name`. `<alias> 123`, `<alias>#123` and `owner/name#123` name the PR in that repo instead of the default `repo`. |
| `owner_logins` | `{}` | Map of owner name to GitHub login, for peers who author PRs. |
| `reviewer_logins` | `[]` | Logins whose review counts. Empty means any non-author review. |
| `skip_briefs` | `["mep watch", "inbox watch"]` | Briefs containing one of these are standing watchers, never routed work. |
| `activity_globs` | two Claude Code paths | Files whose growth shows a background agent is alive. `{id}` is the agent id. |
| `done_when_markers` | `["Done-when:", "Done when:"]` | A brief line starting with one of these declares what shows a no-PR job is done; stored on the item. |
| `require_done_when` | `"warn"` | `"warn"`: a non-review brief that names no PR and has no such line gets a warning in the hook output. `"off"`: none. |

### `[threads]` (linking work to the ask it serves)

| Key | Default | Meaning |
|---|---|---|
| `min_shared` | `2` | Key words a routed brief must share with an open ask to link under it. |
| `root_share` | `0.5` | The share of the ask's own key words that must also be in the brief. |

### `[roster]` (learned peer roster)
| Key | Default | Meaning |
|---|---|---|
| `stale_min` | `120` | A free/busy state learned longer ago than this is shown as stale and is not trusted. |
| `correction_window_min` | `10` | A correction by the user this soon after a brief to a role is counted against that role (`hubkit peers ls`). |
| `reviewer_words` | `["reviewer", "code-review only", ...]` | Words in the opening of a peer's message that make it a reviewer. |
| `worker_words` | `["worker", "no role assigned"]` | Same, for a free worker. |
| `busy_words` / `free_words` | see `config.example.toml` | Words in the opening of a peer's message that say it is busy or free (idle). "not busy" counts as free. |

### `[roles.<role>]`
| Key | Default | Meaning |
|---|---|---|
| `brief_must_include` | `[]` | Phrases or regexes (case-insensitive) a brief to a peer of this role must contain; a review ask counts as a brief to `reviewer`. The routed hook names each missing one. Example: `[roles.reviewer] brief_must_include = ["code-review skill"]`. |
| `report_must_include` | `[]` | Phrases or regexes a review report from a peer of this role (or any reviewer) must show. When the owner of a review item reports a result lacking one, a `REPORT-INCOMPLETE` line (prompt and tick alert) names what is missing and says to ask for it before relaying the verdict. Warn only: the review still closes on GitHub evidence. |

## intents.toml

The first kind whose words match wins, so put the narrower kind first (`hotfix` before `release`).

### `[intake]`
| Key | Meaning |
|---|---|
| `imperative` | An order opens as `open` only if one of these words is present; otherwise it is `unconfirmed`. |
| `hold_words` | Explicit holds only. Bare "don't", "wait", "stop" are not holds. |
| `urgent_words` | A hold is ignored, and a held order restarts, when one is in the prompt. |
| `stop_loss` | This many chases on one order print a stop-loss line. |
| `not_order_phrases` | Phrases that contain an order word but are not orders ("release notes"). |
| `question_start` | Regex; an order word in a question is never an order, urgent words or not (the hook then says so and shows `hubkit order open` for opening one by hand). |
| `unconfirmed_ttl_min` | `tick` expires unconfirmed orders after this long. |
| `blocker_fix_min` | A blocker with no fix, or a fix with no result after this many minutes (default `3`), alerts and holds the Stop gate. |
| `held_review_min` | A hold this long raises an alert. |
| `cancel_words`, `resume_words` | Words for the cancel and resume verbs. |
| `verb_max_chars` | Prompts longer than this never hold, cancel, resume or count as chases. |
| `chase` | Regex; a short prompt matching it while an order is open is a chase. |

### `[stop_gate]`
| Key | Meaning |
|---|---|
| `blocked_words` | A final message with one of these, while an order is open and none has a fix on record, cannot end the turn. |
| `unchecked_words` | "A person's call" style claims held when the turn made no lookup. |
| `verified_words` | "Tests pass", "it works", "is deployed" style claims held when the turn ran no Bash command. |
| `full_links` | Default `true`. Set `false` to stop holding messages that name a PR or issue number with no link. |
| `permission_words` | Phrases (`shall i`, `should i`, `do you want me to`, `waiting for your go`, `let me know if`, `want me to proceed`, ...) that, in a final message while a `no_questions` order is open and not held, hold the turn (`urgent-ask`). |

### `[corrections]` (the learn loop)
| Key | Meaning |
|---|---|
| `patterns` | Regexes; a typed prompt matching one injects a `CORRECTION` line asking the hub to record it. |
| `repeat_similarity` | Word-set similarity (0 to 1) above which a prompt counts as repeating a recent one. Default `0.6`. |
| `repeat_window_min` | How far back a repeat is looked for. Default `30`. |
| `rule_patterns` | Regexes for a stated standing rule ("from now on", "always/never <verb> ..", "remember to", "going forward", "save this to memory"). A short typed statement that is not a question and matches injects a `hubkit RULE` line with the `learn.py add --class standing-rule` command. |
| `rule_max_chars` | Longer prompts never count as a stated rule. Default `300`. |

### `[kinds.<name>]`
| Key | Meaning |
|---|---|
| `words` | Words that open an order of this kind. |
| `first_action_min` | No `order move` within this many minutes raises an alert. |
| `stall_min` | No movement for this long raises an alert. |
| `dispatch` | Text printed when the order opens. |
| `done_when` | Evidence names `order close` requires (`--evidence name=value`). |
| `sha_evidence` | Evidence names that must hold a commit sha (the example merge kind uses `["merged"]`). |
| `movement_files` | Globs; a file touched after the order opened counts as movement. |
| `no_questions` | Default `false`; the example hotfix and release kinds set `true`. While such an order is open and not held, a final message that ends in a question or holds a `permission_words` phrase is held (`urgent-ask`) unless the order has a blocker recorded as waiting on the user. An `ask` work item does not lift it. |

Add your own kinds the same way; the words are matched as whole words, case-insensitively.
