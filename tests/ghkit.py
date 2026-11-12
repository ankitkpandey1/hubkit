"""A local stand-in for the `gh` binary, per PR and per search (the tests cannot reach GitHub; everything else is real).

`pr view <url>` prints <root>/pr_<number>.json; `search prs ...` prints <root>/search.json; a missing file exits 1 like a failing gh.
Every call is appended to <root>/gh.log."""

import json
import time

HEAD = "a" * 40
OLD = "b" * 40
URL = "https://github.com/acme/repo/pull/{n}"


def install(env) -> None:
    gh = env.root / "gh"
    gh.write_text(f'#!/bin/sh\necho "$@" >> {env.root}/gh.log\n'
                  f'case "$1 $2" in\n'
                  f'"pr view") f={env.root}/pr_$(basename "$3").json; [ -f "$f" ] && cat "$f" || exit 1;;\n'
                  f'"search prs") [ -f {env.root}/search.json ] && cat {env.root}/search.json || exit 1;;\n'
                  f'*) exit 1;;\nesac\n')
    gh.chmod(0o755)
    env.cfg.write_text(env.cfg.read_text() + f'[tools]\ngh="{gh}"\n')


def conf(env, text: str, login: str = "") -> None:
    """Append config tables; `login` sets user.github_login (a key of the existing [user] table)."""
    body = env.cfg.read_text()
    if login:
        body = body.replace('name="Tester"', f'name="Tester"\ngithub_login="{login}"')
    env.cfg.write_text(body + text)


def iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def review(commit, login="rev", state="APPROVED", ago=60, body=""):
    return {"submittedAt": iso(time.time() - ago), "state": state, "commit": {"oid": commit}, "author": {"login": login}, "body": body}


def pr(env, n=101, head=HEAD, state="OPEN", reviews=None, author="dev", ci=None, merged_ago=None, commit_ago=None):
    data = {"headRefOid": head, "state": state, "author": {"login": author}, "reviews": reviews or [],
            "commits": [{"authors": [{"login": author}], "committedDate": iso(time.time() - (commit_ago or 0))}],
            "statusCheckRollup": [{"conclusion": c} for c in (ci or [])]}
    if merged_ago is not None:
        data["mergedAt" if state == "MERGED" else "closedAt"] = iso(time.time() - merged_ago)
    (env.root / f"pr_{n}.json").write_text(json.dumps(data))


def search(env, *prs):
    (env.root / "search.json").write_text(json.dumps([{"url": URL.format(n=n), "title": t, "number": n, "repository": {"nameWithOwner": "acme/repo"}} for n, t in prs]))


def calls(env, word="") -> int:
    p = env.root / "gh.log"
    return len([x for x in p.read_text().splitlines() if word in x]) if p.exists() else 0
