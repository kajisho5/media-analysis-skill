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
from media_analysis.analyzers.noise import parse_window_levels, percentile, summarise
from media_analysis.analyzers.timing import av_start_offset
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


# ---- noise_floor parser / summary (ADR-029)
def test_noise_window_parser_and_summary():
    text = ("[Parsed_ametadata_2 @ 0x1] frame:0    pts:0       pts_time:0\n"
            "[Parsed_ametadata_2 @ 0x1] lavfi.astats.Overall.RMS_level=-40.5\n"
            "[Parsed_ametadata_2 @ 0x1] lavfi.astats.Overall.RMS_level=-inf\n"
            "[Parsed_ametadata_2 @ 0x1] lavfi.astats.Overall.RMS_level=nan\n"
            "[Parsed_ametadata_2 @ 0x1] lavfi.astats.Overall.RMS_level=-9.0\n")
    levels = parse_window_levels(text)
    assert levels[0] == -40.5 and levels[1] == float("-inf") and levels[2] == -9.0 and len(levels) == 3
    assert percentile([1.0, 2.0, 3.0, 4.0], 10) == 1.0 and percentile([1.0, 2.0, 3.0, 4.0], 95) == 4.0 and percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0
    d = summarise([-60.0] * 5 + [-10.0] * 5, 10, 95)
    assert d["noise_floor_dbfs"] == -60.0 and d["signal_level_dbfs"] == -10.0 and d["snr_db"] == 50.0 and d["unmeasurable"] == []
    d = summarise([float("-inf")] * 2 + [-10.0] * 8, 10, 95)
    assert d["noise_floor_dbfs"] is None and d["snr_db"] is None and d["digital_silence_windows"] == 2
    assert d["unmeasurable"] == ["noise_floor_dbfs", "snr_db"] and d["min_window_rms_dbfs"] is None
    with pytest.raises(AnalysisError):
        summarise([], 10, 95)


