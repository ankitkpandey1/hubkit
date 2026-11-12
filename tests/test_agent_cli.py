"""The CLI as an AI agent uses it: --json views, stable exit codes, NEXT on every error, `hubkit next`, help examples.

Everything runs as real processes on scratch state. No fake: these verbs read only hubkit's own files.
"""

import json
import shlex
import sys

from conftest import SCRIPTS  # noqa: F401  (puts the scripts on sys.path)
from test_hubkit import HOTFIX, order_ids
from test_requests import ASK, age

PR = "https://github.com/acme/repo/pull/12"


def doc(r):
    """The whole of stdout must be one JSON document."""
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_json_views_are_one_document_with_stable_keys(env):
    env.bind()
    env.prompt(HOTFIX)
    env.prompt(ASK)
    env.run("work", "add", "--kind", "active", "--title", "triage")
    keys = {
        ("order", "ls"): "orders", ("work", "ls"): "work", ("req", "ls"): "requests", ("mine",): "lines",
        ("peers", "ls"): "peers", ("inbound", "ls"): "lines", ("digest",): "lines", ("gate", "ls"): "lines",
        ("standing", "ls"): "rules", ("how", "ls"): "playbooks", ("status",): "orders", ("doctor",): "lines",
    }
    for args, key in keys.items():
        d = doc(env.run(*args, "--json"))
        assert key in d, args
    assert [o["id"] for o in doc(env.run("order", "ls", "--json"))["orders"]] == order_ids(env)
    assert doc(env.run("order", "status", order_ids(env)[0], "--json"))["orders"][0]["kind"] == "hotfix"
    assert [i["kind"] for i in doc(env.run("req", "ls", "--json"))["requests"]] == ["request"]
    assert any(i["title"] == "triage" for i in doc(env.run("work", "ls", "--json"))["work"])
    assert doc(env.run("doctor", "--json"))["ok"] is True
    assert env.run("order", "ls").stdout.startswith(order_ids(env)[0])


def test_exit_codes_not_found_refused_usage(env):
    env.bind()
    env.prompt(HOTFIX)
    oid = order_ids(env)[0]
    assert env.run("order", "status", "O-nope").returncode == 4
    assert env.run("req", "done", "W99", "--evidence", "merged in abc1234 and checked").returncode == 4
    assert env.run("how", "show", "nope").returncode == 4
    assert env.run("order", "close", oid).returncode == 3  # valid, but no evidence
    assert env.run("order", "close").returncode == 2  # no id
    assert env.run("order", "bogus").returncode == 2
    env.prompt(ASK)
    rid = [i["id"] for i in json.loads((env.state / "work.json").read_text()) if i["kind"] == "request"][0]
    assert env.run("work", "done", rid).returncode == 3  # a request closes only through req done
    assert env.run("req", "done", rid).returncode == 2  # evidence missing is usage


def test_json_error_document_on_stdout_and_stderr_line(env):
    r = env.run("order", "status", "O-nope", "--json")
    assert r.returncode == 4
    d = json.loads(r.stdout)
    assert d["code"] == 4 and d["next"] == "hubkit order ls" and "O-nope" in d["error"]
    assert r.stderr.splitlines()[-1] == "NEXT: hubkit order ls"
    bad = env.run("order", "bogus", "--json")
    assert bad.returncode == 2 and json.loads(bad.stdout)["code"] == 2


