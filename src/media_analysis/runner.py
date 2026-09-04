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
import time
from dataclasses import dataclass
from typing import List, Optional

from .errors import AnalysisError


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    seconds: float


def which(name: str) -> Optional[str]:
    return shutil.which(name)


def run_argv(argv: List[str], timeout: Optional[float]) -> RunResult:
    """Run an executable with an argv list. Raises ANALYZER_TIMEOUT on timeout, ANALYZER_UNAVAILABLE when the
    executable cannot be started."""
    for a in argv:
        if not isinstance(a, str) or "\x00" in a:
            raise AnalysisError("INVALID_INPUT", "argument contains NUL or is not a string")
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, errors="replace", start_new_session=True, env=_clean_env())
    except FileNotFoundError:
        raise AnalysisError("ANALYZER_UNAVAILABLE", f"executable not found: {os.path.basename(argv[0])}")
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise AnalysisError("ANALYZER_TIMEOUT", f"{os.path.basename(argv[0])} exceeded {timeout}s", {"timeout": timeout})
    return RunResult(proc.returncode, out or "", err or "", round(time.monotonic() - t0, 3))


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError):
        proc.kill()
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