def test_av_start_offset_from_probe():
    p = {"streams": [{"index": 0, "type": "video", "start_time": 0.0, "duration": 3.0, "disposition": []},
                     {"index": 1, "type": "audio", "start_time": 0.5, "duration": 3.0, "disposition": []}]}
    off = av_start_offset(p, 0.045)
    assert off["offset"] == 0.5 and off["within_tolerance"] is False and off["audio_index"] == 1
    p["streams"][1]["start_time"] = -0.02
    assert av_start_offset(p, 0.045)["within_tolerance"] is True
    p["streams"][1]["start_time"] = None
    assert av_start_offset(p, 0.045)["status"] == "not_performed"


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
    base = dict(identity="ab" * 32, asset_id="asset-1", kind="media_probe", tool="probe", analyzer_version=VERSION, algorithm_version="1", data={"container": {"duration": 6.0}},
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


def test_cache_invalidation_asset_algorithm_version_parameters(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"media")
    eng, an = make_engine(tmp_path)
    eng.analyze(req(f))
    f.write_bytes(b"media changed")
    assert eng.analyze(req(f))["cache"]["status"] == "miss" and an.calls == 2
    an.algorithm_version = "2"
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
        eng.cache.get("not-hex", asset_fingerprint="", analyzer="", algorithm_version="", kind="", parameters={})
    assert e.value.code == "CACHE_INVALID"


# ---- cache survives releases (ADR-028): the package version is provenance, the algorithm version is the identity
def test_package_patch_bump_keeps_cache(tmp_path, monkeypatch):
    import media_analysis.cache as cache_mod
    f = tmp_path / "a.bin"
    f.write_bytes(b"media")
    eng, an = make_engine(tmp_path)
    an.version = "0.1.3"
    monkeypatch.setattr(cache_mod, "VERSION", "0.1.3")
    r1 = eng.analyze(req(f))
    key = r1["cache"]["key"]
    entry = json.loads((tmp_path / "cache" / key[:2] / f"{key}.json").read_text())
    assert entry["metadata"]["package_version"] == "0.1.3" and entry["metadata"]["algorithm_version"] == an.algorithm_version
    # the release workflow bumps the patch version (docs-only release): same analyzer, same measurement
    an.version = "0.1.4"
    monkeypatch.setattr(cache_mod, "VERSION", "0.1.4")
    eng2, _ = make_engine(tmp_path, analyzer=an)
    r2 = eng2.analyze(req(f))
    assert r2["cache"] == {"status": "hit", "policy": "use", "key": key} and an.calls == 1 and r2["usage"]["analyzer_calls"] == 0
    # the cached Observation keeps the provenance of the package version that measured it
    assert r2["observation"]["source"] == "media-analysis/probe@0.1.3" and r2["observation"]["analysis"]["analyzer_version"] == "0.1.3"
    assert r2["observation"]["id"] == r1["observation"]["id"] and r2["observation"]["analysis"]["identity"] == key
    # a fresh measurement records the new package version
    r3 = eng2.analyze(req(f, cache_policy="bypass"))
    assert r3["observation"]["source"] == "media-analysis/probe@0.1.4" and r3["observation"]["analysis"]["algorithm_version"] == an.algorithm_version
    assert r3["observation"]["analysis"]["identity"] == key and an.calls == 2


def test_algorithm_version_bump_invalidates_only_that_analyzer(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"media")

    class Sil(CountingAnalyzer):
        id, supported_kinds, required_capabilities = "silence", ("silence",), ("ffprobe",)

    class Both:
        def __init__(self, *analyzers):
            self.by_kind = {k: a for a in analyzers for k in a.supported_kinds}
        def for_kind(self, kind):
            return self.by_kind[kind]
        def all(self):
            return list(self.by_kind.values())

    probe, sil = CountingAnalyzer(), Sil()
    policy = PathPolicy(workspace=str(tmp_path))

    def engine():
        return AnalysisEngine(caps=fake_caps("ffprobe"), registry=Both(probe, sil), policy=policy,
                              cache=ObservationCache(str(tmp_path / "cache"), policy), clock=lambda: "2026-09-04T00:00:00Z")
    eng = engine()
    p1, s1 = eng.analyze(req(f)), eng.analyze(req(f, kind="silence"))
    assert (p1["cache"]["status"], s1["cache"]["status"], probe.calls, sil.calls) == ("miss", "miss", 1, 1)
    sil.algorithm_version = str(int(sil.algorithm_version) + 1)     # silence's measurement changed, probe's did not
    eng = engine()
    p2, s2 = eng.analyze(req(f)), eng.analyze(req(f, kind="silence"))
    assert p2["cache"]["status"] == "hit" and probe.calls == 1 and p2["observation"]["id"] == p1["observation"]["id"]
    assert s2["cache"]["status"] == "miss" and sil.calls == 2 and s2["cache"]["key"] != s1["cache"]["key"]
    assert s2["observation"]["analysis"]["algorithm_version"] == sil.algorithm_version
    assert eng.analyze(req(f, kind="silence"))["cache"]["status"] == "hit" and sil.calls == 2


def test_every_analyzer_declares_its_algorithm_version():
    for a in default_registry().all():
        assert "algorithm_version" in type(a).__dict__, f"{type(a).__name__} must declare its own algorithm_version"
        assert a.algorithm_version.isdigit() and int(a.algorithm_version) >= 1
        assert a.version == VERSION                      # tool version stays the package version (ADR-002, provenance)
    spec = {t["tool_id"]: t for t in skill_contract()["tools"]}
    assert all(spec[f"media-analysis/{a.id}"]["algorithm_version"] == a.algorithm_version for a in default_registry().all())
    assert "analyzer_version" not in skill_contract()["identity"]["analysis_identity"]
    assert "algorithm_version" in skill_contract()["identity"]["analysis_identity"]


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


# ---- edit_style helpers (ADR-030)
def test_edit_style_shots_and_distribution():
    from media_analysis.analyzers.style import distribution, shots_from_cuts, video_profile
    shots = shots_from_cuts([12, 13, 37, 74, 124, 186, 0], 186, 25.0, min_shot_frames=2)
    assert [s["frames"] for s in shots] == [12, 25, 37, 50, 62]          # 13 is 1 frame after 12: dropped; 0 / 186 are not cuts
    assert shots[0]["start"] == 0.0 and shots[-1]["end"] == 7.44 and shots[1]["seconds"] == 1.0
    assert distribution([10, 10, 20, 30, 30, 40, 40])["median"] == 30.0 and distribution([1, 2, 3, 4])["median"] == 2.5
    assert distribution([1, 2, 3, 4])["p10"] == 1.0 and distribution([]) is None
    v = video_profile([12, 37, 74, 124], 186, 25.0, 0.25)
    assert v["cut_count"] == 4 and v["shot_count"] == 5 and v["shot_frames"]["median"] == 37.0 and v["min_shot_frames"] == 6
    assert sum(b["count"] for b in v["shot_length_histogram"]) == 5 and v["shot_length_histogram"][-1]["to_seconds"] is None


def test_edit_style_ass_positions():
    from media_analysis.analyzers.style import parse_ass_events, subtitle_stream_profile
    doc = "\n".join([
        "[Script Info]", "PlayResY: 288", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Arial,16,&Hffffff,&Hffffff,&H0,&H0,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1",
        "Style: Sign,Arial,16,&Hffffff,&Hffffff,&H0,&H0,0,0,0,0,100,100,0,0,1,1,0,8,10,10,10,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        "Dialogue: 0,0:00:00.50,0:00:01.50,Default,,0,0,0,,Hello, world.",
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\an8}Top",
        "Dialogue: 0,0:00:03.00,0:00:04.00,Sign,,0,0,0,,Style says top",
        "Dialogue: 0,0:00:05.00,0:00:06.00,Default,,0,0,0,,{\\pos(192,144)}Middle by position",
        "Dialogue: 0,0:00:07.00,0:00:07.00,Default,,0,0,0,,zero length is not an event",
    ])
    ev = parse_ass_events(doc)
    assert [e["position"] for e in ev] == ["bottom", "top", "top", "middle"] and ev[0]["start"] == 0.5 and ev[0]["end"] == 1.5
    prof = subtitle_stream_profile(ev, 60.0)
    assert prof["events"] == 4 and prof["events_per_minute"] == 4.0 and prof["positions"] == {"bottom": 1, "middle": 1, "top": 2}
    assert prof["dominant_position"] == "top" and abs(prof["coverage_share"] - 3.5 / 60) < 1e-6    # 0.5-2.0 overlaps -> 1.5 s


