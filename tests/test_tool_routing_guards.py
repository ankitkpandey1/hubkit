"""Warn-only tool and routing guards: hub inline edits, unasked reviews, yes before the draft, wrong account, one agent per host,
review report completeness, and two small fixes (work done on a request, draft regexes)."""

import json

from test_hubkit import PR1, age_routed, fake_gh, routed_items, send
from test_peer_reports import deliver, peer_entry
from test_reportcheck import ptool

PR2 = "https://github.com/acme/repo/pull/102"
LONG = " Done-when: the change is pushed and the tests pass. Output: at most 15 lines. Estimate: 20m. Do not print secrets."


def conf(env, text):
    env.cfg.write_text(env.cfg.read_text() + text)


# ---- 1. the hub editing files itself ----

def test_the_hub_editing_a_file_itself_is_warned_once_per_turn_and_notes_and_scratch_are_not(env):
    env.bind()
    env.prompt("fix the retry bug")
    out = ptool(env, "Edit", {"file_path": "/home/someone/app/main.py"})
    assert "INLINE-EDIT" in out and "/home/someone/app/main.py" in out and "NEXT: route it to a worker" in out
    assert ptool(env, "Write", {"file_path": "/home/someone/app/other.py"}) == ""  # once per turn
    env.prompt("and the second one too")
    assert "INLINE-EDIT" in ptool(env, "NotebookEdit", {"notebook_path": "/home/someone/app/n.ipynb"})  # a new turn warns again
    env.prompt("one more")
    for ok in ("/home/someone/notes/today.md", "/tmp/scratch/x.py", str(env.state / "learnings.md"), str(env.state / "x.json")):
        assert ptool(env, "Edit", {"file_path": ok}) == "", ok


def test_inline_edit_warning_can_be_switched_off_and_the_ok_paths_are_config(env):
    conf(env, '[guards]\ninline_ok_paths=["/srv/scratch/*"]\n')
    env.bind()
    assert ptool(env, "Edit", {"file_path": "/srv/scratch/a.py"}) == ""
    assert "INLINE-EDIT" in ptool(env, "Edit", {"file_path": "/home/someone/a.md"})  # the configured list replaces the default (*.md)


def test_inline_edits_off_is_silent_and_the_hubs_own_dirs_and_temp_dir_are_always_ok(env):
    conf(env, '[guards]\ninline_edits="off"\n')
    env.bind()
    assert ptool(env, "Edit", {"file_path": "/home/someone/a.py"}) == ""


def test_the_hubs_state_dir_and_the_temp_dir_are_allowed_without_any_glob(env):
    mytmp = env.root / "mytmp"
    mytmp.mkdir()
    conf(env, "[guards]\ninline_ok_paths=[]\n")
    env.bind()

    def edit(path):
        r = env.hook("pretool", {"session_id": "sess-hub", "tool_name": "Edit", "tool_input": {"file_path": str(path)}}, TMPDIR=str(mytmp))
        return "INLINE-EDIT" in r.stdout

    assert not edit(env.state / "x.json") and not edit(mytmp / "y.py")
    env.prompt("next turn")
    assert edit("/home/someone/z.py")


# ---- 2. a review nobody asked for ----

def test_a_review_brief_on_a_pr_nobody_named_is_warned_and_a_typed_word_clears_it(env):
    fake_gh(env, head="a" * 40)  # gh is a local stand-in: the tests cannot reach GitHub
    env.bind()
    r = send(env, "peer-a", f"Please review {PR1} and report a verdict.{LONG}", "review 101")
    assert "NO-ASK-REVIEW" in r.stdout and "#101" in r.stdout and "NEXT: say who asked, or do not send it" in r.stdout
    env.prompt("please get 102 reviewed")
    r = send(env, "peer-b", f"Please review {PR2} and report a verdict.{LONG}", "review 102")
    assert "NO-ASK-REVIEW" not in r.stdout


def test_a_review_brief_is_not_warned_when_an_open_order_or_work_item_names_the_pr(env):
    fake_gh(env, head="a" * 40)
    env.bind()
    env.run("work", "add", "--kind", "active", "--title", "reply to the ask about 101")
    r = send(env, "peer-a", f"Please review {PR1} and report a verdict.{LONG}", "review 101")
    assert "tracking" in r.stdout and "NO-ASK-REVIEW" not in r.stdout


# ---- 3. a yes tied to the draft, the sending account ----

def test_a_yes_typed_before_the_draft_does_not_approve_it(env):
    env.bind()
    def cmd(n):  # a different command each time: an identical one is a different warning (repeat)
        return {"command": f"gh pr comment 5 --body hello{n}"}

    env.prompt("ok go ahead with the rest")
    assert ptool(env, "Bash", cmd(1)) == ""  # no draft item: any recent yes counts, as before
    env.prompt("draft a reply to the invoice mail")
    assert "no typed yes" in ptool(env, "Bash", cmd(2))
    env.prompt("make the draft shorter")
    assert "no typed yes" in ptool(env, "Bash", cmd(3))
    env.prompt("yes, send it")
    assert ptool(env, "Bash", cmd(4)) == ""
    env.prompt("actually make the draft friendlier")  # a changed draft needs a fresh yes
    assert "no typed yes" in ptool(env, "Bash", cmd(5))


