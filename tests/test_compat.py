"""compat.py against real processes and real files. Meant to run on every OS."""

import os
import shutil
import subprocess
import sys
import time

import pytest
from conftest import SCRIPTS

from hubcore import compat

SLEEPER = [sys.executable, "-c", "import time; time.sleep(300)"]
needs_ps = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("ps"), reason="the ps path needs a POSIX ps")


@pytest.fixture
def child():
    p = subprocess.Popen(SLEEPER)
    yield p
    p.kill()
    p.wait()


def test_proc_start_names_a_live_process_and_forgets_a_dead_one(child):
    first = compat.proc_start(child.pid)
    assert first and compat.proc_start(child.pid) == first
    child.kill()
    child.wait()
    assert compat.proc_start(child.pid) is None


def test_parent_pid_of_a_child_is_this_process(child):
    assert compat.parent_pid(child.pid) == os.getpid()


@needs_ps
def test_without_proc_the_ps_path_gives_the_same_answers(child, monkeypatch, tmp_path):
    monkeypatch.setattr(compat, "_PROC", tmp_path / "no-proc")  # macOS has no /proc: take that branch with the real ps
    first = compat.proc_start(child.pid)
    assert first and compat.proc_start(child.pid) == first
    assert compat.parent_pid(child.pid) == os.getpid()
    child.kill()
    child.wait()
    assert compat.proc_start(child.pid) is None


HOLDER = (
    "import os, sys, time\n"
    f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
    "from hubcore import compat\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
    "assert compat.lock_exclusive(fd, wait=True)\n"
    "print('held', flush=True)\n"
    "time.sleep(float(sys.argv[2]))\n"
)


def test_a_lock_held_by_another_process_blocks_until_it_exits(tmp_path):
    path = tmp_path / "lock"
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(path), "1.5"], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        fd = os.open(path, os.O_RDWR | os.O_CREAT)
        try:
            assert compat.lock_exclusive(fd, wait=False) is False
            began = time.time()
            assert compat.lock_exclusive(fd, wait=True)  # waits for the holder to end
            assert time.time() - began > 0.3
            compat.unlock(fd)
        finally:
            os.close(fd)
    finally:
        holder.kill()
        holder.wait()


def test_stdio_is_utf8_whatever_the_console_code_page_is():
    code = f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); from hubcore import compat; compat.utf8_stdio(); print('café → ok')"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, env={**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"})
    assert r.stdout.decode("utf-8").strip() == "café → ok"