def test_edit_style_diff():
    from media_analysis.analyzers.style import PROFILE_SCHEMA, diff_profiles
    ref = {"profile_schema": PROFILE_SCHEMA, "video": {"fps": 25.0, "cut_count": 4, "shot_frames": {"median": 37.0}},
           "subtitles": {"present": False}, "audio": {"integrated_lufs": -23.0}}
    cand = {"profile_schema": PROFILE_SCHEMA, "video": {"fps": 25.0, "cut_count": 6, "shot_frames": {"median": 30.0}},
            "subtitles": {"present": True}, "audio": {"integrated_lufs": None}}
    d = diff_profiles({"observations": [{"kind": "edit_style", "data": ref}]}, {"data": cand})
    items = {i["metric"]: i for i in d["items"]}
    assert items["video.shot_frames.median"]["difference"] == -7.0 and items["video.cut_count"]["difference"] == 2
    assert items["audio.integrated_lufs"]["status"] == "unavailable" and items["audio.integrated_lufs"]["missing_in"] == ["candidate"]
    assert items["video.fps"]["equal"] is True and items["subtitles.present"]["equal"] is False
    assert set(d["different"]) == {"video.shot_frames.median", "video.cut_count", "subtitles.present"}
    with pytest.raises(AnalysisError) as ei:
        diff_profiles({"video": {}}, cand)
    assert ei.value.code == "INVALID_INPUT"


