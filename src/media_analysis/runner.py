"""Safe execution of ffprobe / ffmpeg.

Rules (docs/security.md):
- argv lists only; never a shell of any kind, never a user-supplied command or argv.
- every argument is built by this package from structured data; the only user-controlled argument is the
  resolved absolute input path, always placed after "-i" so it cannot be parsed as an option.
- protocol whitelist "file" so a path can never become a network / pipe / concat source.
- a timeout kills the whole process group (ffmpeg keeps decoding otherwise).
- stdout / stderr are returned to the caller for parsing; they are never copied into Observations.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from .errors import AnalysisError


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    seconds: float


def which(name: str) -> Optional[str]:
    return shutil.which(name)


def _check_argv(argv: List[str]) -> None:
    for a in argv:
        if not isinstance(a, str) or "\x00" in a:
            raise AnalysisError("INVALID_INPUT", "argument contains NUL or is not a string")


def run_argv(argv: List[str], timeout: Optional[float]) -> RunResult:
    """Run an executable with an argv list. Raises ANALYZER_TIMEOUT on timeout, ANALYZER_UNAVAILABLE when the
    executable cannot be started."""
    _check_argv(argv)
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, errors="replace", env=_clean_env(), **_group_kwargs())
    except FileNotFoundError:
        raise AnalysisError("ANALYZER_UNAVAILABLE", f"executable not found: {os.path.basename(argv[0])}")
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise AnalysisError("ANALYZER_TIMEOUT", f"{os.path.basename(argv[0])} exceeded {timeout}s", {"timeout": timeout})
    return RunResult(proc.returncode, out or "", err or "", round(time.monotonic() - t0, 3))


def stream_records(argv: List[str], timeout: Optional[float], record_size: int, on_record: Callable[[bytes], None]) -> RunResult:
    """Run an executable whose stdout is a sequence of fixed-size binary records (raw video frames) and hand each
    record to `on_record` as it arrives, so memory stays at one record however long the input is. stderr is drained
    on a thread (an undrained pipe would stall the child) and returned as text. A watchdog kills the whole process
    group at the deadline (ANALYZER_TIMEOUT), the same as run_argv; a trailing partial record is dropped."""
    _check_argv(argv)
    if record_size <= 0:
        raise ValueError("record_size must be positive")
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=_clean_env(), **_group_kwargs())
    except FileNotFoundError:
        raise AnalysisError("ANALYZER_UNAVAILABLE", f"executable not found: {os.path.basename(argv[0])}")
    err_chunks: List[bytes] = []
    drain = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
    drain.start()
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        _kill_group(proc, wait=False)

    watchdog = threading.Timer(timeout, expire) if timeout else None
    if watchdog:
        watchdog.daemon = True
        watchdog.start()
    try:
        buf = bytearray()
        while True:
            chunk = proc.stdout.read(max(record_size, 65536))
            if not chunk:
                break
            buf += chunk
            while len(buf) >= record_size:
                on_record(bytes(buf[:record_size]))
                del buf[:record_size]
        proc.wait()
    except BaseException:
        _kill_group(proc)
        raise
    finally:
        if watchdog:
            watchdog.cancel()
        drain.join(5)
    if timed_out.is_set():
        raise AnalysisError("ANALYZER_TIMEOUT", f"{os.path.basename(argv[0])} exceeded {timeout}s", {"timeout": timeout})
    err = b"".join(c for c in err_chunks if c).decode("utf-8", "replace")
    return RunResult(proc.returncode, "", err, round(time.monotonic() - t0, 3))


def _group_kwargs() -> dict:
    """Start the child as the leader of its own process group so a timeout can kill it together with anything it
    spawned: setsid on POSIX, CREATE_NEW_PROCESS_GROUP on Windows."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _kill_group(proc: subprocess.Popen, wait: bool = True) -> None:
    """Kill the whole process group (POSIX: killpg SIGKILL; Windows: taskkill /T /F kills the process tree)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
    if not wait:
        return
    try:
        proc.communicate(timeout=5)
    except Exception:
        pass


_ENV_KEEP = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "TERM",
             "SYSTEMROOT", "SYSTEMDRIVE", "PATHEXT", "COMSPEC", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA")


def _clean_env() -> dict:
    """Child processes only see what they need to run (PATH, home / temp / locale, Windows system variables);
    secrets in the parent environment are not inherited. Comparison is case-insensitive (Windows)."""
    return {k: v for k, v in os.environ.items() if k.upper() in _ENV_KEEP}


FF_INPUT_PREFIX = ["-hide_banner", "-nostdin", "-protocol_whitelist", "file"]


def ffprobe_argv(exe: str, input_path: str, *options: str) -> List[str]:
    return [exe, "-hide_banner", "-protocol_whitelist", "file", "-v", "error", *options, "-i", input_path]


def ffmpeg_null_argv(exe: str, input_path: str, *options: str, loglevel: str = "info") -> List[str]:
    """ffmpeg reading one input, running a filter, writing nothing (`-f null -`)."""
    return [exe, *FF_INPUT_PREFIX, "-loglevel", loglevel, "-i", input_path, *options, "-f", "null", "-"]
