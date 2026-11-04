# Research: hub orchestration (2026-11-04)

Fetched and read in full: [A1], [A2], [A3]. Search snippets only (not opened): [S1], [S2], [S3].
Figures below come from the fetched pages. Treat snippet-only claims as unverified.

## Sources
- [A1] https://www.anthropic.com/engineering/multi-agent-research-system (read)
- [A2] https://www.anthropic.com/engineering/building-effective-agents (read)
- [A3] https://code.claude.com/docs/en/sub-agents (first 100k chars read)
- [S1] https://github.com/langchain-ai/langgraph-supervisor-py (snippet)
- [S2] https://fast.io/resources/openai-agents-sdk-vs-langgraph/ (snippet, vendor blog)
- [S3] https://www.developersdigest.tech/blog/multi-agent-orchestration-patterns (snippet)
- Not fetched: the Claude Code skills doc (https://code.claude.com/docs/en/skills).

## What applies to the hub

Delegation criteria
- [A3] Delegate side tasks that would flood the main context with logs, search results or file bodies. Keep in the main thread only what needs back-and-forth.
- [A1] Scale effort to the task: simple fact-finding = 1 agent, 3-10 tool calls. Early agents spawned 50 subagents for simple queries. Put an effort rule in the brief.
- [A1] Multi-agent costs about 15x the tokens of chat. Skip it for shared-context work, many dependencies, few parallel steps (most coding).
- [A2] Orchestrator-workers fits tasks whose subtasks cannot be predicted. Start simple, add structure only when it measurably helps.
- [S2] Handoffs (control moves) fit linear triage. Agents-as-tools (manager keeps control) fits the hub: it never hands the user over to a peer.

Brief quality
- [A1] Every brief carries: objective, output format, tools and sources to use, clear task boundaries. Vague briefs caused duplicated work and gaps.
- [A3] A subagent does not see the conversation. The brief is its only context.
- [S1] A supervisor that calls workers as tools keeps control of context. This matches the hub model.

Parallelism
- [A1] Launch independent workers in parallel, not in series. This cut research time by up to 90%.
- House cap (user rule): at most 4 at once, Haiku/Sonnet only.

Verification by a separate judge
- [A2] Evaluator-optimizer: one model produces, another evaluates against clear criteria. Ground truth comes from the environment (tool results), not from the agent's own claim.
- [A1] Judge the end state, not the path. Use an LLM judge plus human checks for edge cases.
- The hub never accepts "done" from the agent that did the work.

Context hygiene
- [A1] Save the plan to external memory before context fills. Spawn fresh agents with clean context and hand off carefully. Workers return lightweight references, not bulk output.
- [A3] Subagents return a summary only. Resume with SendMessage to keep their context. Explore and Plan agents cannot be resumed.

Stop conditions and human checkpoints
- [A2] Pause for human input at checkpoints or blockers. Add a hard stop (max iterations).
- [A1] Resume from the point of failure, not from scratch. Tell an agent when a tool is failing so it adapts.

Learning loops and memory
- [A1] Start evaluating at once with ~20 real cases. Prompt changes were the main lever. Trace decision patterns, not every transcript.
- [A1] Persist the plan. [A2] Test tools with real inputs and fix the mistakes the model makes (poka-yoke).
- Applied here: every user correction becomes a learnings entry tied to a gate check. A class seen twice is promoted into the gate. See SKILL.md section 8.