def test_edit_style_subtitles_present_over_every_stream():
    """An empty first text track (e.g. a forced track with no events) must not hide dialogue on a later track."""
    from media_analysis.analyzers.style import subtitle_stream_profile, subtitle_summary
    empty = {"stream_index": 2, "status": "measured", **subtitle_stream_profile([], 60.0)}
    dialogue = {"stream_index": 3, "status": "measured", **subtitle_stream_profile([{"start": 1.0, "end": 2.0, "position": "bottom"}], 60.0)}
    bitmap = {"stream_index": 4, "status": "not_performed", "reason": "bitmap"}
    s = subtitle_summary(3, [empty, dialogue, bitmap])
    assert s["present"] is True and s["summary_stream_index"] == 2 and s["events_per_minute"] == 0.0   # summary: first measured stream
    assert subtitle_summary(1, [empty])["present"] is False and subtitle_summary(1, [bitmap])["present"] is False
    assert subtitle_summary(0, [])["summary_stream_index"] is None


# ---- library helpers (ADR-031)
def test_library_describe_and_search():
    from media_analysis.library import describe, name_words, parse_external_tags, search
    assert name_words("Trips/Beach_Sunset-02.MP4") == ["trips", "beach", "sunset", "02"]
    probe = {"container": {"duration": 12.0, "format": "mov,mp4", "size": 10},
             "video": {"codec": "h264", "width": 1920, "height": 1080, "fps": 30000 / 1001, "rotation": 90}, "audio": None}
    scenes = {"scene_count": 2, "cut_count": 1, "scenes": [{"index": 0, "start": 0.0, "end": 4.0, "duration": 4.0},
                                                         {"index": 1, "start": 4.0, "end": 12.0, "duration": 8.0}]}
    c = describe(probe, scenes, None)
    assert c["video"]["orientation"] == "portrait" and c["video"]["resolution_class"] == "fhd" and c["video"]["fps_class"] == "29.97fps"
    assert c["representative_time"] == 8.0 and c["scene_type"] == "multi_shot" and c["audio"] is None
    assert set(c["measured_tags"]) == {"portrait", "fhd", "29.97fps", "h264", "multi-shot", "no-audio", "medium"}
    ext = parse_external_tags([{"path": "a\\b.mp4", "tags": ["Ocean ", "ocean"], "source": "vision-model"}])
    assert ext == {"a/b.mp4": {"tags": ["ocean"], "source": "vision-model"}}
    for bad in ({"a.mp4": "ocean"}, [{"path": "a.mp4", "tags": [""]}], "x"):
        with pytest.raises(AnalysisError):
            parse_external_tags(bad)
    index = {"root": "/lib", "clips": [
        {"path": "beach.mp4", "duration": 6.0, "video": {"orientation": "landscape", "height": 1080}, "audio": {}, "scene_type": "single_shot",
         "tags": ["landscape", "ocean"], "name_words": ["beach"]},
        {"path": "phone.mp4", "duration": 2.0, "video": {"orientation": "portrait", "height": 320}, "audio": None, "scene_type": "single_shot",
         "tags": ["portrait"], "name_words": ["phone"]}]}
    assert [h["path"] for h in search(index, ["Ocean"])["results"]] == ["beach.mp4"]
    assert [h["path"] for h in search(index, ["bea"])["results"]] == ["beach.mp4"]          # prefix of a path word
    assert [h["path"] for h in search(index, has_audio=False)["results"]] == ["phone.mp4"]
    assert [h["path"] for h in search(index, min_height=720)["results"]] == ["beach.mp4"]
    assert search(index, ["ocean", "phone"])["result_count"] == 0                             # every word must match
    assert [h["path"] for h in search(index, max_duration=3)["results"]] == ["phone.mp4"]


