"""Asks from other people become `reply` items; none is lost, none is double-counted, an old one alerts."""

import json
import time

LINES = (
    "1750735 · dana · #eng/work > API · addressed the comments, can you review again? https://github.com/acme/repo/pull/101 · reply\n"
    "1751039 · erin · #hr > leaves · travelling today · todo\n"
    "garbage line\n"
)


def items(env):
    p = env.state / "work.json"
    return [i for i in json.loads(p.read_text()) if i["kind"] == "reply"] if p.exists() else []


def test_inbound_asks_open_one_reply_item_each_once(env, tmp_path):
    f = tmp_path / "inbox.txt"
    f.write_text(LINES, encoding="utf-8")
    out = env.run("inbound", "ingest", "--file", str(f)).stdout
    assert out.count("reply owed to") == 2 and "dana" in out
    assert [i["owner"] for i in items(env)] == ["dana", "erin"] and items(env)[0]["prs"]
    assert env.run("inbound", "ingest", "--file", str(f)).stdout.strip() == "nothing new"
    assert len(items(env)) == 2


def test_a_reply_owed_past_the_limit_alerts_once_and_closes_with_work_done(env, tmp_path):
    f = tmp_path / "inbox.txt"
    f.write_text(LINES, encoding="utf-8")
    env.run("inbound", "ingest", "--file", str(f))
    assert "reply owed" not in env.run("tick", "--no-notify").stdout
    w = env.state / "work.json"
    rows = json.loads(w.read_text())
    for r in rows:
        r["added"] = time.time() - 61 * 60
    w.write_text(json.dumps(rows))
    assert "reply owed to dana" in env.run("tick", "--no-notify").stdout
    assert env.run("work", "done", "W1", "--note", "answered").returncode == 0
    assert "reply owed to dana" not in env.run("tick", "--no-notify").stdout
    assert env.run("inbound", "ingest", "--file", str(f)).stdout.strip() == "nothing new"  # an answered ask is not reopened
