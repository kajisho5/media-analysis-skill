"""Unit tests: contracts, parsers, identity / cache / budget, verification and security. No ffmpeg required except
where a test explicitly uses a fake capability set."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from media_analysis import SKILL_ID, VERSION
from media_analysis.analyzers.base import AnalysisContext, Analyzer
from media_analysis.analyzers.integrity import classify_errors, decide_status, parse_progress
from media_analysis.analyzers.loudness import parse_ebur128
from media_analysis.analyzers.scenes import build_scenes, parse_scdet
from media_analysis.analyzers.silence import classify, parse_silencedetect
from media_analysis.budget import Budget, BudgetTracker
from media_analysis.cache import ObservationCache
from media_analysis.canonical import canonical_json
from media_analysis.capabilities import Capability, CapabilitySet
from media_analysis.contract import (ANALYSIS_KINDS, KIND_TO_TOOL, PARAMETER_SCHEMAS, AnalysisRequest, analysis_identity, make_observation,
                                     skill_contract, source_for, validate_parameters)
from media_analysis.engine import AnalysisEngine
from media_analysis.errors import ERROR_CODES, EXIT_CODES, AnalysisError
from media_analysis.packets import frame_rate_mode, parse_packets, timestamp_report
from media_analysis.probe import parse_ffprobe, select_stream, streams_of
from media_analysis.registry import AnalyzerRegistry, default_registry
from media_analysis.security import PathPolicy
from media_analysis.verify import verify_observation

RAW_PROBE = {
    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "format_long_name": "QuickTime / MOV", "duration": "6.000000", "size": "208685",
               "bit_rate": "278246", "start_time": "0.000000", "nb_streams": "4", "probe_score": 100},
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "profile": "High", "width": 320, "height": 180, "coded_width": 320, "coded_height": 180,
         "sample_aspect_ratio": "1:1", "display_aspect_ratio": "16:9", "pix_fmt": "yuv420p", "color_space": "bt709", "color_transfer": "bt709",
         "color_primaries": "bt709", "color_range": "tv", "field_order": "progressive", "r_frame_rate": "25/1", "avg_frame_rate": "25/1",
         "time_base": "1/12800", "start_time": "0.000000", "duration": "6.000000", "bit_rate": "233082", "nb_frames": "150", "bits_per_raw_sample": "8",
         "disposition": {"default": 1, "attached_pic": 0}, "tags": {"language": "und"}},
        {"index": 1, "codec_type": "video", "codec_name": "mjpeg", "width": 100, "height": 100, "r_frame_rate": "0/0", "avg_frame_rate": "0/0",
         "disposition": {"default": 0, "attached_pic": 1}},
        {"index": 2, "codec_type": "audio", "codec_name": "aac", "profile": "LC", "sample_rate": "48000", "channels": 1, "channel_layout": "mono",
         "sample_fmt": "fltp", "start_time": "0.000000", "duration": "6.000000", "bit_rate": "37060", "nb_frames": "283", "disposition": {"default": 1},
         "tags": {"language": "eng"}},
        {"index": 3, "codec_type": "audio", "codec_name": "aac", "sample_rate": "44100", "channels": 2, "channel_layout": "stereo", "sample_fmt": "fltp",
         "duration": "5.900000", "disposition": {}, "tags": {"language": "jpn"}},
    ],
}


# ---- 1-3 contract / request / kind
def test_contract_declares_only_implemented_tools():
    c = skill_contract()
    assert c["skill_id"] == SKILL_ID and "/" not in c["skill_id"] and c["version"] == VERSION
    reg = default_registry()
    assert {t["tool_id"] for t in c["tools"]} == {a.to_dict()["tool_id"] for a in reg.all()}
    assert set(c["analysis_kinds"]) == set(ANALYSIS_KINDS) == set(KIND_TO_TOOL)
    for t in c["tools"]:
        assert t["tool_id"].startswith(SKILL_ID + "/") and t["tool_id"].count("/") == 1
        assert t["produces_output"] is False and t["deterministic"] is True
        for k in t["kinds"]:
            assert KIND_TO_TOOL[k] == t["tool_id"].split("/")[1]


def test_registry_rejects_contract_mismatch():
    class Extra(Analyzer):
        id, supported_kinds, required_capabilities = "future", ("future_kind",), ()
    with pytest.raises(ValueError):
        AnalyzerRegistry(default_registry().all() + [Extra()])


def test_request_validation():
    r = AnalysisRequest.from_dict({"analysis_id": "analysis-001", "asset_id": "asset-001", "input": "sample.mp4", "kind": "media_probe", "parameters": {}})
    assert r.tool == "probe" and r.output_policy == {"round": 3}
    for bad, code in [({"asset_id": "a", "input": "x"}, "INVALID_INPUT"), ({"asset_id": "a", "input": "", "kind": "media_probe"}, "INVALID_INPUT"),
                      ({"asset_id": "bad id!", "input": "x", "kind": "media_probe"}, "INVALID_INPUT"),
                      ({"asset_id": "a", "input": "x", "kind": "media_probe", "timeout": -1}, "INVALID_INPUT"),
                      ({"asset_id": "a", "input": "x", "kind": "media_probe", "extra": 1}, "INVALID_INPUT"),
                      ("not a dict", "INVALID_INPUT")]:
        with pytest.raises(AnalysisError) as e:
            AnalysisRequest.from_dict(bad)
        assert e.value.code == code


def test_request_rejects_command_and_argv():
    for field in ("command", "argv", "args", "shell", "cmd", "exec", "filter_complex", "filter", "api_key", "token", "env"):
        with pytest.raises(AnalysisError) as e:
            AnalysisRequest.from_dict({"asset_id": "a", "input": "x.mp4", "kind": "media_probe", field: ["rm", "-rf", "/"]})
        assert e.value.code == "INVALID_INPUT" and field in e.value.details["fields"]
        # nested inside parameters (one level and deeper): rejected as forbidden, never silently stripped
        with pytest.raises(AnalysisError) as e:
            AnalysisRequest.from_dict({"asset_id": "a", "input": "x.mp4", "kind": "silence", "parameters": {field: "x"}})
        assert e.value.code == "INVALID_INPUT" and f"parameters.{field}" in e.value.details["fields"]
        with pytest.raises(AnalysisError) as e:
            AnalysisRequest.from_dict({"asset_id": "a", "input": "x.mp4", "kind": "silence", "parameters": {"stream": 0}, "output_policy": {"nested": [{field.upper(): 1}]}})
        assert e.value.code == "INVALID_INPUT" and "output_policy.nested[0]." in e.value.details["fields"][0]


def test_kind_validation():
    with pytest.raises(AnalysisError) as e:
        AnalysisRequest.from_dict({"asset_id": "a", "input": "x", "kind": "semantic_scene"})
    assert e.value.code == "INVALID_INPUT" and "semantic_scene" in str(e.value)


# ---- 15 parameter validation
def test_parameter_validation():
    p = validate_parameters("silence", {"threshold_db": -30})
    assert p == {"stream": 0, "threshold_db": -30.0, "min_duration": 0.5, "edge_tolerance": 0.1}
    for kind, params in [("silence", {"threshold_db": 5}), ("silence", {"nope": 1}), ("silence", {"stream": "0"}), ("silence", {"stream": True}),
                         ("scene_detection", {"threshold": float("nan")}), ("media_probe", {"stream": 0}), ("silence", {"min_duration": 0})]:
        with pytest.raises(AnalysisError) as e:
            validate_parameters(kind, params)
        assert e.value.code == "INVALID_INPUT"
    assert all(k in KIND_TO_TOOL for k in PARAMETER_SCHEMAS)


# ---- 4-7 parsers
def test_probe_parser():
    p = parse_ffprobe(RAW_PROBE)
    assert p["container"]["duration"] == 6.0 and p["container"]["size"] == 208685 and p["container"]["stream_count"] == 4
    v = p["streams"][0]
    assert v["video"]["fps"] == 25.0 and v["video"]["display_aspect_ratio"] == "16:9" and v["video"]["color_transfer"] == "bt709"
    assert v["nb_frames"] == 150 and v["disposition"] == ["default"]
    assert p["streams"][1]["video"]["fps"] is None  # 0/0 is not a rate
    a = p["streams"][2]
    assert a["audio"] == {"sample_rate": 48000, "channels": 1, "channel_layout": "mono", "sample_format": "fltp", "bits_per_sample": None}


def test_stream_parser_keeps_streams_apart():
    p = parse_ffprobe(RAW_PROBE)
    vids, auds = streams_of(p, "video"), streams_of(p, "audio")
    assert [s["index"] for s in vids] == [0]          # attached picture excluded
    assert [s["index"] for s in auds] == [2, 3]
    assert select_stream(p, "audio", 1)["language"] == "jpn"
    with pytest.raises(AnalysisError) as e:
        select_stream(p, "audio", 2)
    assert e.value.code == "INVALID_INPUT"
    with pytest.raises(AnalysisError) as e:
        select_stream({"container": {}, "streams": [auds[0]]}, "video", 0)
    assert e.value.code == "UNSUPPORTED_FORMAT"


def test_video_parser_frame_rate_mode():
    cfr = parse_packets("\n".join(f"0|{i * 0.04:.6f}|{i * 0.04:.6f}|0.04|K__" for i in range(50)))
    assert frame_rate_mode(cfr)["mode"] == "constant"
    vfr = parse_packets("\n".join(f"0|{t}|{t}|N/A|___" for t in (0, 0.04, 0.08, 0.2, 0.24, 0.5)))
    assert frame_rate_mode(vfr)["mode"] == "variable"
    assert frame_rate_mode(parse_packets("0|0|0|N/A|K__\n0|0.04|0.04|N/A|___"))["mode"] == "unknown"


def test_audio_parser_through_analyzer(monkeypatch):
    from media_analysis.analyzers.probe import AudioAnalyzer
    ctx = AnalysisContext("/x", CapabilitySet(), None)
    ctx._probe = parse_ffprobe(RAW_PROBE)
    d = AudioAnalyzer().analyze(ctx, "audio_format", validate_parameters("audio_format", {"stream": 1}))
    assert d["stream_index"] == 3 and d["channels"] == 2 and d["channel_layout"] == "stereo" and d["sample_rate"] == 44100 and d["duration"] == 5.9


# ---- 8 silence parser
def test_silence_parser_and_classification():
    err = "[silencedetect @ 0x1] silence_start: 0\n[silencedetect @ 0x1] silence_end: 2.85 | silence_duration: 2.85\n" \
          "[silencedetect @ 0x1] silence_start: 4.0\n[silencedetect @ 0x1] silence_end: 4.9 | silence_duration: 0.9\n[silencedetect @ 0x1] silence_start: 9.95\n"
    segs = classify(parse_silencedetect(err), 10.0, 0.1)
    assert [s["type"] for s in segs] == ["leading", "internal", "trailing"]
    assert segs[0] == {"start": 0.0, "end": 2.85, "duration": 2.85, "type": "leading", "runs_to_end": False}
    assert segs[2]["end"] == 10.0 and segs[2]["runs_to_end"] is True
    assert classify([(0.0, None)], 5.0, 0.1)[0]["type"] == "entire"


# ---- 9 loudness parser
def test_loudness_parser():
    text = ("  Integrated loudness:\n    I:         -23.0 LUFS\n    Threshold: -33.0 LUFS\n\n  Loudness range:\n    LRA:         0.0 LU\n"
            "    Threshold: -43.0 LUFS\n    LRA low:   -23.0 LUFS\n    LRA high:  -23.0 LUFS\n\n  True peak:\n    Peak:      -20.0 dBFS\n")
    d = parse_ebur128(text)
    assert d["integrated_lufs"] == -23.0 and d["true_peak_dbtp"] == -20.0 and d["loudness_range_lu"] == 0.0 and d["unmeasurable"] == []
    silent = text.replace("-23.0 LUFS\n    Threshold: -33.0", "-70.0 LUFS\n    Threshold: 0.0").replace("Peak:      -20.0", "Peak:      -inf")
    d = parse_ebur128(silent)
    assert d["true_peak_dbtp"] is None and "true_peak_dbtp" in d["unmeasurable"] and d["integrated_below_absolute_gate"] is True
    with pytest.raises(AnalysisError):
        parse_ebur128("no summary here")


# ---- 10-11 integrity result / malformed result
def test_integrity_result_status():
    clean = {"status": "performed", "exit_code": 0, "errors": classify_errors("", 10)}
    frames = {"status": "performed", "expected_video_frames": 150, "decoded_video_frames": 150}
    ts = {"status": "performed", "streams": {"0": timestamp_report(parse_packets("0|0|0|0.04|K__\n0|0.04|0.04|0.04|___\n0|0.08|0.08|0.04|___"), 2.5)}}
    assert decide_status(clean, frames, ts)["status"] == "PASS"
    bad = {"status": "performed", "exit_code": 0, "errors": classify_errors("[h264 @ 0x1] Invalid NAL unit size (0 > 2072).\n[h264 @ 0x1] no frame!", 10)}
    v = decide_status(bad, frames, ts)
    assert v["status"] == "FAIL" and bad["errors"]["categories"] == {"corrupt_data": 1, "missing_reference": 1}
    gappy = "\n".join(f"0|{t}|{t}|0.04|___" for t in (0, 0.04, 0.08, 0.12, 0.16, 1.0, 1.04, 1.08))
    gaps = {"status": "performed", "streams": {"0": timestamp_report(parse_packets(gappy), 2.5)}}
    assert decide_status(clean, frames, gaps)["status"] == "WARN"
    assert decide_status(clean, {"status": "performed", "expected_video_frames": 150, "decoded_video_frames": 148}, ts)["status"] == "WARN"
    assert parse_progress("frame=12\nout_time_us=480000\nprogress=end\n") == {"decoded_video_frames": 12, "decoded_time": 0.48}


def test_scene_parser():
    out = "frame:0    pts:0       pts_time:0\nlavfi.scd.score=0.000\nframe:50   pts:25600   pts_time:2\nlavfi.scd.mafd=30.395\nlavfi.scd.score=29.367\nlavfi.scd.time=2\n"
    cuts = parse_scdet(out)
    assert cuts == [{"frame": 50, "time": 2.0, "score": 29.367}]
    scenes = build_scenes(cuts, 6.0, 0.5)
    assert [(s["start"], s["end"]) for s in scenes] == [(0.0, 2.0), (2.0, 6.0)] and scenes[1]["cut_score"] == 29.367


def _obs(**over):
    base = dict(identity="ab" * 32, asset_id="asset-1", kind="media_probe", tool="probe", analyzer_version=VERSION, data={"container": {"duration": 6.0}},
                analysis_id="analysis-1", observed_at="2026-09-04T00:00:00Z", parameters={}, asset={"path": "/x", "fingerprint": "f", "size": 1}, seconds=0.1)
    base.update(over)
    return make_observation(**base)


def test_malformed_result_rejected():
    class Broken(Analyzer):
        id, supported_kinds, required_capabilities = "probe", ("media_probe",), ("ffprobe",)
        def analyze(self, ctx, kind, parameters):
            return ["not", "an", "object"]
    with pytest.raises(AnalysisError) as e:
        verify_observation({"id": "x"}, expected_asset_id="a", expected_kind="media_probe", expected_analysis_id="i")
    assert e.value.code == "VERIFICATION_FAILED"
    o = _obs(data={"container": {"duration": float("nan")}})
    with pytest.raises(AnalysisError) as e:
        verify_observation(o, expected_asset_id="asset-1", expected_kind="media_probe", expected_analysis_id="analysis-1")
    assert "non-finite" in json.dumps(e.value.details)
    o = _obs(data={"segments": [{"start": 99.0, "end": 100.0}]})
    with pytest.raises(AnalysisError):
        verify_observation(o, expected_asset_id="asset-1", expected_kind="media_probe", expected_analysis_id="analysis-1", duration=6.0)


# ---- 12-14 wrong asset / kind / source
def test_wrong_asset_kind_source():
    o = _obs()
    verify_observation(o, expected_asset_id="asset-1", expected_kind="media_probe", expected_analysis_id="analysis-1")
    for kw in [dict(expected_asset_id="asset-2", expected_kind="media_probe", expected_analysis_id="analysis-1"),
               dict(expected_asset_id="asset-1", expected_kind="silence", expected_analysis_id="analysis-1"),
               dict(expected_asset_id="asset-1", expected_kind="media_probe", expected_analysis_id="other")]:
        with pytest.raises(AnalysisError) as e:
            verify_observation(o, **kw)
        assert e.value.code == "VERIFICATION_FAILED"
    for src in ("ffmpeg-skill/probe@0.8.4", "media-analysis/probe", "ai:provider@1", "AI_GENERATED", "media-analysis@0.1.0"):
        bad = dict(o, source=src)
        with pytest.raises(AnalysisError):
            verify_observation(bad, expected_asset_id="asset-1", expected_kind="media_probe", expected_analysis_id="analysis-1")
    assert source_for("probe") == f"media-analysis/probe@{VERSION}" and o["source"] == source_for("probe")


# ---- 16 deterministic identity / cache key
def test_deterministic_cache_key():
    a = analysis_identity("fp", "silence", "0.1.0", "silence", {"threshold_db": -40.0, "min_duration": 0.5})
    b = analysis_identity("fp", "silence", "0.1.0", "silence", {"min_duration": 0.5, "threshold_db": -40.0})
    assert a == b and len(a) == 64
    assert canonical_json({"b": 1, "a": [1.5, {"z": None}]}) == '{"a":[1.5,{"z":null}],"b":1}'
    assert analysis_identity("fp2", "silence", "0.1.0", "silence", {}) != a
    assert analysis_identity("fp", "silence", "0.1.1", "silence", {}) != analysis_identity("fp", "silence", "0.1.0", "silence", {})
    assert analysis_identity("fp", "silence", "0.1.0", "silence", {"threshold_db": -30.0}) != a
    with pytest.raises(ValueError):
        canonical_json({"x": float("inf")})


# ---- fake analyzer environment for engine tests
class CountingAnalyzer(Analyzer):
    id, supported_kinds, required_capabilities = "probe", ("media_probe",), ("ffprobe",)
    def __init__(self):
        self.calls = 0
        self.data = {"container": {"duration": 1.0}}
    def plan(self, ctx, kind, parameters):
        return [{"executable": "ffprobe", "purpose": "fake"}]
    def analyze(self, ctx, kind, parameters):
        self.calls += 1
        return dict(self.data)


class FakeRegistry:
    def __init__(self, analyzer):
        self.a = analyzer
    def for_kind(self, kind):
        if kind not in self.a.supported_kinds:
            raise AnalysisError("ANALYZER_UNAVAILABLE", f"no analyzer for {kind}")
        return self.a
    def all(self):
        return [self.a]


def fake_caps(*names):
    cs = CapabilitySet()
    for n in names:
        cs.items[n] = Capability(n, "AVAILABLE", version="6.1.1", path="/usr/bin/" + n)
    return cs


def make_engine(tmp_path, analyzer=None, cache=True, budget=None):
    analyzer = analyzer or CountingAnalyzer()
    policy = PathPolicy(workspace=str(tmp_path))
    c = ObservationCache(str(tmp_path / "cache"), policy) if cache else None
    eng = AnalysisEngine(caps=fake_caps("ffprobe", "ffmpeg"), registry=FakeRegistry(analyzer), policy=policy, cache=c, budget=budget, clock=lambda: "2026-09-04T00:00:00Z")
    return eng, analyzer


def req(path, **over):
    d = {"asset_id": "asset-1", "input": str(path), "kind": "media_probe"}
    d.update(over)
    return d


# ---- 17-21 cache hit / miss / invalidation
def test_cache_hit_and_miss(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"media")
    eng, an = make_engine(tmp_path)
    r1 = eng.analyze(req(f))
    assert r1["cache"]["status"] == "miss" and an.calls == 1 and eng.cache.stats()["misses"] == 1 and r1["usage"]["analyzer_calls"] == 1
    r2 = eng.analyze(req(f))
    assert r2["cache"]["status"] == "hit" and an.calls == 1 and r2["observation"] == r1["observation"] and r2["usage"]["analyzer_calls"] == 0
    r3 = eng.analyze(req(f, asset_id="asset-2"))          # same file under another label: reuse, relabel
    assert r3["cache"]["status"] == "hit" and r3["observation"]["asset_id"] == "asset-2" and an.calls == 1
    assert eng.analyze(req(f, cache_policy="bypass"))["cache"]["status"] == "bypass" and an.calls == 2
    assert eng.analyze(req(f, cache_policy="only"))["cache"]["status"] == "hit" and an.calls == 2
    eng2, an2 = make_engine(tmp_path, cache=False)
    assert eng2.analyze(req(f))["cache"]["status"] == "disabled" and an2.calls == 1
    with pytest.raises(AnalysisError) as e:
        eng2.analyze(req(f, cache_policy="only"))
    assert e.value.code == "CACHE_MISS" and an2.calls == 1
    with pytest.raises(AnalysisError):
        eng2.analyze(req(f, cache_policy="sometimes"))


def test_cache_invalidation_asset_version_parameters(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"media")
    eng, an = make_engine(tmp_path)
    eng.analyze(req(f))
    f.write_bytes(b"media changed")
    assert eng.analyze(req(f))["cache"]["status"] == "miss" and an.calls == 2
    an.version = "0.1.1"
    assert eng.analyze(req(f))["cache"]["status"] == "miss" and an.calls == 3
    assert eng.analyze(req(f))["cache"]["status"] == "hit" and an.calls == 3
    # parameter invalidation with a parameterised kind
    class Sil(CountingAnalyzer):
        id, supported_kinds, required_capabilities = "silence", ("silence",), ("ffprobe",)
    eng2, an2 = make_engine(tmp_path, analyzer=Sil())
    eng2.analyze(req(f, kind="silence", parameters={"threshold_db": -40}))
    assert eng2.analyze(req(f, kind="silence", parameters={"threshold_db": -40.0}))["cache"]["status"] == "hit"
    assert eng2.analyze(req(f, kind="silence", parameters={"threshold_db": -30}))["cache"]["status"] == "miss" and an2.calls == 2


def test_cache_corrupt_entry_is_a_miss(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"media")
    eng, an = make_engine(tmp_path)
    key = eng.analyze(req(f))["cache"]["key"]
    entry = tmp_path / "cache" / key[:2] / f"{key}.json"
    doc = json.loads(entry.read_text())
    doc["observation"]["data"]["container"]["duration"] = 999   # tampered: result hash no longer matches
    entry.write_text(json.dumps(doc))
    assert eng.analyze(req(f))["cache"]["status"] == "invalid" and an.calls == 2 and eng.cache.stats()["invalid"] == 1
    entry.write_text("{not json")
    assert eng.analyze(req(f))["cache"]["status"] == "invalid" and an.calls == 3
    with pytest.raises(AnalysisError) as e:
        eng.cache.get("not-hex", asset_fingerprint="", analyzer="", analyzer_version="", kind="", parameters={})
    assert e.value.code == "CACHE_INVALID"


# ---- 22-23 timeout / budget
def test_timeout_is_structured(tmp_path):
    import sys
    from media_analysis.runner import run_argv
    with pytest.raises(AnalysisError) as e:
        run_argv([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
    assert e.value.code == "ANALYZER_TIMEOUT"
    with pytest.raises(AnalysisError) as e:
        run_argv(["/nonexistent/ffprobe-xyz", "-version"], timeout=1)
    assert e.value.code == "ANALYZER_UNAVAILABLE"
    t = BudgetTracker(Budget(timeout=10, max_total_seconds=3))
    t.charge(2.5)
    assert t.effective_timeout(None) == 0.5 and t.effective_timeout(0.2) == 0.2


def test_budget_exceeded(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"media")
    eng, an = make_engine(tmp_path, budget=Budget(max_analysis_calls=1))
    eng.analyze(req(f))
    assert eng.analyze(req(f))["cache"]["status"] == "hit"             # cache hits are free
    f.write_bytes(b"other")
    with pytest.raises(AnalysisError) as e:
        eng.analyze(req(f))
    assert e.value.code == "BUDGET_EXCEEDED" and an.calls == 1 and e.value.details["calls"] == 1
    class Slow(CountingAnalyzer):
        def analyze(self, ctx, kind, parameters):
            import time
            time.sleep(0.05)          # measurable on every platform's monotonic clock (Windows: ~16 ms resolution)
            return super().analyze(ctx, kind, parameters)
    eng2, an2 = make_engine(tmp_path, analyzer=Slow(), cache=False, budget=Budget(max_total_seconds=0.01))
    eng2.analyze(req(f))
    with pytest.raises(AnalysisError) as e:
        eng2.analyze(req(f))
    assert e.value.code == "BUDGET_EXCEEDED" and an2.calls == 1 and e.value.details["seconds"] >= 0.01
    with pytest.raises(AnalysisError):
        Budget(max_analysis_calls=-1)


# ---- 24 unsupported analyzer
def test_unsupported_analyzer(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"media")
    eng, an = make_engine(tmp_path)
    eng.caps = fake_caps()   # nothing available
    with pytest.raises(AnalysisError) as e:
        eng.analyze(req(f))
    assert e.value.code == "ANALYZER_UNAVAILABLE" and e.value.details["missing"] == ["ffprobe"] and an.calls == 0
    rows = default_registry().availability(fake_caps("ffprobe"))
    by = {r["id"]: r for r in rows}
    assert by["probe"]["status"] == "available" and by["loudness"]["status"] == "unavailable" and "filter:ebur128" in by["loudness"]["missing_capabilities"]


# ---- 25 dry-run
def test_dry_run_runs_nothing(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"media")
    eng, an = make_engine(tmp_path)
    res = eng.plan(req(f))
    plan = res["plan"]
    assert res["status"] == "ok" and res["usage"]["analyzer_calls"] == 0 and "observation" not in res
    assert plan["dry_run"] is True and plan["executable"] is True and plan["operations"] == [{"executable": "ffprobe", "purpose": "fake"}]
    assert an.calls == 0 and eng.tracker.calls == 0 and not (tmp_path / "cache").exists()
    with pytest.raises(AnalysisError) as e:      # validation still happens
        eng.plan(req(tmp_path / "missing.mp4"))
    assert e.value.code == "FILE_NOT_FOUND"
    eng.caps = fake_caps()
    assert eng.plan(req(f))["plan"]["executable"] is False


# ---- 26 path security
def test_path_security(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    inside = root / "in.bin"
    inside.write_bytes(b"x")
    outside = tmp_path / "out.bin"
    outside.write_bytes(b"x")
    link = root / "link.bin"
    cases = [(str(outside), "PATH_NOT_ALLOWED"), (str(root), "FILE_NOT_FOUND"), (str(root / "nope"), "FILE_NOT_FOUND"),
             (str(root / ".." / "out.bin"), "PATH_NOT_ALLOWED"), ("", "INVALID_INPUT"), ("a\x00b", "INVALID_INPUT")]
    try:
        link.symlink_to(outside)
        cases.append((str(link), "PATH_NOT_ALLOWED"))     # a symlink inside the root pointing outside is refused
    except OSError:
        pass                                              # symlinks need privileges on Windows; the other cases still run
    pol = PathPolicy(workspace=str(root), allowed_input_roots=[str(root)])
    assert pol.resolve_input(str(inside)) == inside.resolve()
    for p, code in cases:
        with pytest.raises(AnalysisError) as e:
            pol.resolve_input(p)
        assert e.value.code == code, p
    assert pol.resolve_write_dir("cache") == root.resolve() / "cache"
    for w in ("../escape", str(tmp_path / "elsewhere"), "/tmp"):
        with pytest.raises(AnalysisError) as e:
            pol.resolve_write_dir(w)
        assert e.value.code == "PATH_NOT_ALLOWED"
    # argv construction never lets the path become an option or a protocol
    from media_analysis.runner import ffmpeg_null_argv, ffprobe_argv
    argv = ffprobe_argv("/usr/bin/ffprobe", "/tmp/-loglevel")
    assert argv[argv.index("-i") + 1] == "/tmp/-loglevel" and "-protocol_whitelist" in argv and argv[argv.index("-protocol_whitelist") + 1] == "file"
    assert ffmpeg_null_argv("/usr/bin/ffmpeg", "/x")[-3:] == ["-f", "null", "-"]


def test_no_shell_execution_in_source():
    src = Path(__file__).resolve().parent.parent / "src" / "media_analysis"
    # conformance.py's own denylist names these patterns as string literals to scan *other* files for them; it never
    # executes a subprocess itself, so its literals are not a hit (media_analysis.conformance.check_no_unsafe_shell_out
    # proves the same property the other way: it scans every *other* file and excludes only this one).
    text = "\n".join(p.read_text(encoding="utf-8") for p in src.rglob("*.py") if p.name != "conformance.py")
    for needle in ("os.system", "shell=True", "eval(", "exec(", "os.popen", "commands.getoutput"):
        assert needle not in text, needle


# ---- 27 secret leakage
def test_secret_leakage(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_TEST_API_KEY", "sk-super-secret-value-123456")
    f = tmp_path / "a.bin"
    f.write_bytes(b"media")
    an = CountingAnalyzer()
    an.data = {"container": {"note": "sk-super-secret-value-123456"}}
    eng, _ = make_engine(tmp_path, analyzer=an)
    with pytest.raises(AnalysisError) as e:
        eng.analyze(req(f))
    assert e.value.code == "VERIFICATION_FAILED" and "secret environment" in json.dumps(e.value.details)
    an.data = {"container": {"api_key": "x"}}
    with pytest.raises(AnalysisError) as e:
        eng.analyze(req(f))
    assert "secret-looking key" in json.dumps(e.value.details)
    from media_analysis.runner import _clean_env
    assert "MEDIA_TEST_API_KEY" not in _clean_env() and "PATH" in _clean_env()


# ---- 28 command / argv leakage
def test_command_argv_leakage(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"media")
    for key in ("command", "argv", "cmd", "shell", "commands"):
        an = CountingAnalyzer()
        an.data = {"container": {key: ["ffprobe", "-i", "x"]}}
        eng, _ = make_engine(tmp_path, analyzer=an)
        with pytest.raises(AnalysisError) as e:
            eng.analyze(req(f))
        assert e.value.code == "VERIFICATION_FAILED" and "forbidden key" in json.dumps(e.value.details)
    # a good observation carries operations (executable + purpose) but never argv
    an = CountingAnalyzer()
    eng, _ = make_engine(tmp_path, analyzer=an)
    obs = eng.analyze(req(f))["observation"]
    from media_analysis.verify import walk
    keys = {k for _, k, _ in walk(obs) if isinstance(k, str)}
    assert not keys & {"argv", "command", "commands", "cmd"} and "-print_format" not in json.dumps(obs)
    assert eng.executions[0]["operations"] == []


def test_error_model():
    assert len(set(EXIT_CODES.values())) == len(ERROR_CODES) and min(EXIT_CODES.values()) >= 2
    assert EXIT_CODES["INVALID_INPUT"] == 2 and EXIT_CODES["FILE_NOT_FOUND"] == 3 and EXIT_CODES["BUDGET_EXCEEDED"] == 10 and EXIT_CODES["CACHE_MISS"] == 13
    e = AnalysisError("FILE_NOT_FOUND", "x", {"path": "/p"})
    assert e.to_dict() == {"code": "FILE_NOT_FOUND", "message": "x", "details": {"path": "/p"}, "class": "FATAL"}
    from media_analysis.errors import ERROR_CLASSES, ERROR_CLASS_OF
    assert set(ERROR_CLASS_OF) == set(ERROR_CODES) and set(ERROR_CLASS_OF.values()) <= set(ERROR_CLASSES)
    assert ERROR_CLASS_OF["ANALYZER_TIMEOUT"] == "RETRYABLE" and ERROR_CLASS_OF["BUDGET_EXCEEDED"] == "BLOCKED" and ERROR_CLASS_OF["INVALID_INPUT"] == "FATAL"
    with pytest.raises(ValueError):
        AnalysisError("MADE_UP", "x")


def test_timestamp_report():
    pk = parse_packets("0|0|0|0.04|K__\n0|0.04|0.04|0.04|___\n0|0.08|0.04|0.04|___\n0|N/A|0.12|0.04|___\n1|0|0|0.02|K__")
    rep = timestamp_report([p for p in pk if p["stream_index"] == 0], 2.5)
    assert rep["packet_count"] == 4 and rep["missing_pts"] == 1 and rep["non_monotonic_dts"] == 0 and rep["keyframes"] == 1
    rep2 = timestamp_report(parse_packets("0|0|0.1|0.04|K__\n0|0.04|0.05|0.04|___"), 2.5)
    assert rep2["non_monotonic_dts"] == 1
