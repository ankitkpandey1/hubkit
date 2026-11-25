"""auto_open = false: the prompt hook and a card answer print a hint and open nothing; orders open by hand."""

from conftest import INTENTS

HOTFIX = "we need a hotfix for the billing bug now, finish it asap"
MERGE = "saying prioritize a review means to merge and do deploy, when did i say"


def _no_auto_open(env, tmp_path):
    text = INTENTS.read_text().replace("[intake]\n", "[intake]\nauto_open = false\n", 1)
    assert "auto_open = false" in text
    p = tmp_path / "intents-no-auto-open.toml"
    p.write_text(text)
    env.cfg.write_text(env.cfg.read_text().replace(str(INTENTS), str(p)))


def _opened(env):
    return [r["id"] for r in env.rows("orders.jsonl") if r["op"] == "open"]


def test_prompt_prints_a_hint_and_opens_nothing(env, tmp_path):
    _no_auto_open(env, tmp_path)
    env.bind()
    r = env.prompt(HOTFIX)
    assert r.returncode == 0
    assert "no order opened (auto_open is off)" in r.stdout and "hubkit order open --kind hotfix" in r.stdout
    assert _opened(env) == []


def test_reported_speech_about_merge_opens_nothing(env, tmp_path):
    _no_auto_open(env, tmp_path)
    env.bind()
    env.prompt(MERGE)
    assert _opened(env) == []


def test_card_answer_opens_nothing_either(env, tmp_path):
    _no_auto_open(env, tmp_path)
    env.bind()
    ev = {"hook_event_name": "PostToolUse", "session_id": "sess-hub", "tool_name": "AskUserQuestion"}
    env.hook("answer", {**ev, "tool_response": {"answers": {"Which?": HOTFIX}}})
    assert _opened(env) == []


def test_order_open_by_hand_still_works(env, tmp_path):
    _no_auto_open(env, tmp_path)
    env.bind()
    env.prompt(HOTFIX)
    r = env.run("order", "open", "--kind", "hotfix", "--quote", "review 7669 first")
    assert r.returncode == 0, r.stderr
    assert len(_opened(env)) == 1


def test_default_still_auto_opens(env):
    env.bind()
    env.prompt(HOTFIX)
    assert len(_opened(env)) == 1