# ---- highlight candidates (ADR-032)
def test_highlight_sentences_from_segments_and_words():
    from media_analysis.highlights import sentences_from_transcript
    seg = sentences_from_transcript({"segments": [
        {"start": 0.0, "end": 2.0, "text": "Hello and", "speaker_id": "A"},
        {"start": 2.0, "end": 4.0, "text": "welcome. Today", "speaker_id": "A"},     # ends mid-sentence: joins the next
        {"start": 4.0, "end": 6.0, "text": "we start.", "speaker_id": "A"},
        {"start": 6.0, "end": 8.0, "text": "Question from the floor", "speaker_id": "B"},   # speaker change ends a sentence
        {"start": 8.0, "end": 9.0, "text": "「本当ですか？」", "speaker_id": "A"}]})
    assert [(s["start"], s["end"]) for s in seg] == [(0.0, 6.0), (6.0, 8.0), (8.0, 9.0)] and seg[0]["basis"] == "segments"
    assert [s["speaker"] for s in seg] == ["A", "B", "A"]
    words = sentences_from_transcript({"segments": [{"start": 0.0, "end": 3.0, "text": "Hi there. Bye.", "words": [
        {"start": 0.1, "end": 0.5, "text": "Hi"}, {"start": 0.6, "end": 1.0, "text": "there."},
        {"start": 2.0, "end": 2.6, "word": "Bye."}]}]})
    assert [(s["start"], s["end"], s["text"]) for s in words] == [(0.1, 1.0, "Hi there."), (2.0, 2.6, "Bye.")] and words[0]["basis"] == "words"
    for bad in ({}, {"segments": []}, {"segments": [{"start": 1, "end": 0, "text": "x"}]}, {"segments": [{"start": "a", "end": 1, "text": "x"}]}):
        with pytest.raises(AnalysisError):
            sentences_from_transcript(bad)


def test_highlight_scoring_rules():
    from media_analysis.highlights import candidates, parse_signals
    sents = [{"index": i, "start": i * 5.0, "end": i * 5.0 + 4.5, "text": ("Big news!" if i in (4, 5) else "Plain words."),
              "speaker": "A" if i < 8 else "B", "basis": "segments"} for i in range(10)]
    levels = [-30.0] * 100
    for k in range(40, 60):            # 20..30 s, 0.5 s windows: +12 dB
        levels[k] = -18.0
    sig = parse_signals({"applause": [{"start": 30.5, "end": 33.0}]})
    out = candidates(sents, levels=levels, window=0.5, signals=sig, keywords=["news"], min_length=8, max_length=20, reaction_window=3, top=3)
    best = out[0]
    assert (best["start"], best["end"]) == (20.0, 29.5) and best["rank"] == 1 and best["components"]["applause"] == 1.0
    assert best["components"]["keywords"] == 1.0 and abs(best["components"]["energy_lift_db"] - 12.0) < 0.6
    assert any("applause at 30.5-33.0 s (after the span)" == r for r in best["reasons"])
    starts = {s["start"] for s in sents}
    ends = {s["end"] for s in sents}
    assert all(c["start"] in starts and c["end"] in ends for c in out)                 # sentence boundaries only
    assert all(not (a["start"] < b["end"] and b["start"] < a["end"]) for a in out for b in out if a is not b)   # no overlap
    assert [c["score"] for c in out] == sorted((c["score"] for c in out), reverse=True)
    for bad in ([], {"applause": [{"start": 2, "end": 1}]}, {"cheering": []}):
        with pytest.raises(AnalysisError):
            parse_signals(bad)


# ---- burned-in subtitles (ADR-033)
def _synthetic_frame(w, h, y0, y1, x0, n, bg=128, ink=255, band=None):
    luma = bytearray([bg] * (w * h))
    edges = bytearray(w * h)
    if band is not None:
        by0, by1, bx0, bx1, level = band
        for y in range(by0, by1):
            luma[y * w + bx0:y * w + bx1] = bytes([level]) * (bx1 - bx0)
    for y in range(y0, y1):
        for i in range(n):
            x = x0 + i * 5
            luma[y * w + x:y * w + x + 2] = bytes([ink, ink])
            edges[y * w + x] = 255
            edges[y * w + x + 2] = 255
    return bytes(luma), bytes(edges)


