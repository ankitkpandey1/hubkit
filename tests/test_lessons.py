"""Checks born from repeated corrections: asks ls, bare frustration, priority order, decisions, stale agents,
unchecked claims, forbidding briefs and the merge guard. Real files and processes."""

import json
import time

from test_hubkit import HOTFIX, LONG, PR1, iso, send, stop

# ---- asks ls ----


def prompts(env, *texts):
    for t in texts:
        assert env.prompt(t).returncode == 0


def test_asks_ls_prints_his_recent_words_oldest_first(env):
    env.bind()
    prompts(env, "first: please list the open reviews", "second: and tell me which are blocked")
    out = env.run("asks", "ls").stdout.splitlines()
    assert len(out) == 2 and "first: please list" in out[0] and "second: and tell me" in out[1]


def test_asks_ls_keeps_the_oldest_prompt_within_the_span_after_many_prompts(env):
    env.bind()
    prompts(env, "the very first instruction of the day")
    prompts(env, *[f"unrelated chatter number {i} about nothing" for i in range(230)])
    out = env.run("asks", "ls", "--since", "12h").stdout
    assert "the very first instruction" in out


def test_asks_ls_rejects_a_bad_span_and_honours_a_short_one(env):
    env.bind()
    prompts(env, "an old instruction to remember")
    p = env.state / "recent-prompts.jsonl"
    p.write_text(json.dumps({"text": "an old instruction to remember", "ts": time.time() - 3 * 3600}) + "\n")
    assert "old instruction" not in env.run("asks", "ls", "--since", "1h").stdout
    assert "old instruction" in env.run("asks", "ls", "--since", "5h").stdout
    r = env.run("asks", "ls", "--since", "soon")
    assert r.returncode == 2 and "--since takes" in r.stderr


def test_a_done_turn_with_an_unread_newer_prompt_is_blocked_until_asks_ls_runs(env):
    env.bind()
    prompts(env, "do three things: fix the log, tidy the docs, and reply to the thread")
    blocked = stop(env, last_assistant_message="All done, shipped.")
    assert "have not re-read the user's words" in blocked["reason"] and "hubkit asks ls" in blocked["reason"]
    env.run("asks", "ls")
    assert stop(env, last_assistant_message="All done, shipped.") is None
    prompts(env, "one more thing: also bump the version")
    assert "have not re-read the user's words" in stop(env, last_assistant_message="Finished.")["reason"]


def test_a_turn_that_does_not_say_done_is_not_held_for_the_asks(env):
    env.bind()
    prompts(env, "do three things: fix the log, tidy the docs, and reply to the thread")
    assert stop(env, last_assistant_message="Working on the log fix now.") is None


# ---- bare frustration ----


def test_bare_frustration_is_a_correction(env):
    env.bind()
    for text in ("u useless", "wtf is this", "wrong!", "nope"):
        out = env.prompt(text).stdout
        assert "CORRECTION (correction phrase)" in out, text
    assert "CORRECTION" not in env.prompt("the noon build looks fine").stdout


# ---- priority first, decisions in the block ----


def test_priority_work_is_listed_before_active_work_and_flagged_when_active_runs_ahead(env):
    env.bind()
    env.run("work", "add", "--kind", "active", "--title", "tidy the docs")
    env.run("work", "add", "--kind", "priority", "--title", "fix the billing leak")
    block = env.run("status").stdout
    assert block.index("priority: ") < block.index("active: ")
    assert "AHEAD: active work is running while this waits" in block
    env.run("work", "done", "W1", "--note", "tidied")
    assert "AHEAD" not in env.run("status").stdout


def test_the_last_decisions_appear_in_the_status_block(env):
    env.bind()
    dec = env.root / "decisions.md"
    dec.write_text("\n".join(f"- ruling {i}: keep it simple" for i in range(8)) + "\n")
    env.cfg.write_text(env.cfg.read_text() + f'[think]\ndecisions="{dec}"\n')
    block = env.run("status").stdout
    assert "recent decisions (decisions.md):" in block and "ruling 7" in block and "ruling 3" in block and "ruling 2" not in block


# ---- a finished agent that is still listed ----


