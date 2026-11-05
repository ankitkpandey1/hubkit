---
name: hub-mep
description: Optional companion to the hub skill for users of the `mep` (miseenplace) task-list CLI. Use when the hub needs the user's open items, wants to put one batched decision to the user, watches the user's chat while away, or closes the day. Needs `mep` installed; without it, ignore this skill. Thin: mep's own skills own the verbs, this says only what the hub does with them.
---

# hub-mep

`mep` is the user's list, inbox and journal (PyPI package `miseenplace-cli`). The hub reads it and fills its queues. It never edits mep's data files, never writes into the mep data root, and calls only the CLI, with a timeout, as an argument list.

| Hub need | mep verb | Who owns the loop |
|---|---|---|
| Watch the user's chat while away | `mep watch --cursor <name>`, run by a background agent; its exit wakes the hub | the inbox-watch skill; the hub picks the cursor name and decides what each line triggers |
| Classify an inbox line | `mep triage` | the inbox-triage skill |
| What is on the user, what waits on others | `mep status --json` (also in the hubkit block, labelled UNVERIFIED when mep is slow or absent) | the journal skill; the hub only reads |
| A decision the user must make | `mep ask --recommend --default --by` | mep owns the queue; the hub fills it with one batched ask |
| A message to a person | `mep draft`, then `mep send` only on a typed yes (hub gate G3) | the hub's messaging rules (overlay) |
| Replies to review asks | the review-reply skill named in the overlay | that skill |
| Daily close | the user's own end-of-day skill for the list; `hubkit eod` for the hub's orders and learnings | both, in that order |
| Lessons about hub behaviour | `learn.py` today; `mep lessons` is the planned single store (field map: situation->when, right->do, wrong->not, evidence->evidence, seen count->votes) | the hub, until the migration is approved |

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
