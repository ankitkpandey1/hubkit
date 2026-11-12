---
name: hub-mep
description: Optional companion to the hub skill for users of the `mep` (miseenplace) task-list CLI. Use when the hub needs the user's open items, wants to put one batched decision to the user, watches the user's chat while away, or closes the day. Needs `mep` installed; without it, ignore this skill. Thin: mep's own skills own the verbs, this says only what the hub does with them.
---

# hub-mep

`mep` is the user's list, inbox and journal (PyPI package `miseenplace-cli`). The hub reads it and fills its queues. It never edits mep's data files, never writes into the mep data root, and calls only the CLI, with a timeout, as an argument list.

Each step below is a command and the result that means it worked. `mep status --json` and `mep watch` are mep's; check mep's own help for flags.

| Step | Command | Success | Otherwise |
|---|---|---|---|
| Is mep there | `command -v mep` | prints a path | nothing: set `[mep] enabled = false` in `~/.config/hubkit/config.toml` and stop using this skill |
| What is on the user | `mep status --json` | JSON with an `on_you` key | missing key or a timeout: report `UNVERIFIED` or `unreachable`, never "nothing pending" |
| Same, in the hub's view | `hubkit status --json` | JSON, exit 0, its `mep` entry says `ok` | `stale` or `UNVERIFIED`: re-run `mep status --json` |
| Watch the user's chat while away | background agent runs `mep watch --cursor <name>` | the agent exits with the line that woke it | exit with no line: re-arm with the same cursor |
| Classify an inbox line | `mep triage` | one classified item per line (the inbox-triage skill owns it) | |
| Put a decision to the user | `mep ask --recommend --default --by` | the ask is queued (one batched ask) | |
| Draft a message to a person | `mep draft` | a draft id | |
| Send it | the configured send command: the one `tools.send_regex` in `~/.config/hubkit/config.toml` matches (empty means `mep send`); only on the user's typed yes, hub gate G3 | `hubkit inbound ls` no longer lists the ask (hubkit marks it replied when the command names the message id or the person) | still listed: `hubkit inbound replied ID --note "<what was sent>"`, exit 0 |
| Daily close | the user's own end-of-day skill for the list, then `hubkit eod` | `hubkit eod` prints the review, exit 0 | |
| Lessons about hub behaviour | `learn.py add ...` today; `mep lessons` is the planned single store (field map: situation->when, right->do, wrong->not, evidence->evidence, seen count->votes) | | |

Rules
1. Before listing a mep item as pending on the user, re-read it now (hub section 5). Items older than the status build are `UNVERIFIED`.
2. A mep call that fails or times out is reported as `unreachable`, never as "nothing pending".
3. Orders (clocks, evidence) live in hubkit, not in mep: mep models the user's asks, hubkit models what the hub owes while an order is open.
4. The adapter reads the `on_you` key of `mep status --json` and checks it; a missing key shows as UNVERIFIED. Re-check with `hubkit status` after a mep upgrade.
5. Never paste mep output that contains tokens into state or reports; hubkit redacts what it stores, but report key names only.
6. No `mep`? Set `[mep] enabled = false` in `~/.config/hubkit/config.toml`; the hub then ignores this skill.

## Example

The user is away and says "tell me when anyone needs a review."

1. The hub launches a background agent: `mep watch --cursor hub-away`; Done-when: exits on a line that asks for a review; Output: that line only.
2. The agent exits with `review asked: PR 41`. The exit wakes the hub, which routes the review to the reviewer peer (hub gate G5 applies) and records an item with `hubkit work add --kind routed --owner reviewer --title "review PR 41" --pr https://github.com/acme/repo/pull/41`.
3. At the next report, `hubkit status` shows `on the user (mep): N`; the hub re-reads anything it lists as pending (rule 1).