def test_burned_in_regions_and_summary():
    from media_analysis.analyzers.burnin import analysis_size, frame_regions, summarise
    assert analysis_size(1920, 1080) == (480, 270) and analysis_size(320, 180) == (320, 180)
    w, h = 200, 100
    luma, edges = _synthetic_frame(w, h, 80, 88, 60, 16)                 # 8 rows of 100, bottom, centred
    regs = frame_regions(luma, edges, w, h)
    assert len(regs) == 1 and regs[0]["vertical"] == "bottom" and regs[0]["horizontal"] == "center"
    assert regs[0]["height_ratio"] == 0.08 and regs[0]["height_basis"] == "pixels" and regs[0]["band"] is False
    banded = frame_regions(*_synthetic_frame(w, h, 80, 88, 60, 16, band=(74, 94, 50, 150, 0)), w, h)
    assert banded[0]["band"] is True
    top = frame_regions(*_synthetic_frame(w, h, 10, 18, 60, 16), w, h)
    assert top[0]["vertical"] == "top"
    empty = frame_regions(bytes([128] * (w * h)), bytes(w * h), w, h)
    assert empty == []
    s = summarise([regs, regs, [], top, top, []], 0.5, 3.0)
    assert s["coverage_share"] == round(4 / 6, 6) and s["switches"] == 2 and s["switches_per_minute"] == 40.0
    assert s["positions"]["vertical"] == {"top": 2, "middle": 0, "bottom": 2} and s["dominant_position"] == "bottom"
    none = summarise([[], []], 1.0, 2.0)
    assert none["present"] is False and none["text_height_ratio"] is None and none["band_share"] is None


def test_stream_records_streams_and_times_out():
    """stream_records hands over fixed-size records as they arrive (a trailing partial record is dropped), returns
    stderr, and kills the child at the deadline like run_argv."""
    import sys
    from media_analysis.runner import stream_records
    got = []
    script = "import sys; sys.stdout.buffer.write(b'ab' * 5 + b'z'); sys.stderr.write('done')"
    r = stream_records([sys.executable, "-c", script], 30, 4, got.append)
    assert r.returncode == 0 and got == [b"abab", b"abab"] and r.stderr == "done"
    with pytest.raises(AnalysisError) as ei:
        stream_records([sys.executable, "-c", "import time; time.sleep(30)"], 0.5, 4, got.append)
    assert ei.value.code == "ANALYZER_TIMEOUT"
    with pytest.raises(AnalysisError) as ei:
        stream_records(["x\x00y"], 1, 4, got.append)
    assert ei.value.code == "INVALID_INPUT"


def test_edit_style_selects_video_streams_without_cover_art(monkeypatch):
    """streams_of() skips attached pictures (cover art), so ffmpeg must be told the same numbering: 0:V:n, not 0:v:n
    (0:v counts cover art and would point at the wrong stream when the cover comes first)."""
    from media_analysis.analyzers import burnin, style
    from media_analysis.runner import RunResult
    seen = []

    def fake_stream(argv, timeout, size, on_record):
        seen.append(argv)
        return RunResult(0, "", "", 0.0)

    monkeypatch.setattr(burnin, "stream_records", fake_stream)
    out = burnin.measure("ffmpeg", "/x.mp4", 2, 640, 360, 1.0, 10.0, 5)
    assert out["status"] == "not_performed" and out["frames_sampled"] == 0
    graph = seen[0][seen[0].index("-filter_complex") + 1]
    assert graph.startswith("[0:V:2]") and "0:v:" not in graph

    def fake_run(argv, timeout):
        seen.append(argv)
        return RunResult(0, "", "", 0.0)

    monkeypatch.setattr(style, "run_argv", fake_run)
    probe = {"container": {"duration": 4.0}, "streams": [
        {"index": 0, "type": "video", "disposition": ["attached_pic"], "duration": None, "nb_frames": 1, "video": {"fps": 25.0, "width": 64, "height": 64}},
        {"index": 1, "type": "video", "disposition": [], "duration": 4.0, "nb_frames": 100, "video": {"fps": 25.0, "width": 320, "height": 180}}]}
    ctx = AnalysisContext("/x.mp4", fake_caps(), 5)
    ctx.exe = lambda name: name
    v = style.StyleAnalyzer()._video(ctx, probe, {"video_stream": 0, "threshold": 10.0, "min_shot_duration": 0.25})
    assert v["stream_index"] == 1 and seen[-1][seen[-1].index("-map") + 1] == "0:V:0"


