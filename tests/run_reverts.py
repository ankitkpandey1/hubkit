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
    ("harness and peer text filter", "hubcore/store.py", "    return not any(m in squashed for m in HARNESS_MARKERS)", "    return True", "test_peer_and_harness_text_never_open_orders"),
    ("whole-text marker scan and look-alike hyphens", "hubcore/store.py", "    return not any(m in squashed for m in HARNESS_MARKERS)", "    return not any(m in t[:400] for m in HARNESS_MARKERS)", "test_peer_marker_far_into_text_and_lookalike_hyphens_are_not_human"),
    ("origin must be human", "hubcore/store.py", "    if origin is not None and origin != \"human\":\n        return False\n", "", "test_origin_must_be_human_when_present"),
    ("typed paste stays human", "hubcore/store.py", 're.sub(r"</?pasted_content[^>]*>", " ", _normalise(text))', "_normalise(text)", "test_typed_paste_is_still_human"),
    ("evidence required on close", "hubcore/orders.py", "            if problems:\n", "            if False:\n", "test_close_refused_without_evidence_then_accepted"),
    ("bound-hub session match", "hubcore/hooks.py", 'payload.get("session_id") != b["session_id"]', "False", "test_noop_outside_bound_hub"),
    ("headless no-op", "hubcore/hooks.py", 'bool(sess) and sess.get("kind") == "interactive" and ', "bool(sess) and ", "test_noop_in_headless_session"),
    ("hold/cancel verbs", "hubcore/orders.py", "if (hold or cancel or resume) and short:", "if False:", "test_hold_and_cancel_verbs_and_prompt_intents"),
    ("redact before write", "hubcore/store.py", "    row = redact_obj(row)\n", "", "test_append_row_redacts_even_when_the_caller_did_not"),
    ("hook never exits non-zero", "hubcore/hooks.py", "except BaseException as e:  # a hook", "except ZeroDivisionError as e:  # a hook", "test_hook_crash_exits_zero_logs_and_shows_one_line"),
    ("state not in git", "hubcore/store.py", "    if _inside_git(d):", "    if False:", "test_state_inside_git_repo_is_refused"),
    ("bind liveness", "hubcore/hooks.py", 'return bool(b) and proc_start(int(b["pid"])) == str(b["procStart"])', "return bool(b)", "test_dead_hub_frees_bind_and_session_start_reports_open_orders"),
    ("stop_hook_active", "hubcore/hooks.py", ' or payload.get("stop_hook_active")', "", "test_stop_guard_logs_only"),
    ("alert back-off", "hubcore/status.py", "    return min(1800.0, 600.0 * 2 ** max(n - 1, 0)) if n else 0.0", "    return 0.0", "test_watchdog_alerts_then_backs_off"),
    ("learnings lock", "hubcore/learnings.py", "    return store.locked(path.with_name(f\".{path.name}.lock\"))", "    import contextlib\n    return contextlib.nullcontext()", "test_learn_concurrent_adds_lose_nothing"),
    ("archive crash repair", "hubcore/status.py", "        orders.repair_archive()\n        apath", "        apath", "test_archive_crash_repair"),
    ("held order has no clock", "hubcore/status.py", '        if o["state"] != "open":  # held', '        if False:  # held', "test_hold_and_cancel_verbs_and_prompt_intents"),
    ("unconfirmed has no clock", "hubcore/status.py", '        if o["state"] != "open":  # held', '        if False:  # held', "test_unconfirmed_when_question_and_no_clock"),
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
    ("session hook silent outside hub", "hubcore/hooks.py", '    if not is_hub(cfg, payload):\n        return ""  # nothing', '    if False:\n        return ""  # nothing', "test_session_hook_writes_and_prints_nothing_outside_the_hub"),
    ("hook import failure exits 0", "hubkit", "        sys.exit(0)\n    raise\n", "    raise\n", "test_import_failure_in_a_hook_exits_zero"),
    ("unconfirmed expiry", "hubcore/status.py", 'if o["state"] == "unconfirmed" and age_min > ttl:', "if False:", "test_unconfirmed_order_expires_and_has_no_clock"),
    ("held-too-long alert", "hubcore/status.py", "            if (now - since) / 60 > held_max:", "            if False:", "test_held_order_raises_review_alert_after_limit"),
    ("cancel my meeting cancels nothing", "hubcore/orders.py", "    return live if bare and len(live) == 1 else []", "    return live", "test_cancel_without_order_object_cancels_nothing"),
    ("question does not open an order", "hubcore/orders.py", 'orderish = has("imperative", text) and not (question and not urgent)', "orderish = True", "test_question_with_order_word_opens_nothing"),
    ("glued glpat and AIza (no word boundary)", "hubcore/redact.py", 're.compile(r"glpat-', 're.compile(r"\\bglpat-', "test_glued_secrets_are_redacted"),
    ("import log redacted", "hubkit", "                    msg = redact(msg)[0]\n", "                    pass\n", "test_import_error_log_is_redacted_and_survives_a_broken_redactor"),
    ("import log survives broken redactor", "hubkit", "except BaseException:  # the redactor", "except ZeroDivisionError:  # the redactor", "test_import_error_log_is_redacted_and_survives_a_broken_redactor"),
    ("glpat pattern", "hubcore/redact.py", '    re.compile(r"glpat-[A-Za-z0-9_-]{15,}"),\n', "", "test_more_secret_shapes_are_redacted"),
    ("AIza pattern", "hubcore/redact.py", '    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),\n', "", "test_more_secret_shapes_are_redacted"),
    ("status.json redacted at write", "hubcore/store.py", "json.dumps(redact_obj(obj, 2000), indent=indent", "json.dumps(obj, indent=indent", "test_status_json_is_redacted_at_write"),
    ("redact then truncate", "hubcore/store.py", "    return redact(text)[0][:n]", "    return redact(text[:n])[0]", "test_cut_secret_does_not_leak_a_prefix"),
    ("blocking order lock", "hubcore/orders.py", '        with store.locked(self.sd / "lock"):  # blocking', '        with __import__("contextlib").nullcontext():  # blocking', "test_order_open_waits_for_a_held_lock_instead_of_skipping_it"),
    ("close row before archive row", "hubcore/orders.py", '            self._append(oid, "close", evidence=evidence)  # source of truth first\n            self._die_after("close")\n            self._archive(self.get(oid), "closed", {"evidence": evidence})', '            self._archive(self.get(oid), "closed", {"evidence": evidence})\n            self._die_after("close")\n            self._append(oid, "close", evidence=evidence)', "test_close_row_is_written_before_archive_and_crash_is_repaired"),
    ("check-clean scans tests and references", "hubcore/cli.py", '"__pycache__" not in p.parts and', '"__pycache__" not in p.parts and "tests" not in p.parts and "references" not in p.parts and', "test_check_clean_flags_a_term_and_passes_when_absent"),
    ("mep fallback dirs for cron PATH", "hubcore/status.py", '("~/.local/bin", "/usr/local/bin", "~/bin")', "()", "test_mep_found_with_cron_style_path"),
    ("mep stale-keep", "hubcore/status.py", 'if prev and prev.get("state") in ("ok", "stale"):', "if False:", "test_slow_mep_keeps_last_good_answer_not_unverified"),
    ("mep timeout default", "hubcore/config.py", '"mep": {"enabled": True, "timeout_s": 30}', '"mep": {"enabled": True, "timeout_s": 5}', "test_default_mep_timeout_covers_a_real_status_call"),
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
                print(f"BROKEN  {name}: pattern found {text.count(old)}x in {rel}")
                bad += 1
                continue
            f.write_text(text.replace(old, new))
            env = dict(os.environ, HUBKIT_SCRIPTS=str(copy))
            if rel.endswith(".toml"):
                env["HUBKIT_TEST_INTENTS"] = str(f)
            r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", T + test, "-p", "no:cacheprovider"],
                               cwd=HERE.parent, env=env, capture_output=True, text=True)
        failed = r.returncode != 0
        print(f"{'OK   ' if failed else 'SHAM '} {name}: reverted copy {'fails' if failed else 'STILL PASSES'} ({test})")
        bad += 0 if failed else 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
