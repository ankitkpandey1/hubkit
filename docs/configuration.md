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
| `reply_owed_min` | `60` | A `reply` item (from `hubkit inbound ingest`) open this long raises one alert. |
| `verify_stale_min` | `180` | An `active` or `priority` item not stamped with `hubkit work verify` for this long raises an alert. |
| `notify_user` | `true` | Use the platform default desktop notification for hub and peer alerts when `tools.notify_cmd` is empty. |

### `[stop_guard]`
| Key | Default | Meaning |
|---|---|---|
| `phrases` | `["waits for your go", "say go", "want me to", "shall i"]` | A final message ending in `?` or containing one of these, while no `ask` item exists, is blocked by the stop gate. |

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
| `review_overdue_min` | `10` | Reviews: hard OVERDUE this long after routing, whatever the owner said. |
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

### `[corrections]` (the learn loop)
| Key | Meaning |
|---|---|
| `patterns` | Regexes; a typed prompt matching one injects a `CORRECTION` line asking the hub to record it. |
| `repeat_similarity` | Word-set similarity (0 to 1) above which a prompt counts as repeating a recent one. Default `0.6`. |
| `repeat_window_min` | How far back a repeat is looked for. Default `30`. |

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

Add your own kinds the same way; the words are matched as whole words, case-insensitively.