def test_scene_detection_selects_video_stream_without_cover_art(monkeypatch):
    """scene_detection numbers video streams like select_stream() (attached pictures skipped), so the ffmpeg map must
    be 0:V:n: with cover art before the real video, 0:v:0 would run scdet on the single cover frame."""
    from media_analysis.analyzers import scenes
    from media_analysis.runner import RunResult
    seen = []

    def fake_run(argv, timeout):
        seen.append(argv)
        return RunResult(0, "", "", 0.0)

    monkeypatch.setattr(scenes, "run_argv", fake_run)
    probe = {"container": {"duration": 4.0}, "streams": [
        {"index": 0, "type": "video", "disposition": ["attached_pic"], "duration": None, "nb_frames": 1, "video": {"fps": 25.0, "width": 64, "height": 64}},
        {"index": 1, "type": "video", "disposition": [], "duration": 4.0, "nb_frames": 100, "video": {"fps": 25.0, "width": 320, "height": 180}},
        {"index": 2, "type": "video", "disposition": [], "duration": 3.0, "nb_frames": 75, "video": {"fps": 25.0, "width": 160, "height": 90}}]}
    ctx = AnalysisContext("/x.mp4", fake_caps(), 5)
    ctx.exe = lambda name: name
    ctx._probe = probe
    for ordinal, index, duration in ((0, 1, 4.0), (1, 2, 3.0)):
        out = scenes.SceneAnalyzer().analyze(ctx, "scene_detection", {"stream": ordinal, "threshold": 10.0, "min_scene_duration": 0.5})
        mapped = seen[-1][seen[-1].index("-map") + 1]
        assert mapped == f"0:V:{ordinal}" and out["stream_index"] == index and out["duration"] == duration
    assert not any(a.startswith("0:v:") for argv in seen for a in argv)


# ---- speech_regions segmentation / summary / UNKNOWN (ADR-035); the model itself runs in test_integration
def test_speech_segment_hysteresis_min_durations_and_pad():
    from media_analysis.analyzers.vad import FRAME_SECONDS, segment
    f = FRAME_SECONDS
    # 10 windows speech, 2 windows dip to 0.4 (above the 0.35 release threshold: still speech), 10 windows speech,
    # 5 windows silence (0.16 s >= 0.1 s: the region ends), 3 windows speech (0.096 s < 0.25 s: dropped), 5 silence
    probs = [0.9] * 10 + [0.4] * 2 + [0.8] * 10 + [0.1] * 5 + [0.9] * 3 + [0.1] * 5
    regs = segment(probs, threshold=0.5, min_speech=0.25, min_silence=0.1, pad=0.0)
    assert len(regs) == 1 and regs[0]["start"] == 0.0 and regs[0]["end"] == round(22 * f, 4)
    assert regs[0]["max_probability"] == 0.9 and regs[0]["probability"] == round((0.9 * 10 + 0.4 * 2 + 0.8 * 10) / 22, 4)
    # a short low run (2 windows = 0.064 s < 0.1 s) does not split; a long one does
    split = segment([0.9] * 10 + [0.1] * 2 + [0.9] * 10 + [0.1] * 4 + [0.9] * 10, 0.5, 0.25, 0.1, 0.0)
    assert [(r["start"], r["end"]) for r in split] == [(0.0, round(22 * f, 4)), (round(26 * f, 4), round(36 * f, 4))]
    # padding is clamped to the analysed length and padded regions that meet are merged
    padded = segment([0.9] * 10 + [0.1] * 4 + [0.9] * 10, 0.5, 0.25, 0.1, pad=0.1)
    assert len(padded) == 1 and padded[0]["start"] == 0.0 and padded[0]["end"] == round(24 * f, 4)
    assert segment([0.1] * 50, 0.5, 0.25, 0.1, 0.03) == [] and segment([], 0.5, 0.25, 0.1, 0.03) == []