def test_a_post_matching_an_account_rule_without_its_require_is_warned(env):
    conf(env, '[[accounts.rules]]\nmatch = "\\\\bgh\\\\s+pr\\\\s+comment\\\\b"\nrequire = "GH_TOKEN="\nhint = "prefix the command with GH_TOKEN=<the user\'s token>"\n')
    env.bind()
    env.prompt("yes go")
    out = ptool(env, "Bash", {"command": "gh pr comment 5 --body hi"})
    assert "this post would go from the wrong account" in out and "NEXT: prefix the command with GH_TOKEN=" in out
    assert ptool(env, "Bash", {"command": "GH_TOKEN=abc gh pr comment 5 --body hi"}) == ""
    assert ptool(env, "Bash", {"command": "gh pr view 5"}) == ""


# ---- 4. one agent per host ----

def test_a_second_owner_on_a_busy_host_is_warned_and_the_host_is_listed(env):
    conf(env, '[guards]\nhosts=["host-a", "host-b"]\n')
    fake_gh(env, head="b" * 40)
    env.bind()
    env.prompt("fix 101 and 102 on the hosts")
    r = send(env, "peer-a", f"Fix {PR1} by running the suite on host-a.{LONG}", "fix 101")
    assert "HOST-BUSY" not in r.stdout
    (item,) = routed_items(env)
    assert item["hosts"] == ["host-a"]
    r = send(env, "peer-b", f"Fix {PR2} by running the suite on host-a.{LONG}", "fix 102")
    assert f"host-a is already in use by peer-a ({item['id']})" in r.stdout and "NEXT: pick a free host or wait" in r.stdout
    assert f"hosts in use: host-a ({item['id']} peer-a), host-a ({routed_items(env)[1]['id']} peer-b)" in env.run("peers", "ls").stdout
    r = send(env, "peer-c", f"Fix PR #103 on host-b.{LONG}", "fix 103")
    assert "HOST-BUSY" not in r.stdout


# ---- 5. a reviewer's report shows the steps the role requires ----

def test_a_review_report_lacking_a_required_step_is_flagged_until_a_complete_one_arrives(env):
    conf(env, '[roles.reviewer]\nreport_must_include=["ran the tests"]\n')
    fake_gh(env, head="c" * 40)
    env.bind()
    env.prompt("get 101 reviewed")
    send(env, "peer-a", f"Please review {PR1} and report a verdict.{LONG}", "review 101")
    age_routed(env, 40)
    out = deliver(env, peer_entry("peer-a", "Review of PR 101 is done: approved, looks fine.", ago=20))
    line = next(x for x in out.splitlines() if "REPORT-INCOMPLETE" in x)
    assert "peer-a's review report on #101 does not show: ran the tests" in line and "NEXT: ask peer-a for them before relaying the verdict" in line
    assert "REPORT-INCOMPLETE: peer-a's review report on #101" in env.run("tick", "--no-notify").stdout
    out = deliver(env, peer_entry("peer-a", "Review of PR 101 is done again: we ran the tests, approved.", ago=5))
    assert "REPORT-INCOMPLETE" not in out


def test_a_report_for_a_role_with_no_requirement_is_not_flagged(env):
    fake_gh(env, head="c" * 40)
    env.bind()
    env.prompt("get 101 reviewed")
    send(env, "peer-a", f"Please review {PR1} and report a verdict.{LONG}", "review 101")
    age_routed(env, 40)
    assert "REPORT-INCOMPLETE" not in deliver(env, peer_entry("peer-a", "Review of PR 101 is done: approved.", ago=20))


# ---- 6. small fixes ----

def test_work_done_refuses_a_request_item_and_points_to_req_done(env):
    env.bind()
    env.prompt("delete the stale branches")
    (rid,) = [i["id"] for i in json.loads((env.state / "work.json").read_text()) if i["kind"] == "request"]
    r = env.run("work", "done", rid)
    assert r.returncode == 3 and f'NEXT: hubkit req done {rid} --evidence "..."' in r.stderr
    assert rid in (env.state / "work.json").read_text()  # still open
    env.run("work", "add", "--kind", "active", "--title", "plain item")
    assert env.run("work", "done", "W2" if rid == "W1" else "W1").returncode == 0


def test_draft_and_discard_words_come_from_config(env):
    conf(env, '[tools]\ndraft_regex="(?i)\\\\bcompose\\\\b"\ndiscard_regex="(?i)\\\\bbin it\\\\b"\n')
    env.bind()
    env.prompt("write a draft of the standup")
    titles = lambda: [i["title"] for i in json.loads((env.state / "work.json").read_text())] if (env.state / "work.json").exists() else []  # noqa: E731
    assert not any(t.startswith("draft: ") for t in titles())  # the built-in word is replaced
    env.prompt("compose the standup note")
    assert any(t.startswith("draft: ") for t in titles())
    env.prompt("discard the draft")
    assert any(t.startswith("draft: ") for t in titles())
    env.prompt("bin it")
    assert not any(t.startswith("draft: ") for t in titles())
