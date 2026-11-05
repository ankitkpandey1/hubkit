#!/usr/bin/env python3
"""Run each guard test against a copy of the scripts with that guard reverted; it must FAIL.

Usage: python3 tests/run_reverts.py   (exit 0 when every reverted copy fails its test)
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
T = "tests/test_hubkit.py::"

# (name, file, old, new, test)
REVERTS = [
    ("cross-session notices", "hubcore/store.py", '"[artifact", "[system", "[cross-session")', '"[artifact", "[system")', "test_cross_session_notices_never_open_orders"),
    ("harness and peer text filter", "hubcore/store.py", "    return not any(m in squashed for m in HARNESS_MARKERS)", "    return True", "test_peer_and_harness_text_never_open_orders"),
    ("whole-text marker scan and look-alike hyphens", "hubcore/store.py", "    return not any(m in squashed for m in HARNESS_MARKERS)", "    return not any(m in t[:400] for m in HARNESS_MARKERS)", "test_peer_marker_far_into_text_and_lookalike_hyphens_are_not_human"),
    ("origin must be human", "hubcore/store.py", "    if origin is not None and origin != \"human\":\n        return False\n", "", "test_origin_must_be_human_when_present"),
    ("typed paste stays human", "hubcore/store.py", 're.sub(r"</?pasted_content[^>]*>", " ", _normalise(text))', "_normalise(text)", "test_typed_paste_is_still_human"),
    ("evidence required on close", "hubcore/orders.py", "            if problems:\n", "            if False:\n", "test_close_refused_without_evidence_then_accepted"),
    ("bound-hub session match", "hubcore/hooks.py", 'payload.get("session_id") != b["session_id"]', "False", "test_noop_outside_bound_hub"),
    ("headless no-op", "hubcore/hooks.py", 'sess is not None and sess.get("kind") == "interactive" and ', "sess is not None and ", "test_noop_in_headless_session"),
    ("hold/cancel verbs", "hubcore/orders.py", "if (hold or cancel or resume) and short:", "if False:", "test_hold_and_cancel_verbs_and_prompt_intents"),
    ("redact before write", "hubcore/store.py", "    row = redact_obj(row)\n", "", "test_append_row_redacts_even_when_the_caller_did_not"),
    ("hook never exits non-zero", "hubcore/hooks.py", "except BaseException as e:  # a hook", "except ZeroDivisionError as e:  # a hook", "test_hook_crash_exits_zero_logs_and_shows_one_line"),
    ("state not in git", "hubcore/store.py", "    if _inside_git(d):", "    if False:", "test_state_inside_git_repo_is_refused"),
    ("bind liveness", "hubcore/hooks.py", '    return proc_start(int(b["pid"])) == str(b["procStart"])', "    return True", "test_dead_hub_frees_bind_and_session_start_reports_open_orders"),
    ("stop_hook_active", "hubcore/hooks.py", ' or payload.get("stop_hook_active")', "", "test_stop_blocks_until_a_watch_is_running_then_lets_the_turn_end"),
    ("alert back-off", "hubcore/status.py", "    return min(1800.0, 600.0 * 2 ** max(n - 1, 0)) if n else 0.0", "    return 0.0", "test_watchdog_alerts_then_backs_off"),
    ("learnings lock", "hubcore/learnings.py", "    return store.locked(path.with_name(f\".{path.name}.lock\"))", "    import contextlib\n    return contextlib.nullcontext()", "test_learn_concurrent_adds_lose_nothing"),
    ("held order has no clock", "hubcore/status.py", '        if o["state"] != "open":  # held', '        if False:  # held', "test_hold_and_cancel_verbs_and_prompt_intents"),
    ("unconfirmed has no clock", "hubcore/status.py", '        if o["state"] != "open":  # held', '        if False:  # held', "test_unconfirmed_order_expires_and_has_no_clock"),
    ("file evidence must be fresh", "hubcore/orders.py", 'elif p.stat().st_mtime < o["opened"]:', "elif False:", "test_file_evidence_older_than_order_is_refused"),
    ("explicit hold words (old broad list)", "intents.example.toml", 'hold_words = ["hold off", "on hold", "put it on hold", "hold the", "hold it", "pause", "not yet", "don\'t deploy", "dont deploy", "do not deploy", "stand by"]', 'hold_words = ["hold", "don\'t", "dont", "do not", "wait", "not yet", "stop"]', "test_hold_phrases_are_explicit_not_bare_dont_or_wait"),
    ("wrapped peer marker filter", "hubcore/store.py", "    return not any(m in squashed for m in HARNESS_MARKERS)", "    return True", "test_wrapped_peer_forms_open_no_order_no_chase_no_correction"),
    ("urgent words beat hold", "hubcore/orders.py", 'hold = has("hold_words") and not urgent', 'hold = has("hold_words")', "test_dont_wait_merge_now_keeps_clock_and_pr_ref_is_not_a_chase"),
    ("urgent words restart held", "hubcore/orders.py", "    if urgent and kind and held:", "    if False:", "test_urgent_words_restart_a_held_order"),
    ("PR ref is not a chase", "hubcore/orders.py", "    elif same and PR_REF.search(text):", "    elif False:", "test_dont_wait_merge_now_keeps_clock_and_pr_ref_is_not_a_chase"),
    ("stop-loss line", "hubcore/orders.py", "                if n >= stop:", "                if False:", "test_stop_loss_after_repeated_chases"),
    ("overdue at next prompt", "hubcore/orders.py", "    lines += overdue_lines(orders, now)\n", "", "test_overdue_order_reaches_hub_at_next_prompt"),
    ("correction detection", "hubcore/hooks.py", "    if any(re.search(p, text) for p in cfg.get(\"patterns\", [])):", "    if False:", "test_correction_phrase_and_repeated_instruction_inject_record_prompt"),
    ("address needs a rule change", "hubcore/learnings.py", "            if not changed:", "            if False:", "test_address_refused_when_tagged_rule_already_existed_unchanged"),
    ("tick --if-bound", "hubcore/cli.py", "            if a.if_bound and not", "            if False and not", "test_tick_if_bound_is_noop_without_hub_or_orders"),
    ("payload canary wired", "hubcore/hooks.py", "            hook_record(cfg, payload)  # real", "            pass  # real", "test_payload_samples_come_from_real_hook_runs"),
    ("learnings expiry", "hubcore/status.py", "> 30 * 86400:\n            continue", "> 30 * 86400000:\n            continue", "test_block_expires_old_and_marks_probation"),
    ("transcript hook-failure scan", "hubcore/status.py", '    if hook_err["count"]:', "    if False:", "test_hook_failures_found_by_transcript_scan"),
    ("hook import failure exits 0", "hubkit", "        sys.exit(0)\n    raise\n", "    raise\n", "test_import_failure_in_a_hook_exits_zero"),
    ("unconfirmed expiry", "hubcore/status.py", 'if o["state"] == "unconfirmed" and age_min > ttl:', "if False:", "test_unconfirmed_order_expires_and_has_no_clock"),
    ("held-too-long alert", "hubcore/status.py", "            if (now - since) / 60 > held_max:", "            if False:", "test_held_order_raises_review_alert_after_limit"),
    ("cancel my meeting cancels nothing", "hubcore/orders.py", "    return live if bare and len(live) == 1 else []", "    return live", "test_cancel_without_order_object_cancels_nothing"),
    ("question does not open an order", "hubcore/orders.py", 'act = " ".join(statements)', "act = text", "test_question_with_order_word_opens_nothing"),
    ("glued glpat and AIza (no word boundary)", "hubcore/redact.py", 're.compile(r"glpat-', 're.compile(r"\\bglpat-', "test_glued_secrets_are_redacted"),
    ("import log redacted", "hubkit", "                    msg = redact(msg)[0]\n", "                    pass\n", "test_import_error_log_is_redacted_and_survives_a_broken_redactor"),
    ("import log survives broken redactor", "hubkit", "except BaseException:  # the redactor", "except ZeroDivisionError:  # the redactor", "test_import_error_log_is_redacted_and_survives_a_broken_redactor"),
    ("glpat pattern", "hubcore/redact.py", '    re.compile(r"glpat-[A-Za-z0-9_-]{15,}"),\n', "", "test_more_secret_shapes_are_redacted"),
    ("AIza pattern", "hubcore/redact.py", '    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),\n', "", "test_more_secret_shapes_are_redacted"),
    ("status.json redacted at write", "hubcore/store.py", "json.dumps(redact_obj(obj, 2000), indent=indent", "json.dumps(obj, indent=indent", "test_status_json_is_redacted_at_write"),
    ("redact then truncate", "hubcore/store.py", "    return redact(text)[0][:n]", "    return redact(text[:n])[0]", "test_cut_secret_does_not_leak_a_prefix"),
    ("blocking order lock", "hubcore/orders.py", '        with store.locked(self.sd / "lock"):  # blocking', '        with __import__("contextlib").nullcontext():  # blocking', "test_order_open_waits_for_a_held_lock_instead_of_skipping_it"),
    ("close row before archive row", "hubcore/orders.py", '            self._append(oid, "close", evidence=evidence)  # source of truth first\n            self._die_after("close")\n            self._archive(self.get(oid), "closed", {"evidence": evidence})', '            self._archive(self.get(oid), "closed", {"evidence": evidence})\n            self._die_after("close")\n            self._append(oid, "close", evidence=evidence)', "test_close_row_is_written_before_archive_and_crash_is_repaired"),
    ("check-clean scans tests and references", "hubcore/cli.py", '{"__pycache__", ".pytest_cache", ".git"} & set(', '{"__pycache__", ".pytest_cache", ".git", "tests", "references"} & set(', "test_check_clean_flags_a_term_and_passes_when_absent"),
    ("mep fallback dirs for cron PATH", "hubcore/status.py", '("~/.local/bin", "/usr/local/bin", "~/bin")', "()", "test_mep_found_with_cron_style_path"),
    ("mep stale-keep", "hubcore/status.py", 'if prev and prev.get("state") in ("ok", "stale"):', "if False:", "test_slow_mep_keeps_last_good_answer_not_unverified"),
    ("mep timeout default", "hubcore/config.py", '"mep": {"enabled": True, "timeout_s": 30}', '"mep": {"enabled": True, "timeout_s": 5}', "test_default_mep_timeout_covers_a_real_status_call"),
    # routed work
    ("routed: acks and long non-work are not tracked", "hubcore/routed.py", '    if asked if asked is not None else PR_REF.search(text):\n        return True\n    return len(text) >= int(o["min_chars"]) and not ACK.match(text)', "    return True", "test_acks_inbox_watcher_and_other_sessions_are_not_tracked"),
    ("routed: inbox watcher is never tracked", "hubcore/routed.py", '    if any(s.lower() in owner.lower() for s in o["skip_owners"]):\n        return False\n', "", "test_acks_inbox_watcher_and_other_sessions_are_not_tracked"),
    ("routed: hub session only", "hubcore/hooks.py", 'if not is_hub(cfg, payload) or tool not in ("SendMessage", "Agent"):', 'if tool not in ("SendMessage", "Agent"):', "test_acks_inbox_watcher_and_other_sessions_are_not_tracked"),
    ("routed: foreground agent not tracked", "hubcore/routed.py", '        "run_in_background"\n    ):', '        "run_in_background", True\n    ):', "test_background_agent_is_tracked_foreground_agent_is_not"),
    ("routed: baseline head taken at routing", "hubcore/routed.py", '        if info:\n            heads[url] = info["head"]', '        if False:\n            heads[url] = info["head"]', "test_moved_head_alerts_and_the_pr_is_read_once_per_tick"),
    ('routed: re-routing clears the moved flag', 'hubcore/routed.py', '            head_moved=None,\n', '', 'test_a_new_brief_is_a_new_ask_and_clears_flags_and_restarts_the_clock'),
    ("routed: transcript scan marks heard", "hubcore/routed.py", "    if not path:\n        return 0", "    return 0", "test_message_from_the_owner_updates_last_heard_and_opens_nothing"),
    ("routed: peer text reaching the prompt hook marks heard", "hubcore/hooks.py", "    if peer := routed.peer_name(text):", "    if False:", "test_prompt_hook_peer_text_marks_heard_but_opens_no_order"),
    ("routed: stall limit from config", "hubcore/routed.py", '{**cfg["routed"], **load_intents', '{**config.DEFAULTS["routed"], **load_intents', "test_stall_limit_comes_from_config"),
    ("routed: tick raises routed alerts", "hubcore/status.py", "        found += routed.alerts(cfg, now)", "        found += []", "test_stalled_item_reaches_notify_cmd"),
    ("routed: moved head detected", "hubcore/routed.py", 'elif not same_head(old, info["head"]):', "elif False:", "test_moved_head_alerts_and_the_pr_is_read_once_per_tick"),
    ("routed: one gh read per PR per tick", "hubcore/routed.py", "for u in sorted(todo)}", "for u in sorted(todo) * 2}", "test_moved_head_alerts_and_the_pr_is_read_once_per_tick"),
    ("routed: gh timeout is unknown, not a crash", "hubcore/routed.py", "except OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError:", "except OSError, ValueError, KeyError, TypeError:", "test_gh_failure_or_timeout_is_unknown_and_never_crashes_the_tick"),
    ("routed: review counts as movement", "hubcore/routed.py", 'if info["review_at"] and info["review_at"] > base(cfg, i):', "if False:", "test_review_after_routing_counts_as_movement"),
    ("routed: watch wakes on a stall", "hubcore/status.py", "        if fresh:\n            for r in fresh:", "        if False:\n            for r in fresh:", "test_watch_wakes_the_hub_on_a_stall_without_any_prompt"),
    ("routed: status lists routed items", "hubcore/status.py", "        out += routed.block_lines(cfg, st[\"work\"], now)", "        pass", "test_work_done_closes_a_routed_item_and_status_lists_it"),
    ("routed: work add --kind routed", "hubcore/cli.py", 'if a.action == "add" and a.kind == "routed":', "if False:", "test_work_add_routed_by_hand_dedupes_with_the_hook"),
    ("routed: review clock is the short one", "hubcore/routed.py", 'return float(o["review_stall_min"] if item.get("review") else o["stall_min"])', 'return float(o["stall_min"])', "test_review_items_chase_at_5_and_are_overdue_at_10_minutes_from_routing"),
    # fixes of 11-05 that had no revert proof
    ("re-bind: candidate must resume or carry the hub name", "hubcore/hooks.py", "    return pid if resumed or named else None", "    return pid", "test_session_start_rebinds_dead_bind_for_resumed_session_only"),
    ("re-bind: SessionStart re-binds", "hubcore/hooks.py", "        pid = _rebind_candidate(cfg, payload)\n        if pid is None:", "        pid = None\n        if pid is None:", "test_session_start_rebinds_dead_bind_for_resumed_session_only"),
    ("cron heartbeat on every tick run", "hubcore/cli.py", '            store.touch_seen(store.state(cfg), "cron")', "            pass", "test_cron_seen_touched_even_when_nothing_to_do"),
    ("card answer: hub session only", "hubcore/hooks.py", 'if not is_hub(cfg, payload) or payload.get("tool_name") != "AskUserQuestion":', 'if payload.get("tool_name") != "AskUserQuestion":', "test_card_answer_opens_an_order_but_peer_text_does_not"),
    ("card answer: human-only filter", "hubcore/hooks.py", '    text = _answer_text(payload.get("tool_response"))\n    if not store.is_human(text):\n        return ""\n', '    text = _answer_text(payload.get("tool_response"))\n', "test_card_answer_opens_an_order_but_peer_text_does_not"),
    ('archive crash repair', 'hubcore/status.py', '        orders.repair_archive()\n        closed = routed_pass', '        closed = routed_pass', 'test_close_row_is_written_before_archive_and_crash_is_repaired'),
    ('heard: queued peer message', 'hubcore/routed.py', 'if o.get("type") == "queue-operation":', 'if False:', 'test_peer_message_still_queued_marks_heard'),
    ('heard: queued task notification', 'hubcore/routed.py', 'startswith("<task-notification")', 'startswith("<never")', 'test_agent_completion_still_queued_marks_heard'),
    ('watcher briefs are not work', 'hubcore/routed.py', 'if any(w.lower() in low for w in opts(cfg)["skip_briefs"]):', 'if False:', 'test_watcher_and_deliverable_free_agents_are_not_tracked'),
    ('agent without deliverable is not work', 'hubcore/routed.py', 'return bool(DELIVERABLE.search(prompt) or PR_REF.search(prompt))', 'return True', 'test_watcher_and_deliverable_free_agents_are_not_tracked'),
    ('bare ref: full URL replaces it', 'hubcore/routed.py', 'if n not in by_num or ref.startswith("http"):', 'if n not in by_num:', 'test_bare_ref_and_full_url_to_the_same_owner_are_one_outcome'),
    ('bare ref: same number is one outcome', 'hubcore/routed.py', 'return bool(_nums([*i["prs"], *i.get("queue", [])]) & _nums(prs))', 'return False', 'test_bare_ref_and_full_url_to_the_same_owner_are_one_outcome'),
    ('bare ref: resolves against repo', 'hubcore/routed.py', 'f"https://github.com/{repo}/pull/{n}" if repo else f"#{n}"', 'f"#{n}"', 'test_bare_ref_resolves_against_the_configured_repo_so_gh_runs'),
    ('one chase line per owner', 'hubcore/routed.py', 'by_owner.setdefault(i["owner"], []).append(i)', 'by_owner.setdefault(i["id"], []).append(i)', 'test_one_owner_gets_one_chase_line_whatever_the_item_count'),
    ('chase is an event, not a clock', 'hubcore/routed.py', 'if hit is not None and (chase or follow_up):', 'if False:', 'test_a_chase_is_an_event_not_a_new_clock'),
    ('no-PR follow-up is the same item', 'hubcore/routed.py', 'return not i["prs"]  # no PR named', 'return not i["prs"] and i["title"] == summary  # no PR named', 'test_a_new_brief_to_the_same_owner_without_a_pr_refreshes_the_one_item'),
    ('chased item does not alert', 'hubcore/routed.py', 'if lvl is not None and needs_action(cfg, i, now):', 'if lvl is not None:', 'test_a_chased_item_neither_alerts_nor_wakes_the_watch_again'),
    ('ladder steps', 'hubcore/routed.py', 'min(int(idle // lim) - 1, 2)', '0', 'test_ladder_climbs_and_each_step_alerts_once'),
    ('NEXT line in status', 'hubcore/routed.py', 'out.append(f"      NEXT: {next_action(cfg, i, now)}")', 'pass', 'test_every_item_shows_one_next_action'),
    ('review closes at the current head', 'hubcore/routed.py', '            elif done:\n                msgs.append(_close_landed', '            elif False:\n                msgs.append(_close_landed', 'test_review_at_the_current_head_closes_the_item_by_itself'),
    ('old-head review never closes', 'hubcore/routed.py', '        if same_head(last["commit"], info["head"]):\n            out[u]', '        if True:\n            out[u]', 'test_a_review_on_an_older_head_never_closes_the_item'),
    ('author review is ignored', 'hubcore/routed.py', '    if rev["login"] != info["author"]:\n        return True\n', '    return True\n', 'test_the_authors_own_review_does_not_close_a_review_item'),
    ('queue: one PR at a time', 'hubcore/routed.py', 'prs = every[:1] if queue else every', 'prs = every', 'test_a_queue_is_clocked_one_pr_at_a_time'),
    ('queue: advance on review', 'hubcore/routed.py', 'if done and i.get("queue"):', 'if False:', 'test_a_queue_is_clocked_one_pr_at_a_time'),
    ('never re-brief a reporting owner', 'hubcore/routed.py', 'if lvl >= 1 and heard and now - heard < limit(cfg, item) * 60:', 'if False:', 'test_next_never_says_re_brief_while_the_owner_is_alive_and_reporting'),
    ('running agent counts as heard', 'hubcore/routed.py', 'if seen > base(cfg, i):', 'if False:', 'test_a_running_agent_with_a_growing_transcript_counts_as_heard'),
    ('own gate block is not a hook failure', 'hubcore/status.py', 'and not _is_our_gate(att)', '', 'test_our_own_stop_gate_block_is_not_counted_as_a_hook_failure'),
    ('hook-error alert key is stable', 'hubcore/status.py', '{"key": f"hook_errors:{hook_err[\'last\']}",', '{"key": f"hook_errors:{hook_err[\'count\']}",', 'test_a_repeating_hook_error_count_is_one_alert_key'),
    ('gate: watch must be armed', 'hubcore/gate.py', 'if open_work(cfg) and not watch_live(cfg):', 'if False:', 'test_stop_blocks_until_a_watch_is_running_then_lets_the_turn_end'),
    ('gate: question needs an ask', 'hubcore/gate.py', ' and not blocked_on_user(cfg)', '', 'test_a_question_to_the_user_needs_something_blocked_on_him'),
    ('gate: question check', 'hubcore/gate.py', 'if is_question(cfg, last) and not blocked_on_user(cfg):', 'if False:', 'test_a_question_to_the_user_needs_something_blocked_on_him'),
    ('gate: verdict check', 'hubcore/gate.py', 'for n in unverified_verdict(cfg, last):', 'for n in []:', 'test_a_verdict_is_checked_against_github_per_pr_and_per_sentence'),
    ('gate: verdict per sentence', 'hubcore/gate.py', 're.split(r"(?<=[.!?])\\s+|\\n", last)', '[last]', 'test_a_verdict_is_checked_against_github_per_pr_and_per_sentence'),
    ('gate: verdict read from GitHub', 'hubcore/gate.py', '    info = routed.pr_info(cfg, url, timeout)\n    if info is None:\n        return True\n', '    return False\n    info = routed.pr_info(cfg, url, timeout)\n    if info is None:\n        return True\n', 'test_a_verdict_is_checked_against_github_per_pr_and_per_sentence'),
    ('stop output reaches the model as a block', 'hubcore/hooks.py', 'json.dumps({"decision": "block", "reason": "\\n".join(reasons)})', '"\\n".join(reasons)', 'test_stop_blocks_until_a_watch_is_running_then_lets_the_turn_end'),
    ('a stalled item blocks the stop', 'hubcore/gate.py', 'reasons = routed.prompt_lines(cfg, now)', 'reasons = []', 'test_a_stalled_item_blocks_the_stop_until_the_owner_is_chased'),
    ('one watch only', 'hubcore/status.py', '    if gate.watch_live(cfg):', '    if False:', 'test_stop_blocks_until_a_watch_is_running_then_lets_the_turn_end'),
    ('watch writes its pidfile', 'hubcore/status.py', 'store.write_atomic(pidfile, f"{os.getpid()} {hooks.proc_start(os.getpid())}")', 'pass', 'test_stop_blocks_until_a_watch_is_running_then_lets_the_turn_end'),
    ('answer closes asks', 'hubcore/hooks.py', 'routed.close_asks(', '(lambda *a: 0)(', 'test_a_question_to_the_user_needs_something_blocked_on_him'),
    ('sha kept under head fields', 'hubcore/redact.py', '        if sha_ok and GIT_SHA.fullmatch(obj):', '        if False:', 'test_a_40_hex_head_survives_storage_and_unchanged_head_is_not_a_move'),
    ('sha redacted outside head fields', 'hubcore/redact.py', '        if sha_ok and GIT_SHA.fullmatch(obj):', '        if GIT_SHA.fullmatch(obj):', 'test_sha_is_kept_only_under_head_fields_and_secrets_are_still_redacted'),
    ('verdict_acked is a sha field', 'hubcore/redact.py', ', "verdict_acked"})', '})', 'test_an_old_head_verdict_blocks_once_and_a_chase_clears_it_for_good'),
    ('mangled baseline is rebaselined', 'hubcore/routed.py', 'if not (old and HEX_HEAD.fullmatch(old)):', 'if old is None:', 'test_a_mangled_stored_baseline_is_rebaselined_not_called_a_move'),
    ("follow-up naming the item's PR is a chase", 'hubcore/routed.py', 'and _nums(named) <= _nums([*hit["prs"], *hit.get("queue", [])])', 'and False', 'test_follow_up_naming_a_pr_on_the_item_is_a_chase_that_keeps_the_clock_and_answers_a_moved_head'),
    ('chase clears the moved-head flag', 'hubcore/routed.py', 'item["head_moved"] = item["verdict_old_head"] = None', 'pass', 'test_follow_up_naming_a_pr_on_the_item_is_a_chase_that_keeps_the_clock_and_answers_a_moved_head'),
    ('chase rebaselines the head', 'hubcore/routed.py', 'item["heads"] = {**item["heads"], **heads}', 'pass', 'test_follow_up_naming_a_pr_on_the_item_is_a_chase_that_keeps_the_clock_and_answers_a_moved_head'),
    ('chase acks an old-head verdict', 'hubcore/routed.py', 'item["verdict_acked"] = [v["commit"], v["head"]]', 'pass', 'test_an_old_head_verdict_blocks_once_and_a_chase_clears_it_for_good'),
    ('acked verdict does not refire', 'hubcore/routed.py', 'old if old and acked != [old["commit"], old["head"]] else None', 'old', 'test_an_old_head_verdict_blocks_once_and_a_chase_clears_it_for_good'),
    ("short follow-up chases the owner's items", 'hubcore/routed.py', 'if _is_follow_up(cfg, owner, text):', 'if False:', 'test_a_short_follow_up_without_a_pr_counts_as_a_chase_on_every_item_of_the_owner'),
    ('acknowledgement is not a chase', 'hubcore/routed.py', '        not ACK.match(text)\n', '        True\n', 'test_a_short_follow_up_without_a_pr_counts_as_a_chase_on_every_item_of_the_owner'),
    ('several PRs are a queue', 'hubcore/routed.py', 'len(every) > 1 else []', 'len(every) > MAX_PRS else []', 'test_two_prs_in_one_message_keep_one_pr_per_clock'),
    ('queue members match the item', 'hubcore/routed.py', '_nums([*i["prs"], *i.get("queue", [])]) & _nums(prs)', '_nums(i["prs"]) & _nums(prs)', 'test_two_prs_in_one_message_keep_one_pr_per_clock'),
    ('landed PR is dropped from the item', 'hubcore/routed.py', 'item["prs"] = [u for u in item["prs"] if u not in done]', 'item["prs"] = item["prs"]', 'test_landed_reviews_drop_their_pr_and_the_gaps_are_reported'),
    ('gaps are reported', 'hubcore/routed.py', 'Still open:', 'Left:', 'test_landed_reviews_drop_their_pr_and_the_gaps_are_reported'),
    ('bare refs are resolved', 'hubcore/routed.py', '    resolve_bare_refs(cfg)\n    todo', '    todo', 'test_bare_refs_recorded_before_the_repo_was_set_are_resolved_so_gh_runs'),
    ('bare ref unknown says why', 'hubcore/routed.py', 'routed.repo is not set', 'gh failed', 'test_bare_refs_recorded_before_the_repo_was_set_are_resolved_so_gh_runs'),
    ('verdict block names an action', 'hubcore/gate.py', 'NEXT: correct the message', 'correct the message', 'test_every_stop_block_names_a_concrete_action'),
    ('reply to a peer is not an ask', 'hubcore/routed.py', 'if _is_reply(cfg, owner, text, asked, now) or not', 'if not', 'test_a_reply_to_a_peer_who_wrote_to_the_hub_is_not_tracked'),
    ('peer writes are remembered', 'hubcore/routed.py', '    _note_peers(cfg, events)\n', '', 'test_a_reply_to_a_peer_who_wrote_to_the_hub_is_not_tracked'),
    ('peer writes are remembered with no items', 'hubcore/routed.py', '    if not path:\n        return 0', '    if not path or not items(cfg):\n        return 0', 'test_a_reply_to_a_peer_who_wrote_to_the_hub_is_not_tracked'),
    ('a brief makes a reply an ask', 'hubcore/routed.py', '    if asked or DELIVERABLE.search(text):\n        return False', '    if asked:\n        return False', 'test_a_reply_to_a_peer_who_wrote_to_the_hub_is_not_tracked'),
    ('long message: only directed PRs', 'hubcore/routed.py', '    if len(text) < int(opts(cfg)["min_chars"]):\n        return refs', '    if True:\n        return refs', 'test_a_pr_named_in_passing_in_a_long_message_is_not_the_owners_pr'),
    ('long message: directive names a PR', 'hubcore/routed.py', '*(m.group(0) for m in DIRECTIVE.finditer(text))', '*()', 'test_a_pr_named_in_passing_in_a_long_message_is_not_the_owners_pr'),
    ('doctor exempts head shas', 'hubcore/redact.py', 'sum(count_hits_obj(v, sha_ok or k in SHA_KEYS) for k, v in obj.items())', 'sum(count_hits_obj(v, sha_ok) for k, v in obj.items())', 'test_doctor_does_not_call_a_stored_head_sha_a_secret_but_still_finds_secrets'),
    ('review needs a review word beside a PR', 'hubcore/routed.py', '    return "review" in _asks(cfg, strip_examples(text), summary)', '    return any(x.lower() in text.lower() for x in opts(cfg)["review_words"])', 'test_a_handover_that_mentions_review_elsewhere_is_not_a_review'),
    ('author asked: no review tag at routing', 'hubcore/routed.py', 'bool(prs) and not authored and not chase and is_review(', 'bool(prs) and not chase and is_review(', 'test_the_person_asked_being_the_prs_author_is_not_a_review_and_not_a_head_alert'),
    ('author asked: no head alert', 'hubcore/routed.py', '                elif owner_is_author(cfg, i["owner"], info["author"]):', '                elif False:', 'test_the_person_asked_being_the_prs_author_is_not_a_review_and_not_a_head_alert'),
    ('author found later loses the review tag', 'hubcore/routed.py', '                    i["review"] = False  # the person asked', '                    pass  # the person asked', 'test_an_owner_found_to_be_the_author_later_loses_the_review_tag'),
    ('owner_logins map', 'hubcore/routed.py', '    return (mapped or "").lower() == login or owner.lower() == login', '    return owner.lower() == login', 'test_an_owner_found_to_be_the_author_later_loses_the_review_tag'),
    ('agent id is never the author', 'hubcore/routed.py', 'or (len(login) >= 4 and login in owner.lower())', 'or login in owner.lower()', 'test_agents_have_no_login_and_are_never_the_author'),
    ('ETA names every open PR', 'hubcore/routed.py', "for an ETA on {' '.join(_pr_no(u) for u in item['prs']) or 'the work'};", "for an ETA on {_pr_no(item['prs'][0]) if item['prs'] else 'the work'};", 'test_eta_next_names_the_open_prs_not_the_first_one'),
    ('queued: detection', 'hubcore/routed.py', '    return bool(QUEUED.search(f"{summary}\\n{text}"))', '    return False', 'test_a_review_queued_behind_other_work_is_not_overdue_while_the_owner_is_busy'),
    ('queued: detection is narrow', 'hubcore/routed.py', "{0,40}?(?:'s", "{0,40}?(?:|'s", 'test_queued_phrases_are_detected_narrowly'),
    ('queued: no clock until the wait is over', 'hubcore/routed.py', 'if item.get("waiting") else t', 'if False else t', 'test_a_review_queued_behind_other_work_is_not_overdue_while_the_owner_is_busy'),
    ('queued: review on the PR starts it', 'hubcore/routed.py', 'i["waiting"] = False  # a review shows', 'pass  # a review shows', 'test_a_review_showing_on_the_pr_means_the_owner_started_the_queued_work'),
    ('queued: NEXT says why it is quiet', 'hubcore/routed.py', 'queued behind other work: no clock until', 'no clock until', 'test_a_review_queued_behind_other_work_is_not_overdue_while_the_owner_is_busy'),
    ('state ask: wording is recognised', 'hubcore/routed.py', '|one[- ]line (state|status)|current state|what state|state of)', ')', 'test_a_state_ask_is_not_a_review_and_the_owners_reply_closes_it'),
    ('state ask: never a review', 'hubcore/routed.py', 'not chase and is_review(cfg, text, summary)', 'is_review(cfg, text, summary)', 'test_a_state_ask_is_not_a_review_and_the_owners_reply_closes_it'),
    ('state ask: flagged on the item', 'hubcore/routed.py', 'state_ask=chase and not brief,', 'state_ask=False,', 'test_a_state_ask_is_not_a_review_and_the_owners_reply_closes_it'),
    ('state ask: the owner reply closes it', 'hubcore/routed.py', 'if i.get("state_ask") and ts > (i.get("routed_at") or 0):', 'if False:', 'test_a_state_ask_is_not_a_review_and_the_owners_reply_closes_it'),
    ('chase: an action on a PR or a brief marker is never a chase', 'hubcore/routed.py', 'if DELIVERABLE.search(text) or any(DIRECTIVE.match(x) for x in sentences):', 'if False:', 'test_an_action_on_a_pr_or_a_brief_marker_is_never_a_chase'),
    ("doctor --check-clean ignores .git", "hubcore/cli.py", '".pytest_cache", ".git"}', '".pytest_cache"}', "test_check_clean_names_the_file_but_ignores_dot_git"),
    # audited bugs of 11-05
    ('own PR: a COMMENT at the head counts (item close)', 'hubcore/routed.py', '        and rev["state"].upper().startswith("COMMENT")\n', '        and False\n', 'test_a_comment_at_the_head_closes_a_review_item_on_the_hub_users_own_pr'),
    ('own PR: a COMMENT at the head counts (queue advance)', 'hubcore/routed.py', '        and rev["state"].upper().startswith("COMMENT")\n', '        and False\n', 'test_a_comment_at_the_head_advances_a_queue_on_the_hub_users_own_pr'),
    ('own PR: a COMMENT at the head counts (stop gate)', 'hubcore/routed.py', '        and rev["state"].upper().startswith("COMMENT")\n', '        and False\n', 'test_the_stop_gate_accepts_a_comment_at_the_head_on_the_hub_users_own_pr_only'),
    ("own PR: the gate uses the shared rule", "hubcore/gate.py", "routed.counts_as_review(cfg, r, info) and", 'r["login"] != info["author"] and', "test_the_stop_gate_accepts_a_comment_at_the_head_on_the_hub_users_own_pr_only"),
    ("own PR: only the configured user's PRs", 'hubcore/routed.py', '        and rev["login"].lower() == me\n', '        and True\n', 'test_the_stop_gate_accepts_a_comment_at_the_head_on_the_hub_users_own_pr_only'),
    ('fix brief: a review noun is not a review ask', 'hubcore/routed.py', '    if hits and not (LEADS_CHANGE.match(low) and existing and not asks_later):', '    if hits:', 'test_a_fix_brief_that_mentions_a_review_is_not_a_review_item'),
    ("fix owner: pushes are the work, not a moved head", "hubcore/routed.py", '                elif i.get("pushes") and not i.get("review"):', "                elif False:", "test_a_head_move_by_the_owner_fixing_the_pr_raises_no_alert_but_a_review_item_still_does"),
    ("fix owner: the ask is flagged as pushing", "hubcore/routed.py", "        pushes_to_pr(cfg, text, summary) and not chase,", "        False,", "test_a_head_move_by_the_owner_fixing_the_pr_raises_no_alert_but_a_review_item_still_does"),
    ("examples: stripped before PRs are read", "hubcore/routed.py", '    text = strip_examples(text)\n    if len(text) < int(opts(cfg)["min_chars"]):', '    if len(text) < int(opts(cfg)["min_chars"]):', "test_example_and_quoted_pr_numbers_in_a_brief_are_not_tracked"),
    ('examples: sentences that say example are dropped', 'hubcore/routed.py', '        if skip_next or m:', '        if False:', 'test_example_and_quoted_pr_numbers_in_a_brief_are_not_tracked'),
    ("examples: quoted spans are dropped", "hubcore/routed.py", "    text = QUOTED.sub(\" \", text)", "    pass", "test_example_and_quoted_pr_numbers_in_a_brief_are_not_tracked"),
    ("verdict: awaiting wording is not a verdict", "hubcore/gate.py", " and not AWAITING.search(sentence):", ":", "test_awaiting_wording_is_not_a_verdict_but_a_real_verdict_is_still_blocked"),
    ("order: no hint line for a question", "hubcore/orders.py", "    elif kind and not same and not (hold or cancel) and (urgent", "    elif False and (urgent", "test_a_question_never_opens_an_order_even_with_an_urgent_word"),
    ("order: a question never opens one (urgent too)", "hubcore/orders.py", 'act = " ".join(statements)', "act = text", "test_a_question_never_opens_an_order_even_with_an_urgent_word"),
    ("order: an imperative after a question opens", "hubcore/orders.py", "and has(\"imperative\", act):", "and False:", "test_an_imperative_still_opens_an_order_even_after_a_question"),
    ('agent completed: its item closes', 'hubcore/routed.py', '                if by_text and COMPLETED.search(text) and ts > (i.get("routed_at") or i["added"]):', '                if False:', 'test_a_completed_agent_notification_closes_its_item_so_it_never_stalls'),
    ("order close: evidence covers every PR", "hubcore/orders.py", "        problems += self._uncovered_prs(o, evidence)", "        pass", "test_close_evidence_must_cover_every_pr_named_in_the_order"),
    ('ack naming a PR is not work', 'hubcore/routed.py', '    if ACK.match(text) and not (DELIVERABLE.search(text) or DIRECTIVE.search(text) or REQUEST.search(text)):', '    if False:', 'test_an_acknowledgement_that_names_a_pr_is_not_tracked_but_an_ask_that_starts_with_ok_is'),
    ('ack with a directive is work', 'hubcore/routed.py', ' or DIRECTIVE.search(text) or REQUEST.search(text)):\n        return False  # "Ack', ' or REQUEST.search(text)):\n        return False  # "Ack', 'test_an_acknowledgement_that_names_a_pr_is_not_tracked_but_an_ask_that_starts_with_ok_is'),
    ("work ids are never reused", "hubcore/status.py", '    nxt = max([int(i["id"][1:]) for i in items] + [last]) + 1', '    nxt = max([int(i["id"][1:]) for i in items] + [0]) + 1', "test_work_ids_are_never_reused_after_an_item_is_archived"),
    ("doctor flags a stale cron", "hubcore/cli.py", '    if age is None or age > float(cfg["watchdog"]["cron_stale_min"]):', "    if False:", "test_doctor_flags_a_stale_seen_cron_only_when_cron_is_configured"),
    ("doctor only flags when cron is configured", "hubcore/cli.py", '    if not cron_configured():\n        return', '    if False:\n        return', "test_doctor_flags_a_stale_seen_cron_only_when_cron_is_configured"),
    ('review ask: a change word does not hide a review verb', 'hubcore/routed.py', '    if hits and not (LEADS_CHANGE.match(low) and existing and not asks_later):', '    if hits and not PUSH.search(low) and not existing:', 'test_review_asks_with_a_change_word_or_a_leading_clause_are_still_reviews'),
    ('review ask: a later clause asking for a review wins', 'hubcore/routed.py', '    if hits and not (LEADS_CHANGE.match(low) and existing and not asks_later):', '    if hits and not (LEADS_CHANGE.match(low) and existing):', 'test_a_pr_with_review_as_a_noun_is_still_a_review_item'),
    ('review ask: review as a noun is still a review', 'hubcore/routed.py', '    if hits and not (LEADS_CHANGE.match(low) and existing and not asks_later):', '    if hits and not existing:', 'test_a_pr_with_review_as_a_noun_is_still_a_review_item'),
    ('examples: only the example part of a sentence goes', 'hubcore/routed.py', '            if m and not skip_next and not m.start() == 0:\n                kept.append(s[: m.start()])\n', '', 'test_review_asks_with_a_change_word_or_a_leading_clause_are_still_reviews'),
    ('verdict: awaiting is narrow', 'hubcore/gate.py', 'AWAITING = re.compile(\n', 'AWAITING = re.compile(r"(?i)\\b(not yet|until|still need\\w*|waiting)\\b")\n_UNUSED = re.compile(\n', 'test_awaiting_wording_is_not_a_verdict_but_a_real_verdict_is_still_blocked'),
    ('verdict: "review of #N is done" is a verdict', 'hubcore/gate.py', 'review(\\s+(of|on|for)\\s+\\S+)?\\s+(is\\s+)?', 'review\\s+(is\\s+)?', 'test_awaiting_wording_is_not_a_verdict_but_a_real_verdict_is_still_blocked'),
    ('agent completed: an older notification does not close', 'hubcore/routed.py', 'COMPLETED.search(text) and ts > (i.get("routed_at") or i["added"]):', 'COMPLETED.search(text):', 'test_a_completion_older_than_the_item_does_not_close_it'),
    ('agent completed: a review item stays open', 'hubcore/routed.py', '                    if not i.get("review"):  # a review item stays', '                    if True:  # a review item stays', 'test_a_completion_marks_a_review_item_reported_but_leaves_it_open'),
    ('agent completed: marks the owner reported', 'hubcore/routed.py', '                    i["reported"] = ts', '                    i["reported"] = 0', 'test_a_completion_marks_a_review_item_reported_but_leaves_it_open'),
    ('ack with a request cue is tracked', 'hubcore/routed.py', ' or REQUEST.search(text)):\n        return False  # "Ack', '):\n        return False  # "Ack', 'test_an_acknowledgement_with_a_request_is_still_tracked'),
    ('own PR: an empty-body COMMENT is a thread reply', 'hubcore/routed.py', '        and bool(rev["body"].strip())\n', '', 'test_an_inline_thread_reply_on_the_hub_users_own_pr_does_not_close_a_review_item'),
    ("mep missing: message says how to turn it off", "hubcore/status.py", " if exe is None else type(e).__name__", " if False else type(e).__name__", "test_mep_missing_says_how_to_turn_it_off"),
]


def main() -> int:
    bad = 0
    for name, rel, old, new, test in REVERTS:
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "scripts"
            shutil.copytree(SCRIPTS, copy, ignore=shutil.ignore_patterns("__pycache__"))
            f = copy / rel if not rel.endswith(".toml") else Path(td) / rel
            if rel.endswith(".toml"):
                shutil.copy(HERE.parent / rel, f)
            text = f.read_text()
            if text.count(old) != 1:
                print(f"BROKEN  {name}: pattern found {text.count(old)}x in {rel}", flush=True)
                bad += 1
                continue
            f.write_text(text.replace(old, new))
            env = dict(os.environ, HUBKIT_SCRIPTS=str(copy))
            if rel.endswith(".toml"):
                env["HUBKIT_TEST_INTENTS"] = str(f)
            r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", T + test, "-p", "no:cacheprovider"],
                               cwd=HERE.parent, env=env, capture_output=True, text=True)
        if r.returncode in (4, 5):  # the named test does not exist: that is no proof
            print(f"BROKEN  {name}: test not collected ({test})", flush=True)
            bad += 1
            continue
        if r.returncode == 0 and " skipped" in r.stdout and " passed" not in r.stdout:  # skipped = no proof either way
            print(f"SKIP  {name}: the test was skipped here ({test})", flush=True)
            continue
        failed = r.returncode != 0
        print(f"{'OK   ' if failed else 'SHAM '} {name}: reverted copy {'fails' if failed else 'STILL PASSES'} ({test})", flush=True)
        bad += 0 if failed else 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