def test_speech_summary_share_and_longest_pause():
    from media_analysis.analyzers.vad import summarise as speech_summary
    regs = [{"start": 1.0, "end": 2.0, "duration": 1.0}, {"start": 2.5, "end": 4.0, "duration": 1.5}, {"start": 6.0, "end": 7.0, "duration": 1.0}]
    d = speech_summary(regs, 10.0)
    assert d["speech_seconds"] == 3.5 and d["speech_share"] == 0.35 and d["region_count"] == 3 and d["pause_count"] == 2
    assert d["longest_pause"] == {"start": 4.0, "end": 6.0, "duration": 2.0}
    assert d["leading_non_speech"] == 1.0 and d["trailing_non_speech"] == 3.0
    none = speech_summary([], 5.0)
    assert none["speech_share"] == 0.0 and none["longest_pause"] is None and none["leading_non_speech"] is None
    assert speech_summary(regs[:1], 10.0)["longest_pause"] is None


def test_speech_regions_unknown_without_the_extra_is_not_cached(tmp_path, monkeypatch):
    """Without onnxruntime / numpy the kind is UNKNOWN: no regions, no guess from level, and the result is not cached
    (installing the extra must measure, not replay the UNKNOWN)."""
    from media_analysis.analyzers import vad
    probe = {"container": {"duration": 3.0}, "streams": []}
    stream = {"index": 0, "type": "audio", "audio": {"channels": 1, "sample_rate": 16000}}
    monkeypatch.setattr(vad, "vad_status", lambda: {"available": False, "missing": ["onnxruntime"], "runtime": None, "model": vad.MODEL_FILE,
                                                    "detail": "optional extra 'vad' not installed (onnxruntime missing): " + vad.INSTALL_HINT})
    monkeypatch.setattr(vad, "select_stream", lambda p, t, n: stream)

    class Ctx:
        operations = []

        def probe(self):
            return probe

        def exe(self, name):
            raise AssertionError("no executable may run for an UNKNOWN result")

    a = vad.VadAnalyzer()
    params = {"stream": 0, "channel": -1, "threshold": 0.5, "min_speech_duration": 0.25, "min_silence_duration": 0.1, "speech_pad": 0.03}
    d = a.analyze(Ctx(), "speech_regions", params)
    assert d["status"] == "UNKNOWN" and d["regions"] is None and d["speech_share"] is None and d["longest_pause"] is None
    assert d["missing"] == ["onnxruntime"] and "[vad]" in d["install"] and d["engine"] is None
    assert a.cacheable(d) is False and a.cacheable({"status": "MEASURED"}) is True
    assert [op["executable"] for op in a.plan(Ctx(), "speech_regions", params)] == ["ffprobe"]


def test_speech_regions_contract_entries():
    from media_analysis.contract import CAPABILITY_IDS, KIND_TO_TOOL, PARAMETER_SCHEMAS, TOOL_CAPABILITIES, skill_contract
    assert KIND_TO_TOOL["speech_regions"] == "vad" and CAPABILITY_IDS["speech_regions"] == "measure.audio.speech_regions"
    assert TOOL_CAPABILITIES["vad"] == ["ffprobe", "ffmpeg"]
    assert PARAMETER_SCHEMAS["speech_regions"]["threshold"]["default"] == 0.5
    extra = skill_contract()["execution"]["optional_extras"]["vad"]
    assert extra["kinds"] == ["speech_regions"] and extra["download_at_run_time"] is False
