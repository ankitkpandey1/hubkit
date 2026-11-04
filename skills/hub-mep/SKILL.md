---
name: hub-mep
description: How the hub session uses mep (miseenplace) for the user's own list, asks, inbox and journal. Use when the hub needs the user's open items, wants to put a decision to the user, watches the user's chat, or closes the day. Thin: mep's own skills own the verbs; this says only what the hub does with them.
---

# hub-mep

mep is the user's list, inbox and journal. The hub reads it and fills its queues; it never edits mep's data files, never writes into the mep data root (it is git-synced and has reserved names), and calls only the CLI, with a timeout, as an argument list.

| Hub need | mep verb | Owner of the loop |
|---|---|---|
| Watch the user's chat while away | `mep watch --cursor <tools.mep_cursor>` run by a background agent; the exit wakes the hub | skill `inbox-watch` owns the loop; the hub owns the cursor name and what each line triggers |
| Classify an inbox line | `mep triage` | skill `inbox-triage` |
| What is on the user, waiting on others | `mep status --json` (also shown in the hubkit block, with an UNVERIFIED label when mep is slow or absent) | skill `journal`; hub reads only |
| A decision the user must make | `mep ask --recommend --default --by` | mep owns the queue; the hub fills it, one batched ask |
| A message to a person | `mep draft`, then `mep send` only on a typed yes (hub G3) | the hub's messaging rules (overlay) |
| Replies to review asks | the review-reply skill named in the overlay | unchanged |
| Daily close | skill `eod-review` for the user's list; `hubkit eod` for the hub's own orders and learnings | both, in that order |
| Lessons about hub behaviour | `learn.py` today; `mep lessons` is the planned single store (field map: situation->when, right->do, wrong->not, evidence->evidence, seen count->votes) | hub, until the migration is approved |

Rules
1. Before listing a mep item as pending on the user, re-read it now (hub section 5). Items older than the status build are `UNVERIFIED`.
2. A mep call that fails or exceeds its timeout is reported as `unreachable`, never as "nothing pending".
3. Orders (time-of-day clocks, evidence) live in hubkit, not in mep: mep models the user's asks, hubkit models what the hub owes while an order is open.
4. Mep schema: the adapter uses the `on_you` key of `mep status --json` and checks it; a missing key shows as UNVERIFIED. Re-check after a mep upgrade with `hubkit status`.
5. Secrets: never paste mep output containing tokens into state or reports; hubkit redacts what it stores, but report key names only.
