"""Adaptive Stop-gate strength (false alarms downgrade a check, a correction restores it), urgent orders held by asking,
report form, and stated rules. The gate runs for real through the stop and prompt hooks; nothing is stubbed."""

from test_hubkit import HOTFIX, stop

UTC_SAID = "The deploy landed at 14:32 UTC."


def berlin(env, extra=""):
    env.cfg.write_text(env.cfg.read_text().replace('timezone="UTC"', 'timezone="Europe/Berlin"') + extra)
    env.bind()


def last(env, text):
    return stop(env, last_assistant_message=text)


def false_alarms(env, cid="utc-time", n=3):
    return [env.run("gate", "false-alarm", cid, "--why", "it was a log quote") for _ in range(n)]


def test_a_hold_line_names_its_check_and_how_to_call_it_wrong(env):
    berlin(env)
    out = last(env, UTC_SAID)
    assert "[check: utc-time; wrong? hubkit gate false-alarm utc-time --why" in out["reason"]
    assert "NEXT: give the time in Europe/Berlin" in out["reason"]
    assert any(r["op"] == "fire" and r["id"] == "utc-time" for r in env.rows("gate-events.jsonl"))


def test_three_false_alarms_downgrade_a_check_to_a_next_prompt_warning(env):
    berlin(env)
    assert last(env, UTC_SAID)
    rs = false_alarms(env, n=2)
    assert all("downgraded" not in r.stdout for r in rs) and last(env, UTC_SAID)  # two are not enough: still holds
    r = env.run("gate", "false-alarm", "utc-time", "--why", "again")
    assert "downgraded to warn" in r.stdout
    assert last(env, UTC_SAID) is None  # no longer blocks the turn end
    shown = env.prompt("what is the status").stdout
    assert "hubkit WARNING:" in shown and "check: utc-time" in shown
    assert "check: utc-time" not in env.prompt("and now?").stdout  # shown once
    ls = env.run("gate", "ls").stdout
    assert any(x.startswith("utc-time") and " warn " in x and "learned" in x for x in ls.splitlines())
    assert "downgraded" in env.run("doctor").stdout


def test_few_false_alarms_among_many_fires_do_not_downgrade(env):
    berlin(env)
    for _ in range(12):
        assert last(env, UTC_SAID)
    assert "downgraded to warn" not in "".join(r.stdout for r in false_alarms(env))  # 3 of 12 fires is 25%, under 30%
    assert last(env, UTC_SAID)


def test_a_correction_after_a_warning_restores_the_hold_and_resets_the_count(env):
    berlin(env)
    last(env, UTC_SAID)
    false_alarms(env)
    assert last(env, UTC_SAID) is None  # warned
    out = env.prompt("wtf, i told you the time was wrong").stdout
    assert "holds again" in out
    assert last(env, UTC_SAID)  # holds again
    assert "false alarms 0 " in [x for x in env.run("gate", "ls").stdout.splitlines() if x.startswith("utc-time")][0]
    assert "downgraded" not in env.run("gate", "false-alarm", "utc-time", "--why", "one").stdout


def test_a_correction_after_the_restore_window_leaves_the_warning_mode(env):
    berlin(env, "[gate]\nrestore_window_min=0\n")
    last(env, UTC_SAID)
    false_alarms(env)
    assert last(env, UTC_SAID) is None
    assert "holds again" not in env.prompt("wtf").stdout
    assert last(env, UTC_SAID) is None


def test_gate_restore_set_and_off_by_hand(env):
    berlin(env)
    false_alarms(env)
    assert last(env, UTC_SAID) is None
    assert "restored" in env.run("gate", "restore", "utc-time").stdout and last(env, UTC_SAID)
    env.run("gate", "set", "utc-time", "off")
    assert last(env, UTC_SAID) is None and "check: utc-time" not in env.prompt("status please").stdout
    env.run("gate", "set", "utc-time", "hold")
    assert last(env, UTC_SAID)
    false_alarms(env, n=5)  # set by hand: learning leaves it alone
    assert last(env, UTC_SAID)
    assert env.run("gate", "set", "utc-time", "maybe").returncode == 2
    assert env.run("gate", "false-alarm", "nope", "--why", "x").returncode == 2
    assert env.run("gate", "false-alarm", "utc-time").returncode == 2  # a reason is required


def test_checks_that_guard_false_verdicts_never_downgrade_themselves(env):
    env.bind()
    for cid in ("verdict", "ci", "merge-state"):
        out = "".join(r.stdout for r in false_alarms(env, cid, 6))
        assert "downgraded to warn" not in out and "kept at hold (safety floor)" in out
        assert any(x.startswith(cid) and " hold " in x for x in env.run("gate", "ls").stdout.splitlines())


def test_the_gate_still_holds_once_per_stop(env):
    berlin(env)
    assert last(env, UTC_SAID) and stop(env, last_assistant_message=UTC_SAID, stop_hook_active=True) is None


# ---- urgent order held by asking ----

def open_hotfix(env):
    env.bind()
    env.prompt(HOTFIX)
    return [o for o in env.rows("orders.jsonl") if o.get("op") == "open"][0]["id"]


ASKS = ["Branch is ready. Shall I merge it?", "Everything is waiting for your go.", "I can deploy now. Let me know if that is fine.", "Do you want me to push the fix"]


