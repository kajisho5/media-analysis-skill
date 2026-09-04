"""silence: ffmpeg silencedetect over one audio stream, segments classified leading / internal / trailing."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from ..errors import AnalysisError
from ..probe import container_duration, select_stream
from ..runner import ffmpeg_null_argv, run_argv
from .base import PROBE_OP, AnalysisContext, Analyzer

_SIL_RE = re.compile(r"silence_(start|end): (-?[0-9.]+)")


def parse_silencedetect(stderr: str) -> List[Tuple[float, Optional[float]]]:
    """(start, end) pairs; end is None when the silence runs to the end of the stream."""
    out: List[Tuple[float, Optional[float]]] = []
    start: Optional[float] = None
    for kind, val in _SIL_RE.findall(stderr):
        if kind == "start":
            start = float(val)
        elif start is not None:
            out.append((start, float(val)))
            start = None
    if start is not None:
        out.append((start, None))
    return out


def classify(segments: List[Tuple[float, Optional[float]]], duration: Optional[float], edge_tolerance: float) -> List[Dict[str, Any]]:
    result = []
    for s, e in segments:
        end = e if e is not None else duration
        leading = s <= edge_tolerance
        trailing = e is None or (duration is not None and duration - e <= edge_tolerance)
        kind = "entire" if (leading and trailing) else "leading" if leading else "trailing" if trailing else "internal"
        result.append({"start": round(max(0.0, s), 3), "end": round(end, 3) if end is not None else None,
                       "duration": round(end - s, 3) if end is not None else None, "type": kind, "runs_to_end": e is None})
    return result


class SilenceAnalyzer(Analyzer):
    id = "silence"
    supported_kinds = ("silence",)
    required_capabilities = ("ffprobe", "ffmpeg", "filter:silencedetect")

    def plan(self, ctx, kind, parameters):
        return [PROBE_OP, {"executable": "ffmpeg", "purpose": f"silencedetect on audio stream {parameters['stream']} (noise={parameters['threshold_db']}dB, d={parameters['min_duration']}s), null output"}]

    def analyze(self, ctx: AnalysisContext, kind: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        p = ctx.probe()
        s = select_stream(p, "audio", parameters["stream"])
        duration = s["duration"] if s["duration"] is not None else container_duration(p)
        ctx.record("ffmpeg", f"silencedetect on audio stream {parameters['stream']}")
        argv = ffmpeg_null_argv(ctx.exe("ffmpeg"), ctx.input_path, "-map", f"0:a:{parameters['stream']}", "-vn", "-af",
                                f"silencedetect=noise={parameters['threshold_db']}dB:d={parameters['min_duration']}")
        r = run_argv(argv, ctx.timeout)
        if r.returncode != 0:
            raise AnalysisError("ANALYSIS_FAILED", "silencedetect failed", {"stderr_tail": "\n".join(r.stderr.strip().splitlines()[-5:])})
        segs = classify(parse_silencedetect(r.stderr), duration, parameters["edge_tolerance"])
        total = sum(x["duration"] for x in segs if x["duration"] is not None)
        return {
            "stream_index": s["index"], "stream_ordinal": parameters["stream"], "duration": duration,
            "threshold_db": parameters["threshold_db"], "min_duration": parameters["min_duration"], "edge_tolerance": parameters["edge_tolerance"],
            "segments": segs, "segment_count": len(segs), "silent_seconds": round(total, 3),
            "leading": next((x for x in segs if x["type"] in ("leading", "entire")), None),
            "trailing": next((x for x in reversed(segs) if x["type"] in ("trailing", "entire")), None),
            "entirely_silent": len(segs) == 1 and segs[0]["type"] == "entire",
        }
