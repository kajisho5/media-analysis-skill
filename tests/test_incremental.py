"""Incremental measurement on growing files (ADR-041, roadmap step 6). Real ffmpeg writes a recording to a pipe; the
test appends what it has written so far to the file in three bursts and measures after each one, then compares the
last result with one run of the analyzer on the final file. Never mocked, never skipped."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from media_analysis.analyzers.base import AnalysisContext
from media_analysis.capabilities import detect
from media_analysis.contract import validate_parameters
from media_analysis.errors import AnalysisError
from media_analysis.incremental import follow
from media_analysis.registry import default_registry
from media_analysis.security import PathPolicy

DURATION = 40
# picture changes every 3 s (a cut at each step), sound with a 2 s pause every 7 s
VIDEO = f"color=c=black:s=160x120:r=25:d={DURATION},geq=lum='mod(floor(T/3)*80+16,256)':cb=128:cr=128"
AUDIO = f"aevalsrc='if(lt(mod(t,7),5),0.5*sin(2*PI*440*t),0)':s=16000:d={DURATION}"
FORMATS = {"mkv": ["-f", "matroska"], "ts": ["-f", "mpegts"], "fmp4": ["-movflags", "frag_keyframe+empty_moov", "-f", "mp4"]}

CAPS = None


def caps():
    global CAPS
    if CAPS is None:
        CAPS = detect()
    return CAPS


def _writer(fmt: str) -> subprocess.Popen:
    """ffmpeg recording to a pipe, the way a recorder writes a file it cannot seek back into."""
    return subprocess.Popen(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi", "-i", VIDEO, "-f", "lavfi", "-i", AUDIO,
                             "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-pix_fmt", "yuv420p", "-c:a", "aac",
                             *FORMATS[fmt], "pipe:1"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def _full(path: Path, kind: str):
    analyzer = default_registry().for_kind(kind)
    return analyzer.analyze(AnalysisContext(str(path), caps(), 600), kind, validate_parameters(kind, {}))


@pytest.fixture(scope="module")
def recordings(tmp_path_factory):
    """The bytes each writer produced, recorded once per container (the writer is deterministic)."""
    out = {}
    for fmt in FORMATS:
        p = _writer(fmt)
        data, _ = p.communicate(timeout=120)
        assert p.returncode == 0 and len(data) > 100000, fmt
        out[fmt] = data
    return out


@pytest.mark.parametrize("fmt", sorted(FORMATS))
@pytest.mark.parametrize("kind", ["silence", "scene_detection"])
def test_appended_twice_equals_one_run_on_the_final_file(fmt, kind, recordings, tmp_path):
    """Done when (step 6): a fixture appended twice gives the same result as one run on the final file."""
    target = tmp_path / f"rec.{fmt}"
    target.write_bytes(b"")
    policy = PathPolicy(workspace=str(tmp_path))
    proc = _writer(fmt)
    docs = []
    burst = len(recordings[fmt]) // 3
    with open(target, "ab") as fh:
        for i in range(2):                               # first look, first append: while ffmpeg is still writing
            got = proc.stdout.read(burst)
            fh.write(got)
            fh.flush()
            docs.append(follow(str(target), kind, {}, "state.json", caps=caps(), policy=policy, guard=2.0))
        fh.write(proc.stdout.read())                     # second append: the rest, then the recorder closes the file
    assert proc.wait(timeout=120) == 0
    docs.append(follow(str(target), kind, {}, "state.json", caps=caps(), policy=policy, final=True))
    readable = [d for d in docs[:2] if d["readable"]]
    assert readable, "the growing file was never readable"
    for d in readable:
        assert d["growing"] and not d["complete"] and d["data"]["duration"] is None
    final = docs[-1]
    assert final["complete"] and final["completion"]["reason"] == "final" and final["provenance"] == "OBSERVED"
    assert final["data"] == _full(target, kind)
    runs = json.loads((tmp_path / "state.json").read_text())["runs"]
    assert len(runs) == len(readable) + 1
    # each run starts where the last one committed, minus two frames (scenes) or min_duration (silence), 4 s more where MPEG-TS seeks late
    slack = (0.6 if kind == "silence" else 0.2) + (4.0 if fmt == "ts" else 0.0)
    for a, b in zip(runs, runs[1:]):
        assert b["from"] >= a["committed_until"] - slack, runs
    assert runs[-1]["from"] > 0, runs
    measured = sum(r["to"] - r["from"] for r in runs)
    assert measured < 1.5 * DURATION, runs                # from the start each time would be about twice the duration


def _grow_and_follow(data: bytes, target: Path, kind: str, policy: PathPolicy, cuts, **kw):
    docs = []
    target.write_bytes(b"")
    prev = 0
    for i, c in enumerate(cuts):
        with open(target, "ab") as fh:
            fh.write(data[prev:c])
        prev = c
        docs.append(follow(str(target), kind, {}, "state.json", caps=caps(), policy=policy, final=(i == len(cuts) - 1), **kw))
    return docs


def test_continuous_sound_advances_the_cursor(tmp_path):
    """No silence at all: each run still starts near where the last one stopped, not from 0."""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi", "-i", f"sine=f=440:r=16000:d={DURATION}",
                        "-c:a", "aac", "-f", "matroska", "pipe:1"], capture_output=True, check=True)
    data = p.stdout
    policy = PathPolicy(workspace=str(tmp_path))
    target = tmp_path / "tone.mkv"
    docs = _grow_and_follow(data, target, "silence", policy, [len(data) // 3, 2 * len(data) // 3, len(data)])
    runs = json.loads((tmp_path / "state.json").read_text())["runs"]
    # min_duration before the committed point, or 4 s further back where the demuxer's seek landed late (seen on macOS's ffmpeg)
    assert docs[0]["committed_until"] > 5 and runs[1]["from"] >= runs[0]["committed_until"] - 4.6 and runs[2]["from"] > runs[1]["from"] > 0, runs
    assert docs[-1]["data"] == _full(target, "silence") and docs[-1]["data"]["segment_count"] == 0


def test_a_silence_closed_by_the_end_of_the_bytes_stays_pending_with_no_guard(recordings, tmp_path):
    """guard 0: ffmpeg closes a silence that is still running at the end of what is on disk; it is not a finished silence."""
    data = recordings["mkv"]
    policy = PathPolicy(workspace=str(tmp_path))
    probe_file = tmp_path / "probe.mkv"
    cut = None
    for k in range(10, 90):                       # a prefix that ends inside a pause (pauses run 5-7 s, 12-14 s, ... )
        size = len(data) * k // 100
        probe_file.write_bytes(data[:size])
        d = follow(str(probe_file), "silence", {}, f"p{k}.json", caps=caps(), policy=policy, guard=0.0)
        end = d["run"]["to"] if d["readable"] else 0
        if end > 3 and 5.6 < (end % 7.0) < 6.6:
            cut = size
            break
    assert cut, "no prefix ended inside a pause"
    target = tmp_path / "rec.mkv"
    docs = _grow_and_follow(data, target, "silence", policy, [cut, len(data)], guard=0.0)
    first = docs[0]
    assert all(s["end"] is not None and s["end"] < first["run"]["to"] - 0.5 for s in first["data"]["segments"]), first["data"]["segments"]
    assert first["committed_until"] < first["run"]["to"] - 0.5     # the open pause is measured again next time
    assert docs[-1]["data"] == _full(target, "silence")


def test_events_committed_while_growing_are_kept(recordings, tmp_path):
    target = tmp_path / "rec.mkv"
    data = recordings["mkv"]
    target.write_bytes(data[: len(data) // 2])
    policy = PathPolicy(workspace=str(tmp_path))
    d = follow(str(target), "silence", {}, "s.json", caps=caps(), policy=policy)
    assert d["readable"] and d["committed_until"] > 7.0
    starts = [s["start"] for s in d["data"]["segments"]]
    assert starts and all(abs(s - round(s)) < 0.1 and round(s) % 7 == 5 for s in starts), starts   # pauses at 5, 12, 19 ...
    assert all(s["end"] is not None and s["end"] <= d["committed_until"] for s in d["data"]["segments"])
    c = follow(str(target), "scene_detection", {}, "c.json", caps=caps(), policy=policy)
    assert c["data"]["cut_count"] >= 3 and c["data"]["scenes"][-1]["end"] is None


def test_plain_mp4_is_not_readable_until_closed(tmp_path):
    full = tmp_path / "full.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi", "-i", VIDEO, "-f", "lavfi", "-i", AUDIO,
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-t", "10", str(full)], check=True)
    growing = tmp_path / "rec.mp4"
    growing.write_bytes(full.read_bytes()[: full.stat().st_size // 2])       # moov is written last
    policy = PathPolicy(workspace=str(tmp_path))
    d = follow(str(growing), "silence", {}, "s.json", caps=caps(), policy=policy)
    assert not d["readable"] and not d["complete"] and d["data"] is None and "moov" in d["unreadable"]["hint"]
    growing.write_bytes(full.read_bytes())
    d = follow(str(growing), "silence", {}, "s.json", caps=caps(), policy=policy, final=True)
    assert d["readable"] and d["complete"] and d["data"] == _full(growing, "silence")


def test_completion_by_settle_window_and_by_marker(recordings, tmp_path):
    target = tmp_path / "rec.ts"
    target.write_bytes(recordings["ts"])
    policy = PathPolicy(workspace=str(tmp_path))
    now = [1000.0]
    run = lambda **kw: follow(str(target), "silence", {}, "s.json", caps=caps(), policy=policy, settle=30, clock=lambda: now[0], **kw)  # noqa: E731
    first = run()
    assert not first["complete"] and first["completion"]["unchanged_for"] == 0
    now[0] += 10
    second = run()
    assert not second["complete"] and second["completion"]["unchanged_for"] == 10
    now[0] += 25
    third = run()
    assert third["complete"] and third["completion"]["reason"] == "settled" and third["data"] == _full(target, "silence")
    again = run()                                        # complete and unchanged: the stored result, nothing measured
    assert again["complete"] and again["run"] is None and again["data"] == third["data"]

    marker = tmp_path / "rec.done"
    other = tmp_path / "other.ts"
    other.write_bytes(recordings["ts"])
    d = follow(str(other), "silence", {}, "o.json", caps=caps(), policy=policy, complete_marker=str(marker))
    assert not d["complete"]
    marker.write_text("")
    d = follow(str(other), "silence", {}, "o.json", caps=caps(), policy=policy, complete_marker=str(marker))
    assert d["complete"] and d["completion"]["reason"] == "marker"


def test_replaced_or_shortened_file_starts_again(recordings, tmp_path):
    target = tmp_path / "rec.mkv"
    data = recordings["mkv"]
    target.write_bytes(data[: len(data) * 2 // 3])
    policy = PathPolicy(workspace=str(tmp_path))
    follow(str(target), "silence", {}, "s.json", caps=caps(), policy=policy)
    target.write_bytes(data[: len(data) // 3])
    d = follow(str(target), "silence", {}, "s.json", caps=caps(), policy=policy)
    assert d["restarted"] == "the file is shorter than at the last run" and d["run_count"] == 1
    target.write_bytes(bytes(len(data) // 3) + data[len(data) // 3:])     # same length and more, different bytes
    with pytest.raises(AnalysisError):                    # zeros where the header was: not media any more
        follow(str(target), "silence", {}, "s.json", caps=caps(), policy=policy, final=True)


def test_refusals(recordings, tmp_path):
    target = tmp_path / "rec.mkv"
    target.write_bytes(recordings["mkv"])
    policy = PathPolicy(workspace=str(tmp_path))
    with pytest.raises(AnalysisError) as e:
        follow(str(target), "loudness", {}, "s.json", caps=caps(), policy=policy)
    assert e.value.code == "INVALID_INPUT" and e.value.details["supported_kinds"] == ["silence", "scene_detection"]
    follow(str(target), "silence", {}, "s.json", caps=caps(), policy=policy)
    with pytest.raises(AnalysisError) as e:               # the same state with other parameters
        follow(str(target), "silence", {"threshold_db": -30.0}, "s.json", caps=caps(), policy=policy)
    assert e.value.code == "INVALID_INPUT"
    with pytest.raises(AnalysisError) as e:
        follow(str(target), "silence", {}, str(tmp_path.parent / "outside.json"), caps=caps(), policy=policy)
    assert e.value.code == "PATH_NOT_ALLOWED"


def test_follow_cli(recordings, tmp_path):
    target = tmp_path / "rec.mkv"
    target.write_bytes(recordings["mkv"])
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = dict(os.environ, PYTHONPATH=src + os.pathsep + os.environ.get("PYTHONPATH", ""))
    r = subprocess.run([sys.executable, "-m", "media_analysis.cli", "follow", str(target), "--kind", "scene_detection", "--state", "st.json",
                        "--final", "--json"], cwd=str(tmp_path), env=env, capture_output=True)
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout.decode("utf-8"))
    assert doc["schema"] == "media-analysis/follow@1" and doc["complete"] and doc["data"] == _full(target, "scene_detection")
    r = subprocess.run([sys.executable, "-m", "media_analysis.cli", "follow", str(target), "--kind", "loudness", "--state", "st.json", "--json"],
                       cwd=str(tmp_path), env=env, capture_output=True)
    assert r.returncode == 2   # argparse refuses a kind that is not incremental
