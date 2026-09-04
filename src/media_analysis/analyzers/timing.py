"""timing / duration: container and stream durations and start times, A/V duration mismatch, timestamp discontinuities."""
from __future__ import annotations

from typing import Any, Dict, List

from ..packets import by_stream, run_packets, timestamp_report
from ..probe import container_duration, streams_of
from .base import PROBE_OP, AnalysisContext, Analyzer


def _durations(p: Dict[str, Any]) -> Dict[str, Any]:
    streams = [{"index": s["index"], "type": s["type"], "start_time": s["start_time"], "duration": s["duration"]} for s in p["streams"]]
    known = [s["duration"] for s in streams if s["duration"] is not None]
    return {"container_duration": container_duration(p), "container_start_time": p["container"]["start_time"], "streams": streams,
            "longest_stream_duration": max(known) if known else None, "shortest_stream_duration": min(known) if known else None}


def av_mismatch(p: Dict[str, Any], tolerance: float) -> Dict[str, Any]:
    v, a = streams_of(p, "video"), streams_of(p, "audio")
    if not v or not a or v[0]["duration"] is None or a[0]["duration"] is None:
        return {"status": "not_performed", "reason": "needs a video and an audio stream with declared durations"}
    diff = round(v[0]["duration"] - a[0]["duration"], 6)
    return {"status": "performed", "video_index": v[0]["index"], "audio_index": a[0]["index"], "video_duration": v[0]["duration"],
            "audio_duration": a[0]["duration"], "difference": diff, "tolerance": tolerance, "within_tolerance": abs(diff) <= tolerance}


class TimingAnalyzer(Analyzer):
    id = "timing"
    supported_kinds = ("duration", "timing")
    required_capabilities = ("ffprobe",)

    def plan(self, ctx, kind, parameters):
        ops: List[Dict[str, Any]] = [PROBE_OP]
        if kind == "timing":
            ops.append({"executable": "ffprobe", "purpose": "packet timestamps of all streams"})
        return ops

    def analyze(self, ctx: AnalysisContext, kind: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        p = ctx.probe()
        data = _durations(p)
        if kind == "duration":
            return data
        ctx.record("ffprobe", "packet timestamps of all streams")
        pkts = run_packets(ctx.exe("ffprobe"), ctx.input_path, ctx.timeout)
        reports = {str(idx): timestamp_report(group, parameters["gap_factor"]) for idx, group in sorted(by_stream(pkts).items())}
        anomalies = []
        for idx, rep in reports.items():
            if rep["non_monotonic_dts"]:
                anomalies.append({"stream_index": int(idx), "type": "non_monotonic_dts", "count": rep["non_monotonic_dts"]})
            if rep["gap_count"]:
                anomalies.append({"stream_index": int(idx), "type": "timestamp_gap", "count": rep["gap_count"]})
            # negative pts are reported in packet_timestamps (AAC priming / edit lists make a leading negative pts
            # common and valid) and are not listed as anomalies
            if rep["duplicate_pts"]:
                anomalies.append({"stream_index": int(idx), "type": "duplicate_pts", "count": rep["duplicate_pts"]})
        data.update({"gap_factor": parameters["gap_factor"], "av_mismatch": av_mismatch(p, parameters["av_mismatch_tolerance"]),
                     "packet_timestamps": reports, "anomalies": anomalies, "anomaly_count": len(anomalies)})
        return data
