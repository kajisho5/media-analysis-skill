"""Incremental measurement of a file that is still being written (ADR-041, roadmap step 6).

`follow()` measures one kind on a growing recording. Each call measures only from the last committed position to
what is on disk now, appends what it measured to a state file, and leaves the unfinished tail (the last `guard`
seconds, and an event still open there) to be measured again by the next call. Once the file is complete, the last
call measures the rest to the end and returns the same data as one run of the kind's analyzer on the final file.

Kinds: `silence` and `scene_detection`, the two whose filters carry a short, known state, so a run that starts at
the right point continues exactly where a full run would be:
  - silencedetect counts consecutive silent samples. A run that starts on a non-silent sample (the end of a finished
    silence, or the sample before a silence starts) is in the same state as a full run at that sample.
  - scdet compares each frame with the one before and with the previous frame's difference, so a run that starts two
    frames before the next uncommitted frame gives that frame the full run's score.
Kinds whose result depends on the whole file (loudness gating, the VAD model's state) are refused.

Every run keeps the original timestamps (`-copyts -start_at_zero` and `-ss` before `-i`), so the times printed by a
resumed run are those of a full run, digit for digit.

Containers: a file is measurable while it grows when its demuxer can read a prefix of it: Matroska / WebM written
live, MPEG-TS, fragmented MP4 / MOV. A plain MP4 / MOV keeps its index (moov) at the end, so ffprobe cannot open it
until the recorder closes it; such a run reports `readable: false` with the reason and measures nothing.

Completion: a file is complete when the caller says so (`final`), when a marker file exists, or when its size and
mtime have not changed for `settle` seconds between calls (the settle window of ingest-skill `offload --follow`).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import SKILL_ID, VERSION
from .analyzers.base import AnalysisContext
from .analyzers.scenes import scene_data
from .analyzers.silence import parse_silencedetect, silence_data
from .capabilities import CapabilitySet
from .contract import validate_parameters
from .errors import AnalysisError
from .probe import container_duration, select_stream
from .registry import default_registry
from .runner import ffmpeg_null_argv, run_argv
from .security import PathPolicy

FOLLOW_SCHEMA = "media-analysis/follow@1"
STATE_SCHEMA = "media-analysis/follow-state@1"
SUPPORTED_KINDS = ("silence", "scene_detection")
DEFAULT_SETTLE = 30.0      # seconds unchanged before a file counts as complete (ingest-skill ADR-007's default)
DEFAULT_GUARD = 2.0        # seconds at the end of what is on disk that are measured again next time
_SILENCE_LEAD = 0.005      # start a resumed silencedetect run this far before the committed sample (below min_duration's 0.01 floor)
_TOL = 0.0005
_CHECK_BYTES = 65536
_AFRAME = re.compile(r"^frame:\d+\s+pts:-?\d+\s+pts_time:(-?[0-9.]+)", re.M)
_NSAMPLES = re.compile(r"^lavfi\.astats\.Overall\.Number_of_samples=([0-9.]+)", re.M)
_SEEK_BACK = (4.0, 16.0, 64.0)
_FRAME = re.compile(r"^frame:(\d+)\s+pts:(-?\d+)\s+pts_time:(-?[0-9.]+)")
_KV = re.compile(r"^lavfi\.scd\.(\w+)=(-?[0-9.]+)")
_UNREADABLE_HINT = ("a plain MP4 / MOV keeps its index (moov) at the end and cannot be read before the recorder closes it; "
                    "record to Matroska, MPEG-TS or fragmented MP4 to measure while recording")


def _middle_check(path: str, size: int) -> Optional[Dict[str, Any]]:
    """sha256 of 64 KiB around the middle of what was on disk: a muxer rewrites headers at the start and appends
    an index at the end when it closes the file, but never touches the middle, so a change there means the file
    was replaced, not extended."""
    if size <= 0:
        return None
    length = min(_CHECK_BYTES, size)
    offset = max(0, size // 2 - length // 2)
    with open(path, "rb") as fh:
        fh.seek(offset)
        digest = hashlib.sha256(fh.read(length)).hexdigest()
    return {"offset": offset, "length": length, "sha256": digest}


def _check_still(path: str, size: int, check: Optional[Dict[str, Any]]) -> bool:
    if not check:
        return True
    if size < check["offset"] + check["length"]:
        return False
    with open(path, "rb") as fh:
        fh.seek(check["offset"])
        return hashlib.sha256(fh.read(check["length"])).hexdigest() == check["sha256"]


def parse_scdet_frames(stdout: str) -> List[Dict[str, Any]]:
    """Every frame scdet printed: its counter in this run, raw pts_time, score and whether it was flagged."""
    frames: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for ln in stdout.splitlines():
        m = _FRAME.match(ln)
        if m:
            cur = {"n": int(m.group(1)), "time": float(m.group(3)), "score": None, "flagged": False}
            frames.append(cur)
            continue
        k = _KV.match(ln)
        if k and cur is not None:
            if k.group(1) == "score":
                cur["score"] = float(k.group(2))
            elif k.group(1) == "time":
                cur["flagged"] = True
    return frames


def _new_state(path: str, kind: str, parameters: Dict[str, Any], algorithm_version: str) -> Dict[str, Any]:
    return {"schema": STATE_SCHEMA, "input": path, "kind": kind, "parameters": parameters, "algorithm_version": algorithm_version,
            "complete": False, "committed": {"until": 0.0, "pairs": [], "cuts": [], "tail": []}, "observed": None, "check": None,
            "runs": [], "final": None}


def _load_state(state_file: Path) -> Optional[Dict[str, Any]]:
    if not state_file.exists():
        return None
    try:
        doc = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise AnalysisError("INVALID_INPUT", f"follow state is not readable JSON: {e}", {"state": str(state_file)})
    if not isinstance(doc, dict) or doc.get("schema") != STATE_SCHEMA:
        raise AnalysisError("INVALID_INPUT", f"{state_file} is not a {STATE_SCHEMA} document", {"state": str(state_file)})
    return doc


def _save_state(state_file: Path, state: Dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(state_file.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, sort_keys=True, ensure_ascii=False, allow_nan=False)
            fh.write("\n")
        os.replace(tmp, str(state_file))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _state_path(policy: PathPolicy, state: str) -> Path:
    target = os.path.abspath(state if os.path.isabs(state) else os.path.join(str(policy.workspace), state))
    return policy.resolve_write_dir(os.path.dirname(target)) / os.path.basename(target)


def follow(input_path: str, kind: str, parameters: Optional[Dict[str, Any]], state: str, *, caps: CapabilitySet,
           policy: Optional[PathPolicy] = None, timeout: Optional[float] = 600.0, settle: float = DEFAULT_SETTLE,
           guard: float = DEFAULT_GUARD, final: bool = False, complete_marker: Optional[str] = None,
           clock: Callable[[], float] = time.time) -> Dict[str, Any]:
    """One incremental run. Returns a `media-analysis/follow@1` document; raises AnalysisError on failure."""
    if kind not in SUPPORTED_KINDS:
        raise AnalysisError("INVALID_INPUT", f"{kind} cannot be measured incrementally", {"supported_kinds": list(SUPPORTED_KINDS)})
    if not (isinstance(settle, (int, float)) and settle >= 0) or not (isinstance(guard, (int, float)) and guard >= 0):
        raise AnalysisError("INVALID_INPUT", "settle and guard must be non-negative seconds")
    policy = policy or PathPolicy()
    params = validate_parameters(kind, dict(parameters or {}))
    path = str(policy.resolve_input(input_path))
    analyzer = default_registry().for_kind(kind)
    missing = caps.missing(list(analyzer.required_capabilities))
    if missing:
        raise AnalysisError("ANALYZER_UNAVAILABLE", f"media-analysis/{analyzer.id} needs capabilities that are missing", {"missing": missing})
    state_file = _state_path(policy, state)
    st = _load_state(state_file)
    restarted: Optional[str] = None
    if st is not None and (st.get("input") != path or st.get("kind") != kind or st.get("parameters") != params):
        raise AnalysisError("INVALID_INPUT", "the follow state belongs to another input, kind or parameters",
                            {"state": str(state_file), "state_input": st.get("input"), "state_kind": st.get("kind")})
    if st is not None and st.get("algorithm_version") != analyzer.algorithm_version:
        st, restarted = None, "the analyzer's algorithm version changed"

    stat = os.stat(path)
    size, mtime, now = stat.st_size, stat.st_mtime, clock()
    if st is not None:
        obs = st.get("observed") or {}
        if size < obs.get("size", 0):
            st, restarted = None, "the file is shorter than at the last run"
        elif not _check_still(path, size, st.get("check")):
            st, restarted = None, "the file changed before the last measured position"
        elif st.get("complete") and (size != obs.get("size") or mtime != obs.get("mtime")):
            st, restarted = None, "the file changed after it was complete"
    if st is None:
        st = _new_state(path, kind, params, analyzer.algorithm_version)
    obs = st.get("observed") or {}
    unchanged = obs.get("size") == size and obs.get("mtime") == mtime
    since = obs.get("since", now) if unchanged else now
    reason = ("final" if final else "marker" if complete_marker and os.path.exists(complete_marker)
              else "settled" if now - since >= settle else None)
    st["observed"] = {"size": size, "mtime": mtime, "since": since}
    st["check"] = _middle_check(path, size)
    completion = {"reason": reason, "settle_seconds": settle, "unchanged_for": round(now - since, 3), "marker": complete_marker}

    if st.get("complete") and st.get("final") is not None:      # nothing new since the run that completed it
        _save_state(state_file, st)
        return _document(st, path, kind, params, analyzer, st["final"], True, completion, restarted, run=None, readable=True)

    ctx = AnalysisContext(path, caps, timeout)
    try:
        probe = ctx.probe()
    except AnalysisError as e:
        if e.code != "UNSUPPORTED_FORMAT" or reason is not None:
            raise
        _save_state(state_file, st)
        doc = _document(st, path, kind, params, analyzer, None, False, completion, restarted, run=None, readable=False)
        doc["unreadable"] = {"reason": e.message, "hint": _UNREADABLE_HINT}
        return doc
    stream = select_stream(probe, "audio" if kind == "silence" else "video", params["stream"])
    complete = reason is not None
    t0 = time.monotonic()
    if kind == "silence":
        run = _silence_run(ctx, st, params, stream, guard, complete)
    else:
        run = _scene_run(ctx, st, params, guard, complete)
    run["seconds"] = round(time.monotonic() - t0, 3)
    run["size"] = size
    st["runs"].append(run)
    committed = st["committed"]
    if complete:
        duration = stream["duration"] if stream["duration"] is not None else container_duration(probe)
        data = _data(kind, stream, duration, params, committed)
        st["complete"] = True
        st["final"] = data
    else:
        data = _data(kind, stream, None, params, committed)
    _save_state(state_file, st)
    return _document(st, path, kind, params, analyzer, data, complete, completion, restarted, run=run, readable=True)


def _data(kind: str, stream: Dict[str, Any], duration: Optional[float], params: Dict[str, Any], committed: Dict[str, Any]) -> Dict[str, Any]:
    if kind == "silence":
        return silence_data(stream, duration, params, [(p[0], p[1]) for p in committed["pairs"]])
    return scene_data(stream, duration, params, [dict(c) for c in committed["cuts"]])


def _failed(r, what: str) -> AnalysisError:
    return AnalysisError("ANALYSIS_FAILED", f"{what} failed", {"stderr_tail": "\n".join(r.stderr.strip().splitlines()[-5:])})


def _starts(target: float) -> List[float]:
    """Where to start a resumed run: the exact point first, then further back. Some demuxers (MPEG-TS) land after
    the requested time when they seek; a run is only used when it decoded from at or before the point it needs."""
    if target <= 0:
        return [0.0]
    return [target] + [max(0.0, target - m) for m in _SEEK_BACK if target - m > 0] + [0.0]


def _silence_run(ctx: AnalysisContext, st: Dict[str, Any], params: Dict[str, Any], stream: Dict[str, Any], guard: float,
                 complete: bool) -> Dict[str, Any]:
    committed = st["committed"]
    until = float(committed["until"])
    rate = int(stream.get("audio", {}).get("sample_rate") or 48000)
    for start in _starts(until - _SILENCE_LEAD if until > 0 else 0.0):
        ctx.record("ffmpeg", f"silencedetect on audio stream {params['stream']} from {start:.3f}s (growing file)")
        # after silencedetect (so its input is untouched): one frame per second of samples, each printing its sample
        # count, so the run knows the first and the last sample it decoded
        chain = (f"silencedetect=noise={params['threshold_db']}dB:d={params['min_duration']},asetnsamples=n={rate}:p=0,"
                 "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=Number_of_samples,"
                 "ametadata=mode=print:key=lavfi.astats.Overall.Number_of_samples:file=-")
        argv = ffmpeg_null_argv(ctx.exe("ffmpeg"), ctx.input_path, "-map", f"0:a:{params['stream']}", "-vn", "-af", chain,
                                input_options=("-copyts", "-start_at_zero", "-ss", f"{start:.6f}"))
        r = run_argv(argv, ctx.timeout)
        frames = [(float(m.group(1)), float(n)) for m, n in zip(_AFRAME.finditer(r.stdout), _NSAMPLES.findall(r.stdout))]
        if r.returncode != 0 and (complete or not frames):
            raise _failed(r, "silencedetect")
        if not frames or frames[0][0] <= max(until, 0.0) + _TOL or start == 0.0:
            break
    available = frames[-1][0] + frames[-1][1] / rate if frames else until
    pairs = parse_silencedetect(r.stderr)
    new = [p for p in pairs if until <= 0 or p[0] >= until - _TOL]
    if complete:
        commit, new_until = new, max(available, until)
    else:
        safe = available - guard
        idx = next((i for i, (_s, e) in enumerate(new) if e is None or e > safe), len(new))
        commit = new[:idx]
        if idx < len(new) and new[idx][0] <= safe:
            new_until = new[idx][0]                 # the sample a pending silence starts on: the filter's state is reset there
        elif commit:
            new_until = commit[-1][1]               # the first sound after the last finished silence
        else:
            new_until = until
    committed["pairs"] = committed["pairs"] + [[s, e] for s, e in commit]
    committed["until"] = new_until
    return {"from": round(start, 6), "to": round(available, 6), "committed_until": round(new_until, 6), "events": len(commit), "complete": complete}


def _scene_run(ctx: AnalysisContext, st: Dict[str, Any], params: Dict[str, Any], guard: float, complete: bool) -> Dict[str, Any]:
    committed = st["committed"]
    tail = committed["tail"]                         # the last three committed frames, [global index, pts_time]
    if len(tail) < 3:                                # too little committed to resume on: measure from the start again
        committed.update(until=0.0, cuts=[], tail=[])
        tail = []
    last_index = tail[-1][0] if tail else -1
    target = max(0.0, tail[0][1] - (tail[1][1] - tail[0][1]) / 2) if tail else 0.0
    anchor = None
    for start in _starts(target):
        ctx.record("ffmpeg", f"scdet on video stream {params['stream']} from {start:.3f}s (growing file)")
        argv = ffmpeg_null_argv(ctx.exe("ffmpeg"), ctx.input_path, "-map", f"0:V:{params['stream']}", "-an", "-sn", "-vf",
                                f"scdet=threshold={params['threshold']},metadata=print:file=-", loglevel="error",
                                input_options=("-copyts", "-start_at_zero", "-ss", f"{start:.6f}"))
        r = run_argv(argv, ctx.timeout)
        frames = parse_scdet_frames(r.stdout)
        if r.returncode != 0 and (complete or not frames):
            raise _failed(r, "scdet")
        if not tail:
            break
        # the frame two before the next uncommitted one must be decoded, and must not be this run's first frame
        anchor = next((f for f in frames if abs(f["time"] - tail[0][1]) < 1e-6), None)
        if anchor is not None:
            break
    if tail and anchor is None:
        raise AnalysisError("ANALYSIS_FAILED", "the resumed run did not find the last committed frames again", {"anchor_time": tail[0][1]})
    offset = tail[0][0] - anchor["n"] if tail else 0
    available = frames[-1]["time"] if frames else float(committed["until"])
    safe = available if complete else available - guard
    taken = [f for f in frames if f["n"] + offset > last_index and f["time"] <= safe]
    for f in taken:
        if f["flagged"]:
            committed["cuts"].append({"frame": f["n"] + offset, "time": round(f["time"], 3), "score": round(f["score"] or 0.0, 3)})
    known = tail + [[f["n"] + offset, f["time"]] for f in taken]
    committed["tail"] = known[-3:]
    if known:
        committed["until"] = known[-1][1]
    return {"from": round(start, 6), "to": round(available, 6), "committed_until": round(float(committed["until"]), 6),
            "frames": len(taken), "events": sum(1 for f in taken if f["flagged"]), "complete": complete}


def _document(st: Dict[str, Any], path: str, kind: str, params: Dict[str, Any], analyzer, data: Optional[Dict[str, Any]], complete: bool,
              completion: Dict[str, Any], restarted: Optional[str], run: Optional[Dict[str, Any]], readable: bool) -> Dict[str, Any]:
    runs = st.get("runs") or []
    measured = sum(max(0.0, x["to"] - x["from"]) for x in runs)
    until = float(st["committed"]["until"])
    return {"schema": FOLLOW_SCHEMA, "skill": {"id": SKILL_ID, "version": VERSION}, "status": "ok", "input": path, "kind": kind,
            "parameters": params, "source": f"media-analysis/{analyzer.id}@{VERSION}", "provenance": "OBSERVED",
            "readable": readable, "complete": complete, "growing": not complete, "completion": completion, "restarted": restarted,
            "committed_until": round(until, 6), "run": run, "run_count": len(runs),
            "measured_seconds": round(measured, 3), "data": data}
