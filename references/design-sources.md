# Where the design parts come from

| Part | Source |
|---|---|
| Hub keeps control, workers are tools; never accept "done" from the author | research.md [A2] evaluator-optimizer, [S1] supervisor, "Verification by a separate judge" |
| Briefs carry objective, output, boundaries; agents see only the brief | [A1], [A3] |
| Effort scaling, agent cap, parallel launch | [A1]; the cap itself is a user rule, kept in the overlay |
| Hard stop and human checkpoints; resume from failure | [A2], [A1]; implemented as the stop-loss line on repeated chases and the order `hold` verb |
| Evaluate early on about 20 real cases; trace decision patterns | [A1]; the tests replay realistic prompts and transcript entries |
| Persist plan and state outside the context; fresh-context hand-off | [A1]; status file, orders and archive in the state dir, reloaded by the SessionStart block |
| Block size cap (5 learnings), 30-day expiry and 14-day probation of learnings | keeps post-compaction context small; probation so a fix is proven before it drops |
| Orders with clock and evidence, watchdog outside the hooks, hooks fail open and stay silent outside the hub | hook failures are invisible to the model, so the watchdog scans transcripts for them |
| Learnings promoted into rules, addressed only after a rule change | "Learning loops and memory" in research.md: a correction repeated without a rule change is not fixed |
| Human-only intake, fail-open hooks, stale-labelled external reads | prompt origin must be human, whole-text peer-marker scan, import failure exits 0, `mep` kept as last good answer when slow |
