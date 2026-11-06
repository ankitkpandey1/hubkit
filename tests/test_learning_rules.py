"""A rule that did not work is marked; a new rule must pass a deterministic check and not break an earlier one; old rules expire."""

import time

from test_hubkit import _add_learning


def addressed(env, cls, check="", day=None):
    day = day or time.strftime("%Y-%m-%d", time.gmtime())
    _add_learning(env, cls, day)
    ov = env.root / "overlay.md"
    ov.write_text(ov.read_text() if ov.exists() else "# rules\n")
    assert env.learn("promote", cls, "--gate", "G1", "--rule", f"rule for {cls}").returncode == 0
    extra = ["--check", check] if check else []
    return env.learn("address", "--class", cls, "--how", "promoted", "--date", day, *extra)


def test_a_class_seen_again_after_it_was_addressed_is_marked_ineffective(env):
    env.bind()
    assert addressed(env, "again-one", day="2026-01-01").returncode == 0
    r = env.learn("add", "--class", "again-one", "--gate", "G1", "--quote", "again", "--date", "2026-01-05")
    assert "INEFFECTIVE" in r.stdout
    today = time.strftime("%Y-%m-%d", time.gmtime())
    env.learn("add", "--class", "again-one", "--gate", "G1", "--quote", "once more", "--date", today)
    env.run("tick", "--no-notify")
    assert "again-one (INEFFECTIVE" in env.hook("session", {"session_id": "sess-hub", "source": "compact"}).stdout


def test_a_failing_check_refuses_the_address_and_a_passing_one_is_stored(env):
    env.bind()
    r = addressed(env, "chk-one", check="false")
    assert r.returncode == 2 and "check for chk-one does not pass" in r.stderr
    ok = env.learn("address", "--class", "chk-one", "--how", "promoted", "--check", "true")
    assert ok.returncode == 0 and "prose only" not in ok.stdout


def test_a_new_rule_that_breaks_an_earlier_check_is_refused(env, tmp_path):
    env.bind()
    flag = tmp_path / "flag"
    flag.write_text("x")
    assert addressed(env, "first-rule", check=f"test -f {flag}").returncode == 0
    flag.unlink()
    r = addressed(env, "second-rule", check="true")
    assert r.returncode == 2 and "breaks the check of first-rule" in r.stderr


def test_review_lists_prose_only_and_expired_rules(env):
    env.bind()
    addressed(env, "old-prose", day="2026-01-01")
    out = env.learn("review", "--day", "2026-06-01").stdout
    assert "expire: old-prose (addressed 2026-01-01, not seen since)" in out and "prose-only: old-prose" in out
    assert "expire: -" in env.learn("review", "--day", "2026-02-01").stdout
