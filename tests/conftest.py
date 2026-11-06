"""Real-process test harness: hooks and CLI run as subprocesses against real files."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
SCRIPTS = Path(os.environ.get("HUBKIT_SCRIPTS", SKILL / "scripts"))
INTENTS = Path(os.environ.get("HUBKIT_TEST_INTENTS", SKILL / "intents.example.toml"))
sys.path.insert(0, str(SCRIPTS))
from hubcore import compat  # noqa: E402

# Tests whose fake `gh`, `crontab` or notifier is a /bin/sh script cannot run where there is no sh on the PATH.
# They are skipped on Windows (and say so), never silently passed.
SH_SHIMS = ("#!/bin/sh", "fake_gh(", "fake_crontab(", "sink(")


def pytest_collection_modifyitems(config, items):
    if sys.platform != "win32":
        return
    import inspect

    skip = pytest.mark.skip(reason="needs a POSIX sh shim for gh/crontab/notifier; not ported to Windows")
    for item in items:
        try:
            src = inspect.getsource(item.function)
        except (OSError, TypeError):
            continue
        if any(m in src for m in SH_SHIMS):
            item.add_marker(skip)


class Env:
    def __init__(self, root: Path):
        self.root = root
        self.state = root / "state"
        self.sessions = root / "sessions"
        self.sessions.mkdir()
        self.transcripts = root / "transcripts"
        self.transcripts.mkdir()
        self.cfg = root / "config.toml"
        self.cfg.write_text(
            f'[user]\nname="Tester"\ntimezone="UTC"\n[mep]\nenabled=false\n'
            f'[paths]\nintents="{INTENTS}"\ntranscripts_glob="{self.transcripts}/*.jsonl"\n'
            f'extensions_dir="{root / "ext"}"\nlearnings="{root / "learnings.md"}"\n'
        )
        self.procs: list[subprocess.Popen] = []

    def env(self, **extra) -> dict:
        e = dict(os.environ)
        e.update(HUBKIT_STATE=str(self.state), HUBKIT_SESSIONS=str(self.sessions),
                 HUBKIT_CONFIG=str(self.cfg), XDG_CONFIG_HOME=str(self.root / "xdg"))
        e.pop("HUBKIT_DISABLE", None)
        e.update(extra)
        return e

    def run(self, *args, stdin: str | None = None, timeout: int = 20, **extra):
        return subprocess.run([sys.executable, str(SCRIPTS / "hubkit"), *args], input=stdin,
                              capture_output=True, text=True, env=self.env(**extra), timeout=timeout)

    def learn(self, *args, **extra):
        return subprocess.run([sys.executable, str(SCRIPTS / "learn.py"), *args],
                              capture_output=True, text=True, env=self.env(**extra), timeout=20)

    def session(self, sid="sess-hub", kind="interactive", name="hub-test") -> subprocess.Popen:
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        self.procs.append(p)
        stat = compat.proc_start(p.pid)
        (self.sessions / f"{p.pid}.json").write_text(json.dumps(
            {"pid": p.pid, "sessionId": sid, "procStart": stat, "kind": kind, "name": name,
             "entrypoint": "cli"}))
        return p

    def bind(self, **kw) -> subprocess.Popen:
        p = self.session(**kw)
        r = self.run("bind", "--pid", str(p.pid))
        assert r.returncode == 0, r.stderr
        return p

    def hook(self, event, payload: dict, **extra):
        return self.run("hook", event, stdin=json.dumps(payload), **extra)

    def prompt(self, text, sid="sess-hub", **extra):
        return self.hook("prompt", {"hook_event_name": "UserPromptSubmit", "session_id": sid,
                                    "prompt": text}, **extra)

    def rows(self, name):
        p = self.state / name
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def backdate(self, minutes: float):
        """Shift every ts in orders.jsonl into the past (a real file edit)."""
        p = self.state / "orders.jsonl"
        out = []
        for r in self.rows("orders.jsonl"):
            r["ts"] -= minutes * 60
            if r.get("deadline"):
                r["deadline"] -= minutes * 60
            out.append(json.dumps(r))
        p.write_text("\n".join(out) + "\n")

    def close(self):
        for p in self.procs:
            p.kill()
            p.wait()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()