def test_a_finished_review_agent_still_listed_raises_one_alert_and_a_fresh_one_does_not(env):
    env.bind()
    base = {"hook_event_name": "PostToolUse", "session_id": "sess-hub", "tool_name": "Agent", "tool_use_id": "toolu_S1"}
    env.hook("routed", {**base, "tool_input": {"description": "review it", "prompt": f"Full gated review of {PR1}. Output: a verdict", "run_in_background": True},
                        "tool_response": {"agentId": "agent9", "status": "async_launched"}})
    p = env.state / "work.json"
    items = json.loads(p.read_text())
    items[0]["reported"] = time.time() - 5 * 60
    p.write_text(json.dumps(items))
    assert "still listed" not in env.run("tick", "--no-notify").stdout
    items[0]["reported"] = time.time() - 45 * 60
    p.write_text(json.dumps(items))
    out = env.run("tick", "--no-notify").stdout
    assert out.count("agent agent9 finished 45 min ago") == 1


# ---- unchecked claims ----


def transcript(env, tools):
    lines = [json.dumps({"type": "user", "message": {"content": "where did the file go?"}, "timestamp": iso(time.time() - 60)})]
    for name in tools:
        lines.append(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": {}}]}}))
        lines.append(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}))
    tr = env.transcripts / "sess-hub.jsonl"
    tr.write_text("\n".join(lines) + "\n")
    return str(tr)


def test_a_gone_claim_with_no_lookup_this_turn_is_held_and_a_checked_one_passes(env):
    env.bind()
    msg = "The log file is gone, the run never wrote it."
    held = stop(env, last_assistant_message=msg, transcript_path=transcript(env, []))
    assert "claims" in held["reason"] and "read nothing" in held["reason"]
    assert stop(env, last_assistant_message=msg, transcript_path=transcript(env, ["Grep", "Read"])) is None
    assert stop(env, last_assistant_message="Nothing is lost.", transcript_path=transcript(env, [])) is None


def test_relaying_a_persons_call_unchecked_is_held(env):
    env.bind()
    held = stop(env, last_assistant_message="The agent says this is a person's call, so I left it.", transcript_path=transcript(env, []))
    assert "read nothing" in held["reason"]


# ---- a brief must not forbid the fix ----


def block_on_smoke(env):
    env.bind()
    env.prompt(HOTFIX)
    (oid,) = [r["id"] for r in env.rows("orders.jsonl") if r["op"] == "open"]
    env.run("order", "block", oid, "--on", "smoke has no host", "--fix", "provision a smoke host", "--holder", "peer-b")
    return oid


def test_a_brief_that_forbids_the_step_an_order_is_blocked_on_is_warned(env):
    oid = block_on_smoke(env)
    r = send(env, "peer-a", "Run the checks. Output: results. " + LONG + "\nDo not provision a host; use only the existing ones.", "checks")
    assert "WARNING" in r.stdout and oid in r.stdout and "must not forbid the one action" in r.stdout


def test_a_brief_that_forbids_something_else_is_not_warned(env):
    block_on_smoke(env)
    r = send(env, "peer-a", "Run the checks. Output: results. " + LONG + "\nDo not touch the billing tables.", "checks")
    assert "must not forbid" not in r.stdout


# ---- merge guard ----


def pretool(env, command):
    r = env.hook("pretool", {"hook_event_name": "PreToolUse", "session_id": "sess-hub", "tool_name": "Bash", "tool_input": {"command": command}})
    assert r.returncode == 0
    return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] if r.stdout.strip() else ""


def test_an_unasked_merge_is_warned_and_never_denied(env):
    env.bind()
    out = env.hook("pretool", {"hook_event_name": "PreToolUse", "session_id": "sess-hub", "tool_name": "Bash",
                               "tool_input": {"command": "gh pr merge 7501 --squash"}})
    assert out.returncode == 0 and "permissionDecision" not in out.stdout
    assert "merging #7501 with no live order" in pretool(env, "gh pr merge 7501 --squash")
    assert "merging a PR" in pretool(env, "gh pr merge --auto")
    assert pretool(env, "gh pr view 7501") == ""


def test_a_merge_with_an_order_or_his_typed_word_naming_it_is_not_warned(env):
    env.bind()
    env.prompt("merge #7501 now please, finish it asap")
    assert pretool(env, "gh pr merge 7501 --squash") == ""
    assert "merging #7502" in pretool(env, "gh pr merge https://github.com/acme/repo/pull/7502")