def test_every_refusal_ends_with_a_next_line(env):
    env.bind()
    env.prompt(HOTFIX)
    env.prompt(ASK)
    oid = order_ids(env)[0]
    table = [
        ("order", "status", "O-nope"), ("order", "close"), ("order", "close", oid), ("order", "cancel", oid), ("order", "block", oid),
        ("order", "result", oid), ("order", "hold", oid), ("req", "done", "W99"), ("req", "done"), ("req", "drop", "W99"),
        ("req", "note", "W99"), ("work", "done", "W99"), ("work", "done"), ("work", "block", "W99"), ("work", "add", "--kind", "bogus"),
        ("standing", "add"), ("standing", "drop", "S9"), ("standing", "drop"), ("gate", "set", "nope", "warn"), ("gate", "false-alarm"),
        ("gate", "false-alarm", "nope", "--why", "x"), ("inbound", "replied"), ("inbound", "replied", "W99", "--note", "x"),
        ("peers", "set"), ("how", "show", "nope"), ("how", "save", "x"), ("bogus",), ("order", "bogus"),
    ]
    for args in table:
        r = env.run(*args)
        assert r.returncode in (2, 3, 4), (args, r.returncode, r.stderr)
        last = r.stderr.strip().splitlines()[-1]
        assert last.startswith("NEXT: hubkit "), (args, r.stderr)
    work_id = [i["id"] for i in json.loads((env.state / "work.json").read_text()) if i["kind"] == "request"][0]
    r = env.run("work", "done", work_id)
    assert r.returncode == 3 and r.stderr.strip().splitlines()[-1] == f'NEXT: hubkit req done {work_id} --evidence "..."'
    assert env.run("order", "close", oid).stderr.strip().splitlines()[-1].startswith(f"NEXT: hubkit order close {oid} --evidence")


def test_next_is_nothing_due_on_an_empty_state(env):
    r = env.run("next")
    assert r.returncode == 0 and r.stdout.splitlines() == ["nothing is due", "NEXT: hubkit mine"]
    d = doc(env.run("next", "--json"))
    assert set(d) == {"kind", "id", "why", "next"} and d["kind"] == "none"


def test_next_orders_its_choices(env):
    env.bind()
    env.run("work", "add", "--kind", "ask", "--title", "which region?", "--note", "the deploy")
    d = doc(env.run("next", "--json"))
    assert d["kind"] == "waiting_on_user" and d["why"].startswith("waiting on the user: which region?") and d["id"].startswith("W")
    env.prompt(ASK)
    age(env, 100000)
    d = doc(env.run("next", "--json"))
    assert d["kind"] == "overdue_request" and "hubkit req done" in d["next"]  # an overdue request beats a question to the user
    env.prompt(HOTFIX)
    env.backdate(60)
    d = doc(env.run("next", "--json"))
    oid = order_ids(env)[0]
    assert d["kind"] == "overdue_order" and d["id"] == oid and d["next"]  # an overdue order beats the request
    env.run("order", "block", oid, "--on", "no host")
    env.backdate(30)
    d = doc(env.run("next", "--json"))
    assert d["kind"] == "blocker" and "hubkit order block" in d["next"] and d["id"] == oid  # a blocker beats everything
    text = env.run("next").stdout.splitlines()
    assert text[-1].startswith("NEXT: ") and text[0].startswith("blocker ")


def test_help_shows_examples_and_exit_codes_and_every_example_parses(env):
    sys.path.insert(0, str(SCRIPTS))
    from hubcore import agentio, cli

    top = env.run("--help").stdout
    assert "exit codes:" in top and "3  refused" in top and "hubkit next" in top
    parser = cli.build_parser()
    for verb, examples in agentio.EXAMPLES.items():
        assert len(examples) >= 1
        if verb == "hubkit":
            continue
        assert "examples:" in env.run(verb, "--help").stdout, verb
    ran = 0
    for examples in agentio.EXAMPLES.values():
        for ex in examples:
            argv = shlex.split(ex)[1:]
            parser.parse_args(argv)  # a flag or choice that does not exist exits 2 here
            if argv[:2] in (["order", "ls"], ["req", "ls"], ["work", "ls"], ["peers", "ls"], ["standing", "ls"], ["how", "ls"], ["gate", "ls"]) or argv[0] in ("next", "mine", "status", "doctor"):
                assert env.run(*argv).returncode == 0, ex
                ran += 1
    assert ran >= 12
