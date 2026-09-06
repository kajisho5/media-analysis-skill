"""Integration tests: real ffmpeg / ffprobe on generated fixtures (tests/fixtures/generate.py). Never mocked, never
skipped: a missing FFmpeg fails the session."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from media_analysis import VERSION
from media_analysis.budget import Budget
from media_analysis.cache import ObservationCache
from media_analysis.engine import AnalysisEngine
from media_analysis.errors import AnalysisError
from media_analysis.security import PathPolicy


def engine(tmp_path, cache=False, **budget):
    policy = PathPolicy(workspace=str(tmp_path))
    c = ObservationCache(str(tmp_path / "cache"), policy) if cache else None
    return AnalysisEngine(policy=policy, cache=c, budget=Budget(**budget) if budget else None)


def run(eng, path, kind, **params):
    return eng.analyze({"asset_id": "asset-1", "input": str(path), "kind": kind, "parameters": params})["observation"]


def test_mp4_probe(media, tmp_path):
    o = run(engine(tmp_path), media["av"], "media_probe")
    assert o["source"] == f"media-analysis/probe@{VERSION}" and o["kind"] == "media_probe" and o["asset_id"] == "asset-1"
    c, v, a = o["data"]["container"], o["data"]["video"], o["data"]["audio"]
    assert c["format"].startswith("mov,mp4") and abs(c["duration"] - 6.0) < 0.05 and c["size"] > 0 and c["bitrate"] > 0 and c["start_time"] == 0.0
    assert (v["codec"], v["width"], v["height"], v["fps"], v["pixel_format"]) == ("h264", 320, 180, 25.0, "yuv420p")
    assert (a["codec"], a["sample_rate"], a["channels"], a["channel_layout"], a["sample_format"]) == ("aac", 48000, 1, "mono", "fltp")
    assert o["asset"]["fingerprint"] and o["analysis"]["analyzer"] == "media-analysis/probe"


def test_video_stream_analysis(media, tmp_path):
    d = run(engine(tmp_path), media["av"], "video_format")["data"]
    assert d["frame_rate_mode"] == "constant" and d["measured_fps"] == 25.0 and d["frame_count"] == 150 and d["frame_count_basis"] == "nb_frames"
    assert d["display_aspect_ratio"] == "16:9" and d["sample_aspect_ratio"] == "1:1" and d["field_order"] == "progressive"
    short = run(engine(tmp_path), media["short"], "video_format")["data"]
    assert short["frame_count"] == 10 and abs(short["duration"] - 0.4) < 0.02


def test_audio_stream_analysis(media, tmp_path):
    st = run(engine(tmp_path), media["stereo"], "audio_format")["data"]
    assert (st["codec"], st["sample_rate"], st["channels"], st["channel_layout"], st["sample_format"]) == ("pcm_s24le", 48000, 2, "stereo", "s32")
    assert abs(st["duration"] - 3.0) < 0.01 and st["bitrate"] == 48000 * 2 * 24 and st["bits_per_sample"] == 24
    mo = run(engine(tmp_path), media["mono"], "audio_format")["data"]
    assert (mo["sample_rate"], mo["channels"], mo["channel_layout"]) == (44100, 1, "mono")


def test_silence_analysis(media, tmp_path):
    d = run(engine(tmp_path), media["av"], "silence", threshold_db=-50.0, min_duration=0.5)["data"]
    types = [s["type"] for s in d["segments"]]
    assert types == ["leading", "trailing"], d["segments"]
    lead, trail = d["segments"]
    assert lead["start"] == 0.0 and abs(lead["end"] - 2.0) < 0.05 and abs(lead["duration"] - 2.0) < 0.05
    assert abs(trail["start"] - 5.0) < 0.05 and trail["end"] >= 5.9
    assert d["threshold_db"] == -50.0 and d["min_duration"] == 0.5 and d["entirely_silent"] is False
    s = run(engine(tmp_path), media["silence"], "silence")["data"]
    assert s["entirely_silent"] is True and s["segments"][0]["type"] == "entire"
    # a different threshold is a different analysis (identity changes)
    e = engine(tmp_path)
    a = e.analyze({"asset_id": "asset-1", "input": str(media["av"]), "kind": "silence", "parameters": {"threshold_db": -50}})
    b = e.analyze({"asset_id": "asset-1", "input": str(media["av"]), "kind": "silence", "parameters": {"threshold_db": -30}})
    assert a["cache"]["key"] != b["cache"]["key"] and a["observation"]["analysis_id"] != b["observation"]["analysis_id"]


def test_loudness_analysis(media, tmp_path):
    d = run(engine(tmp_path), media["loud"], "loudness")["data"]
    # 1 kHz sine, amplitude 0.1: -20 dBFS peak -> -23.0 LUFS (BS.1770: 0 dBFS 997 Hz sine = -3.01 LKFS), true peak -20 dBTP
    assert abs(d["integrated_lufs"] + 23.0) <= 0.2 and abs(d["true_peak_dbtp"] + 20.0) <= 0.2 and d["loudness_range_lu"] <= 0.5
    assert d["unmeasurable"] == [] and d["integrated_below_absolute_gate"] is False
    st = run(engine(tmp_path), media["stereo"], "loudness")["data"]
    assert abs(st["integrated_lufs"] + 20.0) <= 0.2       # identical tone on two channels: +3.01 LU
    sil = run(engine(tmp_path), media["silence"], "loudness")["data"]
    assert sil["integrated_below_absolute_gate"] is True and sil["true_peak_dbtp"] is None and "true_peak_dbtp" in sil["unmeasurable"]


def test_integrity_analysis(media, tmp_path):
    good = run(engine(tmp_path), media["av"], "integrity")["data"]
    assert good["status"] == "PASS" and good["checks"]["decode"]["errors"]["error_line_count"] == 0
    assert good["checks"]["frames"]["decoded_video_frames"] == 150 == good["checks"]["frames"]["expected_video_frames"]
    assert good["checks"]["timestamps"]["streams"]["0"]["non_monotonic_dts"] == 0
    bad = run(engine(tmp_path), media["corrupt"], "integrity")["data"]
    assert bad["status"] == "FAIL" and bad["checks"]["decode"]["errors"]["error_line_count"] > 0 and bad["reasons"]
    assert bad["checks"]["decode"]["errors"]["categories"].get("corrupt_data", 0) > 0
    vo = run(engine(tmp_path), media["video_only"], "integrity")["data"]
    assert vo["status"] == "PASS"
    ao = run(engine(tmp_path), media["mono"], "integrity")["data"]
    assert ao["status"] == "PASS" and ao["checks"]["frames"]["status"] == "not_performed"


def test_multiple_streams(media, tmp_path):
    e = engine(tmp_path)
    lay = run(e, media["multi"], "stream_layout")["data"]
    assert lay["stream_count"] == 4 and lay["counts"] == {"audio": 2, "video": 2}
    s = lay["streams"]
    assert [(x["index"], x["type"], x["type_ordinal"]) for x in s] == [(0, "video", 0), (1, "video", 1), (2, "audio", 0), (3, "audio", 1)]
    assert (s[0]["width"], s[1]["width"]) == (320, 160) and (s[0]["fps"], s[1]["fps"]) == (25.0, 30.0)
    assert (s[2]["language"], s[3]["language"]) == ("eng", "jpn") and (s[2]["channels"], s[3]["channels"]) == (1, 2)
    v1 = run(e, media["multi"], "video_format", stream=1)["data"]
    assert v1["stream_index"] == 1 and v1["width"] == 160 and v1["fps"] == 30.0
    a1 = run(e, media["multi"], "audio_format", stream=1)["data"]
    assert a1["stream_index"] == 3 and a1["sample_rate"] == 44100 and a1["channels"] == 2
    l0 = run(e, media["multi"], "loudness", stream=0)["data"]
    l1 = run(e, media["multi"], "loudness", stream=1)["data"]
    assert l0["stream_index"] == 2 and l1["stream_index"] == 3 and l1["integrated_lufs"] > l0["integrated_lufs"] + 2.0   # stereo tone is ~3 LU louder
    with pytest.raises(AnalysisError) as ex:
        run(e, media["multi"], "audio_format", stream=2)
    assert ex.value.code == "INVALID_INPUT"


def test_no_audio_video(media, tmp_path):
    e = engine(tmp_path)
    p = run(e, media["video_only"], "media_probe")["data"]
    assert p["audio"] is None and p["audio_stream_count"] == 0 and p["video"]["codec"] == "h264"
    for kind in ("silence", "loudness", "audio_format"):
        with pytest.raises(AnalysisError) as ex:
            run(e, media["video_only"], kind)
        assert ex.value.code == "UNSUPPORTED_FORMAT"
    t = run(e, media["video_only"], "timing")["data"]
    assert t["av_mismatch"]["status"] == "not_performed" and t["anomaly_count"] == 0


def test_scene_detection(media, tmp_path):
    d = run(engine(tmp_path), media["scenes"], "scene_detection")["data"]
    assert [round(c["time"], 1) for c in d["cuts"]] == [2.0, 4.0] and all(c["score"] > 10 for c in d["cuts"])
    assert d["scene_count"] == 3 and [(s["start"], round(s["end"], 1)) for s in d["scenes"]] == [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)]
    assert run(engine(tmp_path), media["av"], "scene_detection")["data"]["cut_count"] == 0


def test_timing_and_duration(media, tmp_path):
    e = engine(tmp_path)
    d = run(e, media["av"], "duration")["data"]
    assert abs(d["container_duration"] - 6.0) < 0.05 and len(d["streams"]) == 2
    t = run(e, media["av"], "timing")["data"]
    assert t["av_mismatch"]["within_tolerance"] is True and t["anomaly_count"] == 0
    assert t["packet_timestamps"]["1"]["negative_pts"] == 1      # AAC priming: reported as a fact, not an anomaly
    assert t["packet_timestamps"]["0"]["packet_count"] == 150 and t["packet_timestamps"]["0"]["median_interval"] == 0.04


def test_cache_hit_skips_analyzer(media, tmp_path):
    e = engine(tmp_path, cache=True)
    first = e.analyze({"asset_id": "asset-1", "input": str(media["av"]), "kind": "loudness"})
    assert first["cache"]["status"] == "miss" and len(e.executions) == 1 and e.executions[0]["operations"][1]["executable"] == "ffmpeg"
    assert first["usage"]["analyzer_calls"] == 1 and first["usage"]["operations"][1]["executable"] == "ffmpeg"
    second = e.analyze({"asset_id": "asset-1", "input": str(media["av"]), "kind": "loudness"})
    assert second["cache"]["status"] == "hit" and len(e.executions) == 1 and second["observation"] == first["observation"]
    assert second["usage"] == {"analyzer_calls": 0, "seconds": 0.0, "operations": []}
    assert e.cache.stats() == {"directory": str(tmp_path / "cache"), "hits": 1, "misses": 1, "invalid": 0}
    # a second engine (new process) reuses the same on-disk cache
    e2 = engine(tmp_path, cache=True)
    assert e2.analyze({"asset_id": "asset-9", "input": str(media["av"]), "kind": "loudness"})["cache"]["status"] == "hit" and e2.executions == []
    # cache policies: bypass never reads or writes; only never runs an analyzer
    by = e2.analyze({"asset_id": "asset-1", "input": str(media["av"]), "kind": "loudness", "cache_policy": "bypass"})
    assert by["cache"]["status"] == "bypass" and len(e2.executions) == 1
    with pytest.raises(AnalysisError) as ex:
        e2.analyze({"asset_id": "asset-1", "input": str(media["av"]), "kind": "silence", "cache_policy": "only"})
    assert ex.value.code == "CACHE_MISS" and len(e2.executions) == 1


def test_budget_and_timeout_real(media, tmp_path):
    e = engine(tmp_path, max_analysis_calls=1)
    run(e, media["av"], "media_probe")
    with pytest.raises(AnalysisError) as ex:
        run(e, media["av"], "loudness")
    assert ex.value.code == "BUDGET_EXCEEDED" and len(e.executions) == 1
    e2 = engine(tmp_path)
    with pytest.raises(AnalysisError) as ex:
        e2.analyze({"asset_id": "asset-1", "input": str(media["av"]), "kind": "integrity", "timeout": 0.001})
    assert ex.value.code == "ANALYZER_TIMEOUT"


def test_determinism_across_runs(media, tmp_path):
    e = engine(tmp_path)
    a = e.analyze({"asset_id": "asset-1", "input": str(media["av"]), "kind": "silence", "parameters": {"threshold_db": -50}})
    b = e.analyze({"asset_id": "asset-1", "input": str(media["av"]), "kind": "silence", "parameters": {"threshold_db": -50}})
    assert a["cache"]["key"] == b["cache"]["key"] and a["observation"]["id"] == b["observation"]["id"] and a["observation"]["data"] == b["observation"]["data"]


# ---- CLI smoke (real subprocess, real ffmpeg)
def cli(*args, cwd):
    return subprocess.run([sys.executable, "-m", "media_analysis.cli", *args], cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_cli_smoke(media, tmp_path):
    r = cli("doctor", "--json", cwd=tmp_path)
    doc = json.loads(r.stdout)
    assert r.returncode == 0 and doc["status"] == "ok" and doc["checks"]["ffprobe"]["status"] == "ok" and doc["checks"]["contract"]["status"] == "ok"
    assert all(a["status"] == "available" for a in doc["checks"]["analyzer_registry"]["analyzers"]) and doc["checks"]["cache"]["writable"] is True
    r = cli("probe", str(media["av"]), "--json", cwd=tmp_path)
    doc = json.loads(r.stdout)
    assert r.returncode == 0 and doc["status"] == "ok" and doc["observations"][0]["kind"] == "media_probe" and r.stderr == ""
    r = cli("analyze", str(media["av"]), "--kind", "silence", "--kind", "loudness", "--param", "threshold_db=-50", "--json", "--cache-dir", "c", cwd=tmp_path)
    doc = json.loads(r.stdout)
    assert r.returncode == 0 and [x["kind"] for x in doc["results"]] == ["silence", "loudness"] and doc["results"][0]["cache"]["status"] == "miss"
    assert doc["usage"] == {"analyzer_calls": 2, "cache_hits": 0, "seconds": doc["usage"]["seconds"]} and doc["usage"]["seconds"] > 0
    r = cli("analyze", str(media["av"]), "--kind", "loudness", "--json", "--cache-dir", "c", cwd=tmp_path)
    doc = json.loads(r.stdout)
    assert doc["results"][0]["cache"]["status"] == "hit" and doc["usage"]["analyzer_calls"] == 0 and doc["usage"]["cache_hits"] == 1
    r = cli("analyze", str(media["av"]), "--kind", "integrity", "--dry-run", "--json", cwd=tmp_path)
    doc = json.loads(r.stdout)
    plan = doc["results"][0]["plan"]
    assert doc["dry_run"] is True and plan["operations"][1]["executable"] == "ffmpeg" and doc["usage"]["analyzer_calls"] == 0 and doc["observations"] == []
    r = cli("analyze", str(media["av"]), "--kind", "integrity", "--dry-run", cwd=tmp_path)
    assert r.returncode == 0 and r.stdout.startswith("[dry-run]") and "ffmpeg" in r.stdout
    r = cli("analyze", str(tmp_path / "missing.mp4"), "--kind", "media_probe", "--json", cwd=tmp_path)
    doc = json.loads(r.stdout)
    assert r.returncode == 3 and doc["status"] == "error" and doc["results"][0]["error_kind"] == "FILE_NOT_FOUND" and r.stderr == ""
    r = cli("analyze", str(tmp_path / "missing.mp4"), "--kind", "media_probe", cwd=tmp_path)
    assert r.returncode == 3 and r.stdout == "" and "FILE_NOT_FOUND" in r.stderr
    r = cli("analyze", str(media["av"]), "--kind", "media_probe", "--kind", "loudness", "--max-analysis-calls", "1", "--json", cwd=tmp_path)
    doc = json.loads(r.stdout)
    assert doc["status"] == "partial" and doc["results"][1]["error_kind"] == "BUDGET_EXCEEDED" and r.returncode == 10 and len(doc["observations"]) == 1
    req = tmp_path / "req.json"
    req.write_text(json.dumps({"analysis_id": "analysis-001", "asset_id": "asset-001", "input": str(media["av"]), "kind": "media_probe", "parameters": {}}))
    r = cli("run", str(req), "--json", cwd=tmp_path)
    o = json.loads(r.stdout)["observations"][0]
    assert o["analysis_id"] == "analysis-001" and o["asset_id"] == "asset-001"
    req.write_text(json.dumps({"asset_id": "a", "input": str(media["av"]), "kind": "media_probe", "argv": ["ffprobe"]}))
    r = cli("run", str(req), "--json", cwd=tmp_path)
    assert json.loads(r.stdout)["results"][0]["error_kind"] == "INVALID_INPUT" and r.returncode == 2
    r = cli("analyze", str(media["av"]), "--kind", "media_probe", "--allowed-input", str(tmp_path), "--json", cwd=tmp_path)
    assert json.loads(r.stdout)["results"][0]["error_kind"] == "PATH_NOT_ALLOWED"
    r = cli("contract", "--json", cwd=tmp_path)
    assert json.loads(r.stdout)["skill_id"] == "media-analysis"


def test_cli_run_stdin_and_batch(media, tmp_path):
    """Canonical machine interface: request document on stdin, exactly one JSON document on stdout."""
    batch = {"requests": [{"asset_id": "a1", "input": str(media["av"]), "kind": "duration"},
                          {"asset_id": "a1", "input": str(media["av"]), "kind": "audio_format", "parameters": {"stream": 5}},
                          {"asset_id": "a1", "input": str(media["av"]), "kind": "loudness"}],
             "budget": {"max_analysis_calls": 2}}
    r = subprocess.run([sys.executable, "-m", "media_analysis.cli", "run", "-", "--json"], cwd=str(tmp_path), input=json.dumps(batch),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    doc = json.loads(r.stdout)                                   # parses as one document
    assert r.stdout.count('"schema": "media-analysis/response@1"') == 1 and r.stderr == ""
    assert doc["status"] == "partial" and [x["status"] for x in doc["results"]] == ["ok", "error", "error"]
    assert doc["results"][1]["error_kind"] == "INVALID_INPUT" and doc["results"][2]["error_kind"] == "BUDGET_EXCEEDED"
    # the out-of-range stream request ran ffprobe before failing: that call is real and is reported and budgeted
    assert doc["budget"]["budget"]["max_analysis_calls"] == 2 and doc["usage"]["analyzer_calls"] == 2 and r.returncode == 2
    assert doc["results"][1]["usage"]["analyzer_calls"] == 1 and doc["results"][2]["usage"]["analyzer_calls"] == 0
    # invalid JSON on stdin -> still exactly one parseable response document, non-zero exit
    r = subprocess.run([sys.executable, "-m", "media_analysis.cli", "run", "-", "--json"], cwd=str(tmp_path), input="{not json",
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    doc = json.loads(r.stdout)
    assert doc["status"] == "error" and doc["error_kind"] == "INVALID_INPUT" and doc["results"] == [] and r.returncode == 2
    # unknown budget field is rejected, not ignored
    r = subprocess.run([sys.executable, "-m", "media_analysis.cli", "run", "-", "--json"], cwd=str(tmp_path),
                       input=json.dumps({"requests": batch["requests"][:1], "budget": {"max_gpu_seconds": 1}}), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    doc = json.loads(r.stdout)
    assert doc["status"] == "error" and doc["error_kind"] == "INVALID_INPUT" and "max_gpu_seconds" in json.dumps(doc["error"])


def test_timeout_kills_process_group_and_leaves_no_cache(media, tmp_path):
    """After ANALYZER_TIMEOUT no ffmpeg keeps running on the input and no cache entry exists for that identity."""
    e = engine(tmp_path, cache=True)
    with pytest.raises(AnalysisError) as ex:
        e.analyze({"asset_id": "asset-1", "input": str(media["av"]), "kind": "integrity", "timeout": 0.001})
    assert ex.value.code == "ANALYZER_TIMEOUT"
    assert not list((tmp_path / "cache").rglob("*.json"))
    if Path("/proc").is_dir():
        alive = []
        for pid in Path("/proc").iterdir():
            if pid.name.isdigit():
                try:
                    cmd = (pid / "cmdline").read_bytes()
                except OSError:
                    continue
                if b"ffmpeg" in cmd and str(media["av"]).encode() in cmd:
                    alive.append(pid.name)
        assert alive == []
    assert e.tracker.calls == 1 and e.executions[0]["kind"] == "integrity"


def test_all_kinds_real_media(media, tmp_path):
    """Every declared kind runs on real media and the response validates against the published schemas."""
    from media_analysis.contract import ANALYSIS_KINDS, PARAMETER_SCHEMAS, skill_contract
    from media_analysis.schemas import RESPONSE_SCHEMA, contract_refs, validate
    e = engine(tmp_path, cache=True)
    reqs = [{"asset_id": "asset-1", "input": str(media["multi"]), "kind": k} for k in ANALYSIS_KINDS]
    doc = e.run({"requests": reqs})
    assert doc["status"] == "ok" and [o["kind"] for o in doc["observations"]] == list(ANALYSIS_KINDS)
    refs = contract_refs(list(ANALYSIS_KINDS), PARAMETER_SCHEMAS)
    assert validate(doc, RESPONSE_SCHEMA, refs) == []
    again = e.run({"requests": reqs})
    assert again["usage"]["analyzer_calls"] == 0 and again["usage"]["cache_hits"] == len(ANALYSIS_KINDS) and again["observations"] == doc["observations"]
    assert len(e.executions) == len(ANALYSIS_KINDS)
    assert {t["tool_id"] for t in skill_contract()["tools"]} == {ex["analyzer"] for ex in e.executions}


def _run_stdin(doc, cwd, extra=()):
    r = subprocess.run([sys.executable, "-m", "media_analysis.cli", "run", "-", "--json", *extra], cwd=str(cwd),
                       input=doc if isinstance(doc, str) else json.dumps(doc), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    parsed = json.loads(r.stdout)                      # exactly one parseable document, always
    assert r.stdout.count('"schema": "media-analysis/response@1"') == 1
    return r, parsed


INVALID_INPUTS = [
    ("invalid_json", "{not json", None, "INVALID_INPUT"),
    ("missing_asset", {"input": "{av}", "kind": "media_probe"}, "results", "INVALID_INPUT"),
    ("missing_kind", {"asset_id": "a", "input": "{av}"}, "results", "INVALID_INPUT"),
    ("unknown_kind", {"asset_id": "a", "input": "{av}", "kind": "speaker_detection"}, "results", "INVALID_INPUT"),
    ("invalid_parameter", {"asset_id": "a", "input": "{av}", "kind": "silence", "parameters": {"threshold_db": 10}}, "results", "INVALID_INPUT"),
    ("unknown_parameter", {"asset_id": "a", "input": "{av}", "kind": "silence", "parameters": {"gain": 1}}, "results", "INVALID_INPUT"),
    ("path_traversal", {"asset_id": "a", "input": "{cwd}/../../{avname}", "kind": "media_probe"}, "results", "FILE_NOT_FOUND"),
    ("nonexistent_file", {"asset_id": "a", "input": "{cwd}/missing.mp4", "kind": "media_probe"}, "results", "FILE_NOT_FOUND"),
    ("unsupported_media", {"asset_id": "a", "input": "{text}", "kind": "media_probe"}, "results", "UNSUPPORTED_FORMAT"),
    ("command_field", {"asset_id": "a", "input": "{av}", "kind": "media_probe", "command": "rm -rf /"}, "results", "INVALID_INPUT"),
    ("executable_override", {"asset_id": "a", "input": "{av}", "kind": "media_probe", "ffmpeg": "/tmp/evil"}, "results", "INVALID_INPUT"),
]


@pytest.mark.parametrize("name,doc,where,code", INVALID_INPUTS, ids=[c[0] for c in INVALID_INPUTS])
def test_invalid_input_is_a_parseable_error_response(media, tmp_path, name, doc, where, code):
    text = tmp_path / "notmedia.txt"
    text.write_text("hello")
    if isinstance(doc, dict):
        doc = json.loads(json.dumps(doc).replace("{av}", str(media["av"]).replace("\\", "\\\\")).replace("{cwd}", str(tmp_path).replace("\\", "\\\\"))
                         .replace("{avname}", media["av"].name).replace("{text}", str(text).replace("\\", "\\\\")))
    r, parsed = _run_stdin(doc, tmp_path)
    assert r.returncode != 0 and parsed["status"] == "error" and parsed["observations"] == [] and r.stderr == ""
    if where is None:
        assert parsed["error_kind"] == code and parsed["results"] == []
    else:
        assert parsed["results"][0]["error_kind"] == code and parsed["results"][0]["status"] == "error"


def test_absolute_and_relative_inputs(media, tmp_path):
    """Absolute paths and paths relative to the process working directory both resolve; the observation records the
    resolved absolute path and the content fingerprint, so the same file under two spellings has one identity."""
    import shutil
    local = tmp_path / "local.mp4"
    shutil.copy(media["av"], local)
    r1, d1 = _run_stdin({"asset_id": "a", "input": "local.mp4", "kind": "duration"}, tmp_path)
    r2, d2 = _run_stdin({"asset_id": "a", "input": str(local), "kind": "duration"}, tmp_path)
    o1, o2 = d1["observations"][0], d2["observations"][0]
    assert d1["status"] == d2["status"] == "ok" and o1["id"] == o2["id"] and o1["asset"]["path"] == o2["asset"]["path"] == str(local.resolve())
    r3, d3 = _run_stdin({"asset_id": "a", "input": str(local), "kind": "duration"}, tmp_path, extra=["--allowed-input", str(tmp_path / "elsewhere")])
    assert d3["results"][0]["error_kind"] == "PATH_NOT_ALLOWED"


def test_timeout_keeps_the_protocol(media, tmp_path):
    """A timeout inside a batch: parseable response, ANALYZER_TIMEOUT for that request, the others still served,
    no cache entry for the timed-out identity, no ffmpeg left behind."""
    batch = {"requests": [{"asset_id": "a", "input": str(media["av"]), "kind": "duration"},
                          {"asset_id": "a", "input": str(media["av"]), "kind": "integrity", "timeout": 0.001},
                          {"asset_id": "a", "input": str(media["av"]), "kind": "audio_format"}]}
    r, doc = _run_stdin(batch, tmp_path, extra=["--cache-dir", "c"])
    assert doc["status"] == "partial" and [x["status"] for x in doc["results"]] == ["ok", "error", "ok"]
    assert doc["results"][1]["error_kind"] == "ANALYZER_TIMEOUT" and r.returncode == 7 and r.stderr == ""
    assert len(doc["observations"]) == 2 and [o["kind"] for o in doc["observations"]] == ["duration", "audio_format"]
    entries = list((tmp_path / "c").rglob("*.json"))
    assert len(entries) == 2 and all(json.loads(e.read_text())["metadata"]["kind"] != "integrity" for e in entries)
    if Path("/proc").is_dir():
        for pid in Path("/proc").iterdir():
            if pid.name.isdigit():
                try:
                    cmd = (pid / "cmdline").read_bytes()
                except OSError:
                    continue
                assert not (b"ffmpeg" in cmd and str(media["av"]).encode() in cmd)


# ---- real media matrix: every kind on every fixture yields a contract-conformant ok or error result
AUDIO_KINDS = {"audio_format", "silence", "loudness"}
VIDEO_KINDS = {"video_format", "scene_detection"}


def test_real_media_matrix(media, tmp_path):
    from media_analysis.contract import ANALYSIS_KINDS, PARAMETER_SCHEMAS
    from media_analysis.schemas import RESPONSE_SCHEMA, contract_refs, validate
    refs = contract_refs(list(ANALYSIS_KINDS), PARAMETER_SCHEMAS)
    e = engine(tmp_path, cache=True)
    expectations = {
        "av": {}, "multi": {}, "short": {}, "scenes": {k: "UNSUPPORTED_FORMAT" for k in AUDIO_KINDS},
        "video_only": {k: "UNSUPPORTED_FORMAT" for k in AUDIO_KINDS},
        "mono": {k: "UNSUPPORTED_FORMAT" for k in VIDEO_KINDS}, "stereo": {k: "UNSUPPORTED_FORMAT" for k in VIDEO_KINDS},
        "silence": {k: "UNSUPPORTED_FORMAT" for k in VIDEO_KINDS}, "loud": {k: "UNSUPPORTED_FORMAT" for k in VIDEO_KINDS},
        "corrupt": {},   # ffprobe still opens it; integrity reports FAIL as data, not as an error
    }
    for name, expected_errors in expectations.items():
        doc = e.run({"requests": [{"asset_id": name, "input": str(media[name]), "kind": k} for k in ANALYSIS_KINDS]})
        assert validate(doc, RESPONSE_SCHEMA, refs) == [], (name, validate(doc, RESPONSE_SCHEMA, refs))
        for res in doc["results"]:
            if res["kind"] in expected_errors:
                assert res["status"] == "error" and res["error_kind"] == expected_errors[res["kind"]], (name, res["kind"], res.get("error"))
            else:
                assert res["status"] == "ok", (name, res["kind"], res.get("error"))
                assert res["observation"]["asset_id"] == name and res["observation"]["kind"] == res["kind"]
        assert doc["status"] == ("ok" if not expected_errors else "partial")
    corrupt = e.run({"asset_id": "corrupt", "input": str(media["corrupt"]), "kind": "integrity"})["observations"][0]["data"]
    assert corrupt["status"] == "FAIL"
    # every ok result is now cached; errors are never cached, so the second pass costs exactly one analyzer call per
    # error result (a failed analyzer run is reported as usage, not hidden)
    before = len(e.executions)
    errors = sum(len(v) for v in expectations.values())
    for name, expected_errors in expectations.items():
        again = e.run({"requests": [{"asset_id": name, "input": str(media[name]), "kind": k} for k in ANALYSIS_KINDS]})
        assert again["usage"]["analyzer_calls"] == len(expected_errors) and again["usage"]["cache_hits"] == len(ANALYSIS_KINDS) - len(expected_errors)
        for res in again["results"]:
            assert res["usage"]["analyzer_calls"] == (1 if res["status"] == "error" else 0)
    assert len(e.executions) == before + errors


def test_deterministic_result_across_processes(media, tmp_path):
    """Two separate processes, no cache: identical observation id, identity, data and asset fingerprint; only
    observed_at / seconds (explicitly outside the identity) may differ."""
    docs = []
    for _ in range(2):
        r, doc = _run_stdin({"requests": [{"asset_id": "a", "input": str(media["av"]), "kind": k} for k in ("silence", "loudness", "video_format", "timing")]}, tmp_path)
        docs.append(doc)
    for o1, o2 in zip(docs[0]["observations"], docs[1]["observations"]):
        assert o1["id"] == o2["id"] and o1["analysis"]["identity"] == o2["analysis"]["identity"] and o1["data"] == o2["data"]
        assert o1["asset"] == o2["asset"] and o1["source"] == o2["source"] and o1["analysis"]["parameters"] == o2["analysis"]["parameters"]
    text = json.dumps(docs[0])
    assert str(tmp_path / "c") not in text and "pid" not in text.lower()


def test_non_ascii_path_on_non_utf8_pipe(media, tmp_path):
    """A Japanese file name must round-trip through the JSON protocol even when the pipe encoding is cp1252 / cp932
    (Windows default for redirected stdout). Before the fix this raised UnicodeEncodeError and printed no JSON."""
    import shutil
    name = "会議_収録 テスト.mp4"
    target = tmp_path / name
    shutil.copy(media["av"], target)
    for enc in ("cp1252", "cp932", "ascii"):
        env = dict(os.environ, PYTHONIOENCODING=enc)
        env.pop("PYTHONUTF8", None)
        r = subprocess.run([sys.executable, "-m", "media_analysis.cli", "analyze", str(target), "--kind", "duration", "--json"], cwd=str(tmp_path),
                           env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        doc = json.loads(r.stdout.decode("utf-8"))
        assert r.returncode == 0 and doc["status"] == "ok" and r.stderr == b"", (enc, r.stderr[-300:])
        assert doc["observations"][0]["asset"]["path"] == str(target.resolve()) and name in doc["observations"][0]["asset"]["path"]
        # human-readable mode never crashes either (unencodable characters are escaped, not fatal)
        r = subprocess.run([sys.executable, "-m", "media_analysis.cli", "probe", str(target)], cwd=str(tmp_path), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert r.returncode == 0 and b"[media_probe]" in r.stdout, (enc, r.stderr[-300:])
        # request document with the non-ASCII path, over stdin
        req = json.dumps({"asset_id": "a", "input": str(target), "kind": "media_probe"}, ensure_ascii=False).encode("utf-8")
        r = subprocess.run([sys.executable, "-m", "media_analysis.cli", "run", "-", "--json"], cwd=str(tmp_path), env=env, input=req, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        doc = json.loads(r.stdout.decode("utf-8"))
        assert r.returncode == 0 and doc["status"] == "ok", (enc, r.stderr[-300:])
    # error documents carry the path too and must survive the same way
    env = dict(os.environ, PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, "-m", "media_analysis.cli", "analyze", str(tmp_path / "無い.mp4"), "--kind", "duration", "--json"], cwd=str(tmp_path),
                       env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    doc = json.loads(r.stdout.decode("utf-8"))
    assert r.returncode == 3 and doc["results"][0]["error_kind"] == "FILE_NOT_FOUND" and "無い" in doc["results"][0]["error"]["message"]