def test_a_merge_warning_follows_the_typed_word_when_no_order_is_open(env):
    env.bind()
    p = env.state / "recent-prompts.jsonl"
    p.write_text(json.dumps({"text": "go ahead and merge 7503", "ts": time.time() - 60}) + "\n")
    assert pretool(env, "gh pr merge 7503") == ""
    assert "merging #7504" in pretool(env, "gh pr merge 7504")


def test_a_hand_opened_merge_order_naming_the_pr_silences_the_guard(env):
    env.bind()
    env.run("order", "open", "--kind", "merge", "--quote", "merge 7505 on his word")  # no typed prompt in the record
    assert pretool(env, "gh pr merge 7505") == ""
    assert "merging #7506" in pretool(env, "gh pr merge 7506")


def test_a_status_line_that_uses_the_word_done_is_not_a_done_claim(env):
    env.bind()
    prompts(env, "do three things: fix the log, tidy the docs, and reply to the thread")
    for plain in ("Tests done: 12 passed, 0 failed.", "PR #101 status: review done, waiting on the merge.", "The build is complete for the first repo; the second is running.",
                  "Step 2 finished. Starting step 3."):
        assert stop(env, last_assistant_message=plain) is None, plain
    for claim in ("Everything is done.", "I'm done.", "All set, nothing left."):
        assert "have not re-read the user's words" in stop(env, last_assistant_message=claim)["reason"], claim


def test_a_machine_named_release_server_opens_no_order_and_an_insult_at_a_tool_is_no_correction(env):
    env.bind()
    out = env.prompt("need to ssh into the release server now and check disk").stdout
    assert "opened from the user's prompt" not in out and not env.run("order", "ls").stdout.strip().startswith("O-")
    assert "opened from the user's prompt" in env.prompt("do the release now, merge both PRs").stdout
    assert "CORRECTION" not in env.prompt("remove stupid ci from the workflow list").stdout
    assert "CORRECTION" in env.prompt("you are stupid").stdout


# ---- pending-on-the-user lists need a lookup in the same turn ----


def test_a_list_of_items_pending_on_the_user_with_no_lookup_is_held(env):
    env.bind()
    msg = "Three items are pending on you: the merge, the draft and the invoice."
    held = stop(env, last_assistant_message=msg, transcript_path=transcript(env, []))
    assert "pending on the user" in held["reason"] and "read nothing" in held["reason"]
    assert stop(env, last_assistant_message=msg, transcript_path=transcript(env, ["Bash"])) is None
    assert stop(env, last_assistant_message="Nothing is pending on you.", transcript_path=transcript(env, [])) is None


# ---- a draft asked for is a work item until it is sent or discarded ----


def work_titles(env):
    p = env.state / "work.json"
    return [i["title"] for i in json.loads(p.read_text())] if p.exists() else []


def test_a_draft_ask_opens_one_work_item_closed_by_send_or_discard(env):
    env.bind()
    env.prompt("write me a standup draft for today")
    env.prompt("make the draft shorter")
    assert [t for t in work_titles(env) if t.startswith("draft: ")] == ["draft: write me a standup draft for today"]
    env.hook("pretool", {"hook_event_name": "PreToolUse", "session_id": "sess-hub", "tool_name": "Bash", "tool_input": {"command": "mep send standup.md"}})
    assert work_titles(env) == []
    env.prompt("draft a reply to the invoice mail")
    assert len(work_titles(env)) == 1
    env.prompt("discard the draft")
    assert work_titles(env) == []
    env.prompt("what is the status")
    assert work_titles(env) == []


def test_a_brief_banning_admin_while_a_release_order_is_open_is_warned(env):
    env.bind()
    env.prompt("do the release now, finish it asap")
    brief = "Run the release. Output: prod live. " + LONG + "\nNever use --admin to merge."
    assert "release.py runs that itself" in send(env, "peer-a", brief, "release").stdout
    assert "release.py runs that itself" not in send(env, "peer-b", brief.replace("Never use --admin", "Never touch billing"), "other").stdout


def test_words_inside_pasted_content_open_no_order(env):
    env.bind()
    env.prompt("<pasted_content>status: the release is blocked on smoke</pasted_content> what do you think?")
    assert env.rows("orders.jsonl") == []
    env.prompt("do the release now, finish it asap <pasted_content>old report</pasted_content>")
    assert len([r for r in env.rows("orders.jsonl") if r["op"] == "open"]) == 1
