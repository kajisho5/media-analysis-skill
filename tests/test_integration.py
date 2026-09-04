"""Integration tests: real ffmpeg / ffprobe on generated fixtures (tests/fixtures/generate.py). Never mocked, never
skipped: a missing FFmpeg fails the session."""
from __future__ import annotations

import json
import subprocess
import sys

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
    assert a["cache_key"] != b["cache_key"] and a["observation"]["analysis_id"] != b["observation"]["analysis_id"]


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
    assert first["cache"] == "miss" and len(e.executions) == 1 and e.executions[0]["operations"][1]["executable"] == "ffmpeg"
    second = e.analyze({"asset_id": "asset-1", "input": str(media["av"]), "kind": "loudness"})
    assert second["cache"] == "hit" and len(e.executions) == 1 and second["observation"] == first["observation"]
    assert e.cache.stats() == {"directory": str(tmp_path / "cache"), "hits": 1, "misses": 1, "invalid": 0}
    # a second engine (new process) reuses the same on-disk cache
    e2 = engine(tmp_path, cache=True)
    assert e2.analyze({"asset_id": "asset-9", "input": str(media["av"]), "kind": "loudness"})["cache"] == "hit" and e2.executions == []


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
    assert a["cache_key"] == b["cache_key"] and a["observation"]["id"] == b["observation"]["id"] and a["observation"]["data"] == b["observation"]["data"]


# ---- CLI smoke (real subprocess, real ffmpeg)
def cli(*args, cwd):
    return subprocess.run([sys.executable, "-m", "media_analysis.cli", *args], cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_cli_smoke(media, tmp_path):
    r = cli("doctor", "--json", cwd=tmp_path)
    doc = json.loads(r.stdout)
    assert r.returncode == 0 and doc["capabilities"]["ffprobe"]["status"] == "AVAILABLE" and all(a["status"] == "available" for a in doc["analyzers"])
    r = cli("probe", str(media["av"]), "--json", cwd=tmp_path)
    assert r.returncode == 0 and json.loads(r.stdout)["observation"]["kind"] == "media_probe" and r.stderr == ""
    r = cli("analyze", str(media["av"]), "--kind", "silence", "--kind", "loudness", "--param", "threshold_db=-50", "--json", "--cache-dir", "c", cwd=tmp_path)
    doc = json.loads(r.stdout)
    assert r.returncode == 0 and [x["observation"]["kind"] for x in doc["results"]] == ["silence", "loudness"] and doc["results"][0]["cache"] == "miss"
    r = cli("analyze", str(media["av"]), "--kind", "loudness", "--json", "--cache-dir", "c", cwd=tmp_path)
    assert json.loads(r.stdout)["cache"] == "hit"
    r = cli("analyze", str(media["av"]), "--kind", "integrity", "--dry-run", "--json", cwd=tmp_path)
    plan = json.loads(r.stdout)
    assert plan["dry_run"] is True and plan["operations"][1]["executable"] == "ffmpeg" and not (tmp_path / "c" / "zz").exists()
    r = cli("analyze", str(media["av"]), "--kind", "integrity", "--dry-run", cwd=tmp_path)
    assert r.returncode == 0 and r.stdout.startswith("[dry-run]") and "ffmpeg" in r.stdout
    r = cli("analyze", str(tmp_path / "missing.mp4"), "--kind", "media_probe", "--json", cwd=tmp_path)
    assert r.returncode != 0 and json.loads(r.stdout)["error"]["code"] == "FILE_NOT_FOUND"
    r = cli("analyze", str(tmp_path / "missing.mp4"), "--kind", "media_probe", cwd=tmp_path)
    assert r.returncode != 0 and r.stdout == "" and "FILE_NOT_FOUND" in r.stderr
    r = cli("analyze", str(media["av"]), "--kind", "media_probe", "--kind", "loudness", "--max-analysis-calls", "1", "--json", cwd=tmp_path)
    assert json.loads(r.stdout)["error"]["code"] == "BUDGET_EXCEEDED"
    req = tmp_path / "req.json"
    req.write_text(json.dumps({"analysis_id": "analysis-001", "asset_id": "asset-001", "input": str(media["av"]), "kind": "media_probe", "parameters": {}}))
    r = cli("run", str(req), "--json", cwd=tmp_path)
    o = json.loads(r.stdout)["observation"]
    assert o["analysis_id"] == "analysis-001" and o["asset_id"] == "asset-001"
    req.write_text(json.dumps({"asset_id": "a", "input": str(media["av"]), "kind": "media_probe", "argv": ["ffprobe"]}))
    r = cli("run", str(req), "--json", cwd=tmp_path)
    assert json.loads(r.stdout)["error"]["code"] == "INVALID_INPUT"
    r = cli("analyze", str(media["av"]), "--kind", "media_probe", "--allowed-input", str(tmp_path), "--json", cwd=tmp_path)
    assert json.loads(r.stdout)["error"]["code"] == "PATH_NOT_ALLOWED"
    r = cli("contract", "--json", cwd=tmp_path)
    assert json.loads(r.stdout)["skill_id"] == "media-analysis"
