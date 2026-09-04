"""ffprobe execution and parsing. `parse_ffprobe` is a pure function over the ffprobe JSON document so it can be
unit-tested without ffprobe; `run_ffprobe` executes it. The structured result (not the raw document) is the
contract exposed by the probe / streams / video / audio / timing analyzers."""
from __future__ import annotations

import json
from fractions import Fraction
from typing import Any, Dict, List, Optional

from .errors import AnalysisError
from .runner import ffprobe_argv, run_argv

PROBE_ENTRIES = ("format=format_name,format_long_name,duration,size,bit_rate,start_time,nb_streams,probe_score:"
                 "stream=index,codec_type,codec_name,codec_long_name,profile,width,height,coded_width,coded_height,"
                 "sample_aspect_ratio,display_aspect_ratio,pix_fmt,color_space,color_transfer,color_primaries,color_range,"
                 "field_order,r_frame_rate,avg_frame_rate,time_base,start_time,duration,bit_rate,nb_frames,"
                 "sample_rate,sample_fmt,channels,channel_layout,bits_per_sample,bits_per_raw_sample:"
                 "stream_tags=language,title,rotate:stream_disposition:stream_side_data")


def run_ffprobe(exe: str, path: str, timeout: Optional[float]) -> Dict[str, Any]:
    r = run_argv(ffprobe_argv(exe, path, "-print_format", "json", "-show_entries", PROBE_ENTRIES), timeout)
    if r.returncode != 0:
        raise AnalysisError("UNSUPPORTED_FORMAT", "ffprobe could not open the input as media", {"stderr_tail": _tail(r.stderr)})
    try:
        raw = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        raise AnalysisError("ANALYSIS_FAILED", "ffprobe returned invalid JSON")
    if not isinstance(raw, dict) or not raw.get("format"):
        raise AnalysisError("UNSUPPORTED_FORMAT", "ffprobe returned no format information")
    return raw


def _tail(text: str, n: int = 8) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


# ---- parsing helpers
def to_float(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def to_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def fraction(v: Any) -> Optional[Fraction]:
    if not v or not isinstance(v, str):
        return None
    try:
        f = Fraction(v)
    except (ValueError, ZeroDivisionError):
        return None
    return f if f > 0 else None


def fraction_float(v: Any) -> Optional[float]:
    f = fraction(v)
    return round(float(f), 6) if f else None


def _ratio(v: Any) -> Optional[str]:
    """ffprobe writes '16:9' / 'N/A' / '0:1'."""
    if not isinstance(v, str) or ":" not in v:
        return None
    a, b = v.split(":", 1)
    if to_int(a) in (None, 0) or to_int(b) in (None, 0):
        return None
    return v


def _rotation(s: Dict[str, Any]) -> Optional[int]:
    for sd in s.get("side_data_list") or []:
        if isinstance(sd, dict) and "rotation" in sd:
            r = to_float(sd["rotation"])
            if r is not None:
                return int(round(r))
    tags = s.get("tags") or {}
    if "rotate" in tags:
        r = to_int(tags["rotate"])
        if r is not None:
            return r
    return None


def parse_stream(s: Dict[str, Any]) -> Dict[str, Any]:
    ctype = s.get("codec_type") or "unknown"
    disp = {k: bool(v) for k, v in (s.get("disposition") or {}).items() if v}
    tags = s.get("tags") or {}
    out: Dict[str, Any] = {
        "index": to_int(s.get("index")),
        "type": ctype,
        "codec": s.get("codec_name"),
        "codec_long_name": s.get("codec_long_name"),
        "profile": s.get("profile"),
        "language": tags.get("language"),
        "title": tags.get("title"),
        "disposition": sorted(disp),
        "start_time": to_float(s.get("start_time")),
        "duration": to_float(s.get("duration")),
        "bitrate": to_int(s.get("bit_rate")),
        "time_base": s.get("time_base"),
        "nb_frames": to_int(s.get("nb_frames")),
    }
    if ctype == "video":
        out["video"] = {
            "width": to_int(s.get("width")), "height": to_int(s.get("height")),
            "coded_width": to_int(s.get("coded_width")), "coded_height": to_int(s.get("coded_height")),
            "sample_aspect_ratio": _ratio(s.get("sample_aspect_ratio")), "display_aspect_ratio": _ratio(s.get("display_aspect_ratio")),
            "pixel_format": s.get("pix_fmt"),
            "color_space": s.get("color_space"), "color_transfer": s.get("color_transfer"),
            "color_primaries": s.get("color_primaries"), "color_range": s.get("color_range"),
            "field_order": s.get("field_order"),
            "r_frame_rate": s.get("r_frame_rate"), "avg_frame_rate": s.get("avg_frame_rate"),
            "fps": fraction_float(s.get("avg_frame_rate")) or fraction_float(s.get("r_frame_rate")),
            "rotation": _rotation(s),
            "bits_per_raw_sample": to_int(s.get("bits_per_raw_sample")),
        }
    elif ctype == "audio":
        out["audio"] = {
            "sample_rate": to_int(s.get("sample_rate")), "channels": to_int(s.get("channels")),
            "channel_layout": s.get("channel_layout"), "sample_format": s.get("sample_fmt"),
            "bits_per_sample": to_int(s.get("bits_per_sample")) or None,
        }
    return out


def parse_ffprobe(raw: Dict[str, Any]) -> Dict[str, Any]:
    fmt = raw.get("format") or {}
    streams = [parse_stream(s) for s in raw.get("streams") or []]
    streams.sort(key=lambda s: (s["index"] is None, s["index"] or 0))
    return {
        "container": {
            "format": fmt.get("format_name"), "format_long_name": fmt.get("format_long_name"),
            "duration": to_float(fmt.get("duration")), "size": to_int(fmt.get("size")), "bitrate": to_int(fmt.get("bit_rate")),
            "start_time": to_float(fmt.get("start_time")), "stream_count": to_int(fmt.get("nb_streams")) or len(streams),
            "probe_score": to_int(fmt.get("probe_score")),
        },
        "streams": streams,
    }


def streams_of(probe: Dict[str, Any], ctype: str) -> List[Dict[str, Any]]:
    """Streams of a type in index order, excluding attached pictures (cover art is not a video track)."""
    return [s for s in probe["streams"] if s["type"] == ctype and "attached_pic" not in s["disposition"]]


def select_stream(probe: Dict[str, Any], ctype: str, ordinal: int) -> Dict[str, Any]:
    """The N-th stream of a type (ffmpeg's `0:v:N` / `0:a:N` numbering)."""
    ss = streams_of(probe, ctype)
    if not ss:
        raise AnalysisError("UNSUPPORTED_FORMAT", f"input has no {ctype} stream", {"stream_types": sorted({s['type'] for s in probe['streams']})})
    if ordinal >= len(ss):
        raise AnalysisError("INVALID_INPUT", f"{ctype} stream {ordinal} does not exist (input has {len(ss)})", {"available": len(ss)})
    return ss[ordinal]


def container_duration(probe: Dict[str, Any]) -> Optional[float]:
    d = probe["container"]["duration"]
    if d is None:
        durs = [s["duration"] for s in probe["streams"] if s["duration"] is not None]
        d = max(durs) if durs else None
    return d
