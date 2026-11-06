"""The day's whole record: the user's PRs, peer session prompts, and one search over all of it."""

import json
import time

from test_hubkit import iso

SECRET = "AKIA" + "ABCDEFGHIJKLMNOP"


def fake_gh_by_verb(env, fail=False):
    """A gh stand-in that answers `search prs` and `pr view` differently (the one boundary the tests fake)."""
    gh = env.root / "gh"
    search = json.dumps([{"number": 7457, "title": "Add the helpdesk agent", "url": "https://github.com/acme/repo/pull/7457", "body": "Answers questions from the docs.", "createdAt": "x"}])
    view = json.dumps({"files": [{"path": "api/agents/helpdesk/handlers.py"}]})
    (env.root / "search.json").write_text(search)
    (env.root / "view.json").write_text(view)
    body = 'echo boom >&2; exit 1' if fail else f'case "$1" in search) cat {env.root / "search.json"};; pr) cat {env.root / "view.json"};; esac'
    gh.write_text(f"#!/bin/sh\n{body}\n")
    gh.chmod(0o755)
    env.cfg.write_text(env.cfg.read_text() + f'[tools]\ngh="{gh}"\n')


def peer_session(env, name="peer-e8", text="build the helpdesk agent for me"):
    (env.sessions / "9991.json").write_text(json.dumps({"pid": 9991, "sessionId": "peer-sid-1", "name": name}))
    lines = [json.dumps({"type": "user", "timestamp": iso(time.time() - 60), "message": {"content": text}})]
    (env.transcripts / "peer-sid-1.jsonl").write_text("\n".join(lines) + "\n")


def test_find_searches_pr_title_body_files_and_peer_prompts_not_only_routed_work(env):
    fake_gh_by_verb(env)
    peer_session(env)
    env.bind()
    out = env.run("find", "helpdesk").stdout
    assert "[pr] #7457" in out and "[peer] peer-e8" in out
    assert "[pr] #7457" in env.run("find", "docs").stdout  # found in the PR body
    assert "[pr] #7457" in env.run("find", "handlers.py").stdout  # found in a changed file path
    assert "no record holds" in env.run("find", "zebra").stdout


def test_find_shows_a_gh_failure_instead_of_swallowing_it(env):
    fake_gh_by_verb(env, fail=True)
    env.bind()
    out = env.run("find", "anything").stdout
    assert "UNKNOWN" in out and "gh search failed" in out


def test_peer_prompts_are_redacted_like_state(env):
    fake_gh_by_verb(env)
    peer_session(env, text=f"use the key {SECRET} to build the helpdesk agent")
    env.bind()
    for out in (env.run("find", "use the key").stdout, env.run("digest", "--since", "12h").stdout):
        assert "use the key" in out and SECRET not in out


def test_digest_and_eod_list_todays_prs_and_what_each_peer_was_asked(env):
    fake_gh_by_verb(env)
    peer_session(env)
    env.bind()
    for out in (env.run("digest", "--since", "12h").stdout, env.run("eod").stdout):
        assert "#7457 Add the helpdesk agent" in out and "peer-e8: build the helpdesk agent" in out
