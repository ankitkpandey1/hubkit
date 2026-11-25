"""One check per mistake of the 2026-11-16 hub session (see HUB-MISTAKES-2026-11-16.md): each test fails without its check."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from conftest import INTENTS, SCRIPTS
from test_guards2 import pre
from test_hubkit import PR1, PR2, send, start_watch, stop
from test_lessons import prompts

sys.path.insert(0, str(SCRIPTS))
from hubcore import claims  # noqa: E402


def cfg_add(env, text):
    env.cfg.write_text(env.cfg.read_text() + text)


def transcript(env, calls, typed="check it"):
    """A transcript: one typed prompt, then one assistant tool call per (name, input)."""
    rows = [{"type": "user", "message": {"content": typed}}]
    for name, inp in calls:
        rows.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "t" + str(abs(hash(json.dumps(inp, sort_keys=True)))), "name": name, "input": inp}
                    ]
                },
            }
        )
        rows.append(
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "ok"}]},
            }
        )
    p = env.transcripts / "sess-hub.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return str(p)


def card(env, question, answer):
    env.hook(
        "answer",
        {
            "hook_event_name": "PostToolUse",
            "session_id": "sess-hub",
            "tool_name": "AskUserQuestion",
            "tool_response": {"answers": {question: answer}},
        },
    )


# ---- M9: a release log line that needs a person is told at once ----

LOG_RUN = "16 Nov 14:52:59 [7/9] prod: START, ETA 20 min\n16 Nov 14:53:00     NEEDS A PERSON: the hotfix adds a onetime script\n16 Nov 14:53:00 [7/9] prod: FAILED (1s): NEEDS A PERSON: the hotfix adds a onetime script\n"


def release_log(env, text):
    d = env.root / "logs" / "hotfix-7669"
    d.mkdir(parents=True, exist_ok=True)
    (d / "release.log").write_text(text)
    return d / "release.log"


def test_a_needs_a_person_log_line_alerts_at_once_and_holds_the_stop_gate_until_acked(
    env,
):
    cfg_add(env, f'[watcher]\nlog_globs=["{env.root}/logs/*/release.log"]\n')
    env.bind()
    release_log(env, LOG_RUN)
    out = env.run("tick", "--no-notify").stdout
    assert "NEEDS A PERSON" in out and "hubkit log ack log:hotfix-7669:" in out
    held = stop(env)
    assert "hubkit LOG" in held["reason"] and "he has not been told" in held["reason"]
    key = next(
        w for w in held["reason"].split() if w.startswith("log:hotfix-7669:")
    ).rstrip(".,;")
    assert env.run("log", "ack", key).returncode == 0
    assert stop(env) is None


def test_a_log_that_went_on_after_the_failure_raises_nothing(env):
    cfg_add(env, f'[watcher]\nlog_globs=["{env.root}/logs/*/release.log"]\n')
    env.bind()
    release_log(
        env,
        LOG_RUN
        + "16 Nov 15:19:26 hotfix svc#7669: state and log in /x\n16 Nov 15:19:28 [7/9] prod: START, ETA 20 min\n",
    )
    assert "NEEDS A PERSON" not in env.run("tick", "--no-notify").stdout
    assert stop(env) is None


# ---- M5: a brief that claims his authority must quote him ----


def test_a_brief_that_says_this_is_his_go_without_his_words_is_warned(env):
    env.bind()
    r = send(
        env,
        "peer-a",
        "Ship the hotfix. Tester ordered it as a hotfix; this brief is his go.",
        "ship",
    )
    assert "claims his authority" in r.stdout and "A brief is not his go" in r.stdout


def test_a_brief_that_quotes_words_he_typed_is_not_warned(env):
    env.bind()
    prompts(env, "merge 7669 now please")
    r = send(
        env,
        "peer-a",
        'Tester ordered it: "merge 7669 now please". Merge 7669.',
        "merge",
    )
    assert "claims his authority" not in r.stdout
    r = send(
        env, "peer-b", 'Tester ordered it: "ship it to prod right now". Do it.', "ship"
    )
    assert "claims his authority" in r.stdout  # a quote he never typed does not count


# ---- M4: merge, release or prod with none of his words ordering it ----

HOTFIX_BRIEF = "Task: ship hotfix https://github.com/acme/repo/pull/7669 to prod.\n1. Run `python3 .claude/skills/do-release/scripts/release.py hotfix repo#7669 --go` in the background."


def test_an_agent_told_to_run_release_on_the_word_hotfix_holds_the_stop_gate(env):
    env.bind()
    start_watch(env)
    env.prompt("prjan pr is hotfix so we must priotize it")
    held = stop(
        env, transcript_path=transcript(env, [("Agent", {"prompt": HOTFIX_BRIEF})])
    )
    assert (
        "none of his typed words or card answers" in held["reason"]
        and "release.py" in held["reason"]
    )
    assert (
        stop(
            env, transcript_path=transcript(env, [("Agent", {"prompt": HOTFIX_BRIEF})])
        )
        is None
    )  # told once per act


def test_a_bash_merge_with_no_order_is_held_and_a_typed_merge_naming_it_is_not(env):
    env.bind()
    start_watch(env)
    calls = [("Bash", {"command": "gh pr merge 7669 -R acme/repo --merge"})]
    assert (
        "none of his typed words"
        in stop(env, transcript_path=transcript(env, calls))["reason"]
    )
    env.prompt("merge 7669")
    calls = [
        (
            "Bash",
            {
                "command": (
                    "gh pr merge 7669 -R acme/repo --merge --match-head-commit abc"
                )
            },
        )
    ]
    assert (
        stop(
            env,
            transcript_path=transcript(
                env, [("Bash", {"command": "gh pr merge 7670 -R acme/repo"})]
            ),
        )
        is not None
    )  # another PR
    assert stop(env, transcript_path=transcript(env, calls, typed="merge 7669")) is None


def test_a_card_answer_naming_the_act_orders_it_and_a_refusal_or_a_question_does_not(
    env,
):
    env.bind()
    calls = [("Bash", {"command": "python3 release.py hotfix repo#7669 --go"})]
    env.prompt("when did i say deploy?")
    card(env, "Deploy #7669?", "Don't deploy")
    assert stop(env, transcript_path=transcript(env, calls)) is not None
    card(env, "Prod for #7669?", "Ship #7669 to prod")
    again = [("Bash", {"command": "python3 release.py hotfix repo#7669 --go --resume"})]
    assert stop(env, transcript_path=transcript(env, again)) is None


# ---- M6, M7: a claim about a PR with no read of it; blaming his config unread ----


def test_what_a_pr_does_is_held_without_a_gh_read_of_it_and_passes_with_one(env):
    env.bind()
    msg = "#6651 (https://github.com/acme/repo/pull/6651) is superseded by #7672 (https://github.com/acme/repo/pull/7672), which makes API-token callers use the token's workspace."
    held = stop(env, last_assistant_message=msg, transcript_path=transcript(env, []))
    assert (
        "#7672" in held["reason"]
        and "no gh or git command of this turn read it" in held["reason"]
    )
    assert (
        stop(
            env,
            last_assistant_message=msg,
            transcript_path=transcript(
                env,
                [
                    ("Bash", {"command": "gh pr diff 7672 -R acme/repo"}),
                    ("Bash", {"command": "gh pr view 6651"}),
                ],
            ),
        )
        is None
    )
    other = stop(
        env,
        last_assistant_message=msg,
        transcript_path=transcript(env, [("Bash", {"command": "gh pr view 9999"})]),
    )
    assert other is not None  # reading a different PR proves nothing


def test_a_pr_listed_as_unhandled_is_held_without_a_read(env):
    env.bind()
    held = stop(
        env,
        last_assistant_message="Review ask #5820 may be unhandled.",
        transcript_path=transcript(env, []),
    )
    assert "#5820" in held["reason"]


def test_blaming_your_config_without_reading_the_rule_files_is_held(env):
    env.bind()
    msg = "Why it happened: your hub config said I should merge at once."
    assert (
        "read none of the hub's rule files"
        in stop(
            env,
            last_assistant_message=msg,
            transcript_path=transcript(env, [("Bash", {"command": "ls"})]),
        )["reason"]
    )
    assert (
        stop(
            env,
            last_assistant_message=msg,
            transcript_path=transcript(
                env,
                [
                    (
                        "Bash",
                        {
                            "command": (
                                "grep -n hotfix ~/.local/state/hubkit/learnings.md"
                            )
                        },
                    )
                ],
            ),
        )
        is None
    )


# ---- M3: a past event at a time that has not come ----


def test_a_past_event_at_a_later_clock_time_is_flagged(env):
    cfg = {"user": {"timezone": "UTC"}}
    now = 1_700_000_000 - (1_700_000_000 % 86400) + 14 * 3600 + 24 * 60  # 14:24 UTC
    assert "14:24" in claims.future_time(
        cfg, "Message 1770853 went out from your account at about 14:55 IST.", now
    )
    assert claims.future_time(cfg, "It went out at 14:21 IST.", now) is None
    assert claims.future_time(cfg, "The prod deploy is due by 15:39 IST.", now) is None


# ---- M12: asking him what the hub does itself ----


def test_asking_him_whether_to_start_a_box_is_warned_in_a_card_and_held_in_a_message(
    env,
):
    cfg_add(env, '[ask_guard]\nnames=["boxone"]\n')
    env.bind()
    card_in = {
        "questions": [
            {
                "question": "boxone: start it so 46 can finish, or keep it off?",
                "options": [{"label": "Start it (Recommended)"}, {"label": "Keep off"}],
            }
        ]
    }
    assert "Starting or stopping a box is the hub's job" in pre(
        env, "AskUserQuestion", **card_in
    )
    ok = {
        "questions": [
            {
                "question": "Merge #7676?",
                "options": [{"label": "Merge (Recommended)"}, {"label": "Hold"}],
            }
        ]
    }
    assert pre(env, "AskUserQuestion", **ok) == ""
    held = stop(
        env,
        last_assistant_message="Three decisions wait:\n1. **boxone:** start it so 46 can finish, or keep it off?\n2. Q-005?",
    )
    assert "asks the user" in held["reason"] and "boxone" in held["reason"]


# ---- M10: one PR to a person who is sent batches ----


def test_a_dm_naming_one_pr_while_others_are_live_is_warned(env):
    cfg_add(env, '[routed]\nbatch_to=["alice"]\n')
    env.bind()
    send(env, "peer-a", f"Full gated review of {PR1}", "review 101")
    one = f"mep draft --dm alice - <<< 'approve and merge: {PR2}'"
    assert "names one PR while #101 are also live" in pre(env, "Bash", command=one)
    both = f"mep draft --dm alice - <<< 'approve and merge: {PR1} {PR2}'"
    assert "names one PR" not in pre(env, "Bash", command=both)
    assert "names one PR" not in pre(
        env, "Bash", command=one.replace("--dm alice", "--dm someone")
    )


def test_the_draft_file_a_dm_reads_is_looked_into(env):
    cfg_add(env, '[routed]\nbatch_to=["alice"]\n')
    env.bind()
    send(env, "peer-a", f"Full gated review of {PR1}", "review 101")
    d = Path(tempfile.mkdtemp(prefix="dm"))
    (d / "alice4.txt").write_text(f"approve and merge: {PR2}\n")
    assert "names one PR" in pre(
        env, "Bash", command=f"S={d}; mep draft --dm alice - < $S/alice4.txt"
    )


# ---- M1: the watchers and the hub skill after a compaction ----


def test_a_session_start_with_no_hubkit_watch_prints_a_blocking_next(env):
    env.bind()
    out = env.hook("session", {"session_id": "sess-hub", "source": "compact"}).stdout
    assert "hubkit BLOCKING: no `hubkit watch` runs" in out
    start_watch(env)
    assert (
        "no `hubkit watch` runs"
        not in env.hook(
            "session", {"session_id": "sess-hub", "source": "compact"}
        ).stdout
    )


def test_a_session_start_with_no_inbox_watcher_prints_a_blocking_next_and_a_running_one_clears_it(
    env, tmp_path
):
    mep = tmp_path / "bin" / "mep"
    mep.parent.mkdir()
    mep.write_text("#!/bin/sh\nsleep 300\n")
    mep.chmod(0o755)
    cfg_add(env, f'[tools]\nmep="{mep}"\n')
    env.cfg.write_text(
        env.cfg.read_text().replace("[mep]\nenabled=false", "[mep]\nenabled=true")
    )
    env.bind()
    assert (
        "no inbox watcher runs"
        in env.hook("session", {"session_id": "sess-hub", "source": "compact"}).stdout
    )
    p = subprocess.Popen(
        [str(mep), "watch", "--cursor", "hub"],
        env={**os.environ, "PATH": f"{mep.parent}:{os.environ['PATH']}"},
    )
    env.procs.append(p)
    time.sleep(0.3)
    assert (
        "no inbox watcher runs"
        not in env.hook(
            "session", {"session_id": "sess-hub", "source": "compact"}
        ).stdout
    )


def test_every_bash_call_is_reminded_until_the_hub_skill_is_loaded_after_a_compaction(
    env,
):
    env.bind()
    assert pre(env, "Bash", command="ls") == ""
    env.hook("session", {"session_id": "sess-hub", "source": "compact"})
    assert "hub skill not loaded since compaction" in pre(env, "Bash", command="ls")
    env.hook(
        "skill",
        {
            "hook_event_name": "PostToolUse",
            "session_id": "sess-hub",
            "tool_name": "Skill",
            "tool_input": {"skill": "hub"},
        },
    )
    assert pre(env, "Bash", command="ls -a") == ""


# ---- M8: a playbook "rewritten" that was not ----


def steps(tmp_path, text, name="steps.txt"):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def test_replacing_a_playbook_with_identical_steps_fails_loudly(env, tmp_path):
    f = steps(tmp_path, "1. Review it.\n2. Report.\n")
    assert (
        env.run(
            "how",
            "save",
            "hotfix-pr",
            "--trigger",
            "hotfix",
            "--steps-file",
            f,
            "--quote",
            "review it",
        ).returncode
        == 0
    )
    r = env.run(
        "how",
        "save",
        "hotfix-pr",
        "--replace",
        "--trigger",
        "hotfix",
        "--steps-file",
        f,
        "--quote",
        "again",
    )
    assert (
        r.returncode == 2
        and "identical to the stored version" in r.stderr
        and "noclobber" in r.stderr
    )
    g = steps(tmp_path, "1. Review it.\n2. Report.\n3. Stop.\n", "new.txt")
    assert (
        env.run(
            "how",
            "save",
            "hotfix-pr",
            "--replace",
            "--trigger",
            "hotfix",
            "--steps-file",
            g,
            "--quote",
            "stop",
        ).returncode
        == 0
    )


def test_a_steps_file_older_than_the_stored_playbook_is_refused(env, tmp_path):
    assert (
        env.run(
            "how",
            "save",
            "pb",
            "--trigger",
            "x",
            "--steps-file",
            steps(tmp_path, "1. One.\n"),
            "--quote",
            "q",
        ).returncode
        == 0
    )
    old = steps(tmp_path, "1. Two.\n", "old.txt")
    os.utime(old, (1_000_000, 1_000_000))
    r = env.run(
        "how",
        "save",
        "pb",
        "--replace",
        "--trigger",
        "x",
        "--steps-file",
        old,
        "--quote",
        "q",
    )
    assert r.returncode == 2 and "older than the stored playbook" in r.stderr


def test_a_playbook_that_merges_or_deploys_needs_a_quote_of_his_naming_the_act(
    env, tmp_path
):
    f = steps(
        tmp_path, "1. Review.\n2. gh pr merge the PR.\n3. Run release.py hotfix --go.\n"
    )
    r = env.run(
        "how",
        "save",
        "hotfix-pr",
        "--trigger",
        "hotfix",
        "--steps-file",
        f,
        "--quote",
        "prioritize it",
    )
    assert r.returncode == 2 and "no --quote of his words names that act" in r.stderr
    assert (
        env.run(
            "how",
            "save",
            "hotfix-pr",
            "--trigger",
            "hotfix",
            "--steps-file",
            f,
            "--quote",
            "merge it and release",
        ).returncode
        == 0
    )
    safe = steps(
        tmp_path,
        "1. Review.\n2. STOP. No merge, no release.py, no deploy unless he types words naming it.\n",
        "safe.txt",
    )
    assert (
        env.run(
            "how",
            "save",
            "safe-pr",
            "--trigger",
            "safe",
            "--steps-file",
            safe,
            "--quote",
            "prioritize",
        ).returncode
        == 0
    )


# ---- M11: the prompt hook opening orders ----


def test_doctor_warns_while_auto_open_is_on_and_not_when_it_is_off(env, tmp_path):
    assert "auto_open is on" in env.run("doctor").stdout
    off = tmp_path / "off.toml"
    off.write_text(
        INTENTS.read_text().replace("[intake]\n", "[intake]\nauto_open = false\n", 1)
    )
    env.cfg.write_text(env.cfg.read_text().replace(str(INTENTS), str(off)))
    assert "auto_open is on" not in env.run("doctor").stdout


# ---- M2: a PR number with no link ----


def test_the_no_link_hold_hands_over_the_url_to_paste(env):
    cfg_add(env, '[routed]\nrepo="acme/repo"\n')
    env.bind()
    held = stop(env, last_assistant_message="The hotfix #7669 is merged.")
    assert (
        "with no link" in held["reason"]
        and "paste: https://github.com/acme/repo/pull/7669" in held["reason"]
    )