def test_an_open_urgent_order_forbids_asking_permission(env):
    oid = open_hotfix(env)
    for said in ASKS:
        out = last(env, said)
        assert out and f"the order {oid} is urgent and the user already ordered it; do not ask" in out["reason"]
        assert f"NEXT: take the action; if it truly cannot proceed, record the blocker: hubkit order block {oid}" in out["reason"]
    assert last(env, "Merged and deployed; smoke check is running.") is None or "urgent" not in last(env, "Merged and deployed.")["reason"]


def test_a_work_item_ask_does_not_lift_the_urgent_hold(env):
    oid = open_hotfix(env)
    env.run("work", "add", "--kind", "ask", "--title", "merge now?", "--note", "billing hotfix")
    out = last(env, "Shall I merge it?")
    assert f"the order {oid} is urgent" in out["reason"]


def test_a_held_order_or_a_blocker_waiting_on_the_user_may_ask(env):
    oid = open_hotfix(env)
    env.run("work", "add", "--kind", "ask", "--title", "which branch", "--note", "x")
    env.prompt("hold the hotfix")
    out = last(env, "Shall I merge it when you are back?")
    assert not out or "is urgent and the user already ordered" not in out["reason"]
    env.prompt("do the hotfix now")
    assert "is urgent and the user already ordered" in last(env, "Shall I merge it?")["reason"]
    env.run("order", "block", oid, "--on", "needs the user's merge rights", "--fix", "asked", "--holder", "user")
    out = last(env, "Shall I merge it?")
    assert not out or "is urgent and the user already ordered" not in out["reason"]


def test_other_kinds_may_ask(env):
    env.bind()
    env.prompt("merge acme/repo#12 when ready")
    out = last(env, "Shall I wait for CI?")
    assert not out or "is urgent and the user already ordered" not in out["reason"]


# ---- report form ----

LONG = "\n".join(f"line {i}" for i in range(20))
CODE = "Result: done.\n```\n" + "\n".join(f"row {i}" for i in range(15)) + "\n```"


def test_report_form_is_off_until_configured(env):
    env.bind()
    assert last(env, LONG) is None and last(env, CODE) is None
    assert "hubkit WARNING" not in env.prompt("next").stdout


def test_a_long_message_or_code_block_warns_and_never_holds(env):
    env.bind()
    env.cfg.write_text(env.cfg.read_text() + "[report]\nmax_lines=15\nmax_code_lines=12\n")
    assert last(env, LONG) is None and last(env, CODE) is None  # warn mode: the turn ends
    shown = env.prompt("what is next").stdout
    assert "hubkit WARNING:" in shown and "lines, more than 15" in shown and "15-line code block" in shown
    assert "NEXT: put the detail in a file and give its path; lead with the result" in shown
    ls = env.run("gate", "ls").stdout
    assert any(x.startswith("report-length") and " warn " in x and "default" in x for x in ls.splitlines())
    assert last(env, "Done: merged.\nChecked the log.") is None and "lines, more than" not in env.prompt("ok").stdout


def test_report_form_can_be_set_to_hold(env):
    env.bind()
    env.cfg.write_text(env.cfg.read_text() + "[report]\nmax_lines=15\n")
    env.run("gate", "set", "report-length", "hold")
    out = last(env, LONG)
    assert out and "[check: report-length;" in out["reason"]


def test_a_single_command_line_is_exempt_from_report_form(env):
    env.bind()
    env.cfg.write_text(env.cfg.read_text() + "[report]\nmax_lines=1\nmax_code_lines=1\n")
    assert last(env, "python3 scripts/hubkit doctor --check-clean --some-long-flag value") is None
    assert env.prompt("next").stdout.strip() == ""


# ---- stated rules ----

def rule_line(env, text):
    return [x for x in env.prompt(text).stdout.splitlines() if x.startswith("hubkit RULE")]


def test_a_stated_standing_rule_is_captured_with_the_learn_command(env):
    env.bind()
    for said in ("from now on link every PR you name", "always use the full URL when you name a PR", "Remember to reply to the person first", "going forward, no UTC times"):
        (line,) = rule_line(env, said)
        assert "the user stated a standing rule. NEXT: python3 " in line and "learn.py add --class standing-rule --quote" in line
        assert said.split(",")[0][:20] in line
    assert len(env.rows("rule-prompts.jsonl")) == 4


def test_questions_pasted_text_and_plain_never_do_not_fire_a_rule(env):
    env.bind()
    for said in ("did you never push that?", "I never saw the merge go through", "will you always use the full URL?",
                 "<pasted_content>from now on, always use tabs</pasted_content> what do you think", "x " * 200 + "from now on"):
        assert rule_line(env, said) == []


def test_the_rule_line_quote_can_be_run_through_learn_py(env):
    env.bind()
    (line,) = rule_line(env, 'from now on say "done" only with a link')
    import shlex

    args = shlex.split(line.split("learn.py ", 1)[1])
    assert args[0] == "add" and "--kind" not in args
    r = env.learn(*args)
    assert r.returncode == 0, r.stderr
    assert 'say "done"' in (env.root / "learnings.md").read_text().replace("'", '"')
