"""ffprobe-backed analyzers: media_probe, stream_layout, video_format, audio_format."""
from __future__ import annotations

from typing import Any, Dict, List

from ..packets import frame_rate_mode, run_packets
from ..probe import container_duration, select_stream, streams_of
from .base import PROBE_OP, AnalysisContext, Analyzer


def _video_summary(s: Dict[str, Any]) -> Dict[str, Any]:
    v = s["video"]
    return {"index": s["index"], "codec": s["codec"], "profile": s["profile"], "width": v["width"], "height": v["height"], "fps": v["fps"],
            "pixel_format": v["pixel_format"], "color_space": v["color_space"], "color_transfer": v["color_transfer"],
            "color_primaries": v["color_primaries"], "color_range": v["color_range"], "bitrate": s["bitrate"], "rotation": v["rotation"]}


def _audio_summary(s: Dict[str, Any]) -> Dict[str, Any]:
    a = s["audio"]
    return {"index": s["index"], "codec": s["codec"], "profile": s["profile"], "sample_rate": a["sample_rate"], "channels": a["channels"],
            "channel_layout": a["channel_layout"], "sample_format": a["sample_format"], "bitrate": s["bitrate"]}


class ProbeAnalyzer(Analyzer):
    id = "probe"
    supported_kinds = ("media_probe",)
    required_capabilities = ("ffprobe",)

    def plan(self, ctx, kind, parameters):
        return [PROBE_OP]

    def analyze(self, ctx: AnalysisContext, kind: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        p = ctx.probe()
        vids, auds = streams_of(p, "video"), streams_of(p, "audio")
        c = dict(p["container"])
        c["duration"] = container_duration(p)
        return {
            "container": c,
            "video": _video_summary(vids[0]) if vids else None,
            "audio": _audio_summary(auds[0]) if auds else None,
            "stream_counts": {t: sum(1 for s in p["streams"] if s["type"] == t) for t in sorted({s["type"] for s in p["streams"]})},
            "video_stream_count": len(vids),
            "audio_stream_count": len(auds),
        }


class StreamAnalyzer(Analyzer):
    id = "streams"
    supported_kinds = ("stream_layout",)
    required_capabilities = ("ffprobe",)

    def plan(self, ctx, kind, parameters):
        return [PROBE_OP]

    def analyze(self, ctx: AnalysisContext, kind: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        p = ctx.probe()
        ordinals: Dict[str, int] = {}
        streams: List[Dict[str, Any]] = []
        for s in p["streams"]:
            t = s["type"]
            entry = {"index": s["index"], "type": t, "type_ordinal": ordinals.get(t, 0), "codec": s["codec"], "profile": s["profile"],
                     "language": s["language"], "title": s["title"], "disposition": s["disposition"], "start_time": s["start_time"],
                     "duration": s["duration"], "bitrate": s["bitrate"], "nb_frames": s["nb_frames"]}
            ordinals[t] = ordinals.get(t, 0) + 1
            if t == "video":
                v = s["video"]
                entry.update({"width": v["width"], "height": v["height"], "fps": v["fps"], "pixel_format": v["pixel_format"],
                              "attached_pic": "attached_pic" in s["disposition"]})
            elif t == "audio":
                a = s["audio"]
                entry.update({"sample_rate": a["sample_rate"], "channels": a["channels"], "channel_layout": a["channel_layout"], "sample_format": a["sample_format"]})
            streams.append(entry)
        return {"stream_count": len(streams), "streams": streams, "counts": dict(sorted(ordinals.items()))}


class VideoAnalyzer(Analyzer):
    id = "video"
    supported_kinds = ("video_format",)
    required_capabilities = ("ffprobe",)

    def plan(self, ctx, kind, parameters):
        return [PROBE_OP, {"executable": "ffprobe", "purpose": f"packet timestamps of video stream {parameters['stream']} (frame count, CFR/VFR)"}]

    def analyze(self, ctx: AnalysisContext, kind: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        p = ctx.probe()
        s = select_stream(p, "video", parameters["stream"])
        v = s["video"]
        ctx.record("ffprobe", f"packet timestamps of video stream {parameters['stream']}")
        pkts = run_packets(ctx.exe("ffprobe"), ctx.input_path, ctx.timeout, f"v:{parameters['stream']}")
        mode = frame_rate_mode(pkts)
        frame_count, basis = (s["nb_frames"], "nb_frames") if s["nb_frames"] else ((len(pkts), "packet_count") if pkts else (None, None))
        return {
            "stream_index": s["index"], "stream_ordinal": parameters["stream"],
            "codec": s["codec"], "profile": s["profile"],
            "width": v["width"], "height": v["height"], "coded_width": v["coded_width"], "coded_height": v["coded_height"],
            "sample_aspect_ratio": v["sample_aspect_ratio"], "display_aspect_ratio": v["display_aspect_ratio"],
            "fps": v["fps"], "r_frame_rate": v["r_frame_rate"], "avg_frame_rate": v["avg_frame_rate"],
            "frame_rate_mode": mode["mode"], "frame_rate_mode_basis": mode["basis"], "measured_fps": mode.get("measured_fps"),
            "frame_count": frame_count, "frame_count_basis": basis,
            "duration": s["duration"] if s["duration"] is not None else container_duration(p),
            "start_time": s["start_time"],
            "pixel_format": v["pixel_format"], "bits_per_raw_sample": v["bits_per_raw_sample"],
            "color_space": v["color_space"], "color_transfer": v["color_transfer"], "color_primaries": v["color_primaries"], "color_range": v["color_range"],
            "field_order": v["field_order"], "rotation": v["rotation"], "bitrate": s["bitrate"],
        }


class AudioAnalyzer(Analyzer):
    id = "audio"
    supported_kinds = ("audio_format",)
    required_capabilities = ("ffprobe",)

    def plan(self, ctx, kind, parameters):
        return [PROBE_OP]

    def analyze(self, ctx: AnalysisContext, kind: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        p = ctx.probe()
        s = select_stream(p, "audio", parameters["stream"])
        a = s["audio"]
        return {
            "stream_index": s["index"], "stream_ordinal": parameters["stream"],
            "codec": s["codec"], "profile": s["profile"],
            "sample_rate": a["sample_rate"], "channels": a["channels"], "channel_layout": a["channel_layout"],
            "sample_format": a["sample_format"], "bits_per_sample": a["bits_per_sample"], "bitrate": s["bitrate"],
            "duration": s["duration"] if s["duration"] is not None else container_duration(p),
            "start_time": s["start_time"], "language": s["language"], "nb_frames": s["nb_frames"],
        }
