"""The one place that knows which operating system it runs on (stdlib only).

Linux reads /proc; macOS and other POSIX systems ask `ps`; Windows asks kernel32 through ctypes. File locks are
flock on POSIX and msvcrt on Windows. The scheduler check is crontab on POSIX and Task Scheduler on Windows.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

_PROC = Path("/proc/self/stat")


# ---- process identity: (pid, start time) names one process for as long as it lives ----


def _ps(*args: str) -> str | None:
    """Output of `ps` (POSIX without /proc, e.g. macOS) or None when it fails or prints nothing."""
    try:
        r = subprocess.run(["ps", *args], capture_output=True, encoding="utf-8", errors="replace", timeout=5, check=False, env={**os.environ, "LC_ALL": "C"})
    except OSError, subprocess.SubprocessError:
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def _win_creation_time(pid: int) -> str | None:
    if sys.platform != "win32":
        raise OSError("Windows only")
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(0x1000 | 0x100000, False, pid)  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in (created, exited, kernel, user))):
            return None
        if k32.WaitForSingleObject(handle, 0) == 0:  # signalled: the process has ended
            return None
        return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
    finally:
        k32.CloseHandle(handle)


def proc_start(pid: int) -> str | None:
    """An opaque start-time token for a live process, or None when it is gone. Equal tokens mean the same process."""
    if sys.platform == "win32":
        return _win_creation_time(pid)
    if _PROC.exists():
        try:
            return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[19]
        except (OSError, IndexError):
            return None
    return _ps("-o", "lstart=", "-p", str(pid))


def _win_parent(pid: int) -> int | None:
    if sys.platform != "win32":
        raise OSError("Windows only")
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32First.argtypes = k32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snap in (None, wintypes.HANDLE(-1).value):
        return None
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        ok = k32.Process32First(snap, ctypes.byref(entry))
        while ok:
            if entry.th32ProcessID == pid:
                return int(entry.th32ParentProcessID)
            ok = k32.Process32Next(snap, ctypes.byref(entry))
        return None
    finally:
        k32.CloseHandle(snap)


def parent_pid(pid: int) -> int | None:
    """Parent of a process, or None when unknown."""
    if sys.platform == "win32":
        return _win_parent(pid)
    if _PROC.exists():
        try:
            return int(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            return None
    out = _ps("-o", "ppid=", "-p", str(pid))
    return int(out) if out and out.isdigit() else None


# ---- file lock: held until the descriptor is closed ----


def lock_exclusive(fd: int, wait: bool) -> bool:
    """Take an exclusive lock on an open file. With wait, blocks until held; without, returns False when it is taken."""
    if sys.platform == "win32":
        import msvcrt

        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                if not wait:
                    return False
                time.sleep(0.05)
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        return True
    except BlockingIOError:
        return False


def unlock(fd: int) -> None:
    """Release a lock taken with lock_exclusive."""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


# ---- the user's scheduler ----


def scheduler_name() -> str:
    return "Task Scheduler" if sys.platform == "win32" else "crontab"


def scheduler_lines() -> list[str]:
    """Active scheduled entries as text lines (crontab lines, or Task Scheduler rows). Unreadable counts as none."""
    argv = ["schtasks", "/query", "/fo", "csv", "/nh"] if sys.platform == "win32" else ["crontab", "-l"]
    try:
        out = subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace", timeout=10, check=False).stdout
    except OSError, subprocess.SubprocessError:
        return []
    return [x for x in out.splitlines() if not x.lstrip().startswith("#")]


# ---- text I/O ----


def utf8_stdio() -> None:
    """Hooks exchange UTF-8 JSON; on Windows the default console code page would mangle it."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf:
            reconf(encoding="utf-8", errors="replace")
