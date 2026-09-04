"""scene_detection: visual scene cuts from ffmpeg's scdet filter. A "scene" here is an interval between measured
picture changes with a score above the threshold. Nothing semantic is inferred."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..errors import AnalysisError
from ..probe import container_duration, select_stream
from ..runner import ffmpeg_null_argv, run_argv
from .base import PROBE_OP, AnalysisContext, Analyzer

_FRAME = re.compile(r"^frame:(\d+)\s+pts:(-?\d+)\s+pts_time:(-?[0-9.]+)")
_KV = re.compile(r"^lavfi\.scd\.(\w+)=(-?[0-9.]+)")


def parse_scdet(stdout: str) -> List[Dict[str, float]]:
    """Frames the filter flagged as scene changes (those carrying lavfi.scd.time), with their score."""
    cuts: List[Dict[str, float]] = []
    cur: Optional[Dict[str, Any]] = None
    for ln in stdout.splitlines():
        m = _FRAME.match(ln)
        if m:
            cur = {"frame": int(m.group(1)), "time": float(m.group(3)), "score": None, "flagged": False}
            continue
        k = _KV.match(ln)
        if k and cur is not None:
            if k.group(1) == "score":
                cur["score"] = float(k.group(2))
            elif k.group(1) == "time":
                cur["flagged"] = True
                cuts.append({"frame": cur["frame"], "time": round(cur["time"], 3), "score": round(cur["score"] or 0.0, 3)})
    return cuts


def build_scenes(cuts: List[Dict[str, float]], duration: Optional[float], min_scene: float) -> List[Dict[str, Any]]:
    times: List[Dict[str, float]] = []
    last = 0.0
    for c in cuts:
        if c["time"] <= 0:
            continue
        if times and c["time"] - last < min_scene:
            continue
        times.append(c)
        last = c["time"]
    bounds = [0.0] + [c["time"] for c in times] + ([duration] if duration is not None else [None])
    scenes = []
    for i in range(len(bounds) - 1):
        s, e = bounds[i], bounds[i + 1]
        scenes.append({"index": i, "start": s, "end": e, "duration": round(e - s, 3) if e is not None else None,
                       "representative_time": s, "cut_score": times[i - 1]["score"] if i > 0 else None})
    return scenes


class SceneAnalyzer(Analyzer):
    id = "scenes"
    supported_kinds = ("scene_detection",)
    required_capabilities = ("ffprobe", "ffmpeg", "filter:scdet")

    def plan(self, ctx, kind, parameters):
        return [PROBE_OP, {"executable": "ffmpeg", "purpose": f"scdet (threshold={parameters['threshold']}) on video stream {parameters['stream']}, metadata to stdout, null output"}]

    def analyze(self, ctx: AnalysisContext, kind: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        p = ctx.probe()
        s = select_stream(p, "video", parameters["stream"])
        duration = s["duration"] if s["duration"] is not None else container_duration(p)
        ctx.record("ffmpeg", f"scdet on video stream {parameters['stream']}")
        argv = ffmpeg_null_argv(ctx.exe("ffmpeg"), ctx.input_path, "-map", f"0:v:{parameters['stream']}", "-an", "-sn", "-vf",
                                f"scdet=threshold={parameters['threshold']},metadata=print:file=-", loglevel="error")
        r = run_argv(argv, ctx.timeout)
        if r.returncode != 0:
            raise AnalysisError("ANALYSIS_FAILED", "scdet failed", {"stderr_tail": "\n".join(r.stderr.strip().splitlines()[-5:])})
        cuts = parse_scdet(r.stdout)
        scenes = build_scenes(cuts, duration, parameters["min_scene_duration"])
        return {"stream_index": s["index"], "stream_ordinal": parameters["stream"], "duration": duration, "method": "ffmpeg scdet score",
                "threshold": parameters["threshold"], "min_scene_duration": parameters["min_scene_duration"],
                "cuts": cuts, "cut_count": len(cuts), "scenes": scenes, "scene_count": len(scenes)}
