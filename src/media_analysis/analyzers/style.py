"""edit_style: an editing-style profile of one finished edit, built only from measurements (ADR-030).

    video      cut points (ffmpeg scdet), shot lengths in frames and seconds, their distribution, cuts per minute
    subtitles  soft (text) subtitle streams: event count, events per minute, share of the running time covered,
               on-screen position (bottom / middle / top) from the ASS alignment / \\an / \\pos of each event
    audio      EBU R128 integrated loudness, loudness range, true peak (ebur128); share of the running time that is
               silent / active (silencedetect) and the longest silence

What is not measured is listed in `unmeasured` and never guessed: burned-in (open) subtitles need text detection in
the picture, and whether a music bed runs under the speech needs an audio classifier. Neither is a deterministic
filter output, so both stay out until a method is chosen in its own ADR.

`diff_profiles` compares two profiles metric by metric (reference vs candidate). It states differences; whether a
difference matters is the caller's rule."""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple

from ..errors import AnalysisError
from ..probe import container_duration, streams_of
from ..runner import FF_INPUT_PREFIX, ffmpeg_null_argv, run_argv
from .base import PROBE_OP, AnalysisContext, Analyzer
from .loudness import parse_ebur128
from .scenes import parse_scdet
from .silence import parse_silencedetect

PROFILE_SCHEMA = "media-analysis/edit-style@1"
TEXT_SUBTITLE_CODECS = ("subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text")
HISTOGRAM_EDGES = (0.0, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)          # seconds; the last bucket is open-ended
UNMEASURED = [
    {"item": "subtitles.burned_in", "reason": "subtitles drawn into the picture need text detection, not a filter measurement"},
    {"item": "audio.music_bed", "reason": "telling music from speech needs an audio classifier; active_share is reported instead"},
]


# ---- distribution helpers
def _median(values: List[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return float(s[mid]) if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _nearest_rank(values: List[float], p: float) -> float:
    s = sorted(values)
    rank = max(1, int(math.ceil(p / 100.0 * len(s))))
    return float(s[min(rank, len(s)) - 1])


def distribution(values: List[float]) -> Optional[Dict[str, float]]:
    """min / p10 / p25 / median / p75 / p90 / max / mean; the median averages the two middle values (even count),
    the other percentiles are nearest-rank (always an observed value)."""
    if not values:
        return None
    return {"min": float(min(values)), "p10": _nearest_rank(values, 10), "p25": _nearest_rank(values, 25), "median": _median(values),
            "p75": _nearest_rank(values, 75), "p90": _nearest_rank(values, 90), "max": float(max(values)),
            "mean": round(sum(values) / len(values), 6)}


def histogram(seconds: List[float]) -> List[Dict[str, Any]]:
    out = []
    for i, lo in enumerate(HISTOGRAM_EDGES):
        hi = HISTOGRAM_EDGES[i + 1] if i + 1 < len(HISTOGRAM_EDGES) else None
        out.append({"from_seconds": lo, "to_seconds": hi, "count": sum(1 for v in seconds if v >= lo and (hi is None or v < hi))})
    return out


# ---- video
def shots_from_cuts(cut_frames: List[int], frame_count: int, fps: float, min_shot_frames: int) -> List[Dict[str, Any]]:
    """Shots between kept cuts. A cut closer than `min_shot_frames` to the previous kept cut (or to frame 0) is
    dropped, so a flash or a dissolve that scdet flags on consecutive frames does not become one-frame shots."""
    kept: List[int] = []
    last = 0
    for f in sorted(set(cut_frames)):
        if f <= 0 or f >= frame_count:
            continue
        if f - last < min_shot_frames:
            continue
        kept.append(f)
        last = f
    bounds = [0] + kept + [frame_count]
    shots = []
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        shots.append({"index": i, "start_frame": a, "frames": b - a, "start": round(a / fps, 6), "end": round(b / fps, 6),
                      "seconds": round((b - a) / fps, 6)})
    return shots


def video_profile(cut_frames: List[int], frame_count: int, fps: float, min_shot_duration: float) -> Dict[str, Any]:
    min_frames = max(1, int(round(min_shot_duration * fps)))
    shots = shots_from_cuts(cut_frames, frame_count, fps, min_frames)
    duration = frame_count / fps
    frames = [float(s["frames"]) for s in shots]
    secs = [s["seconds"] for s in shots]
    cuts = len(shots) - 1
    return {"status": "measured", "fps": fps, "frame_count": frame_count, "duration": round(duration, 6),
            "cut_count": cuts, "cuts_per_minute": round(cuts / duration * 60.0, 6) if duration > 0 else None,
            "shot_count": len(shots), "shot_frames": distribution(frames), "shot_seconds": distribution(secs),
            "shot_length_histogram": histogram(secs), "shots": shots, "min_shot_frames": min_frames}


# ---- subtitles
_ASS_TIME = re.compile(r"^(\d+):(\d{2}):(\d{2})[.,](\d{1,3})$")
_AN = re.compile(r"\\an([1-9])")
_A_LEGACY = re.compile(r"\\a(\d{1,2})(?!\d)")
_POS = re.compile(r"\\(?:pos|move)\(\s*(-?[0-9.]+)\s*,\s*(-?[0-9.]+)")


def _ass_seconds(text: str) -> Optional[float]:
    m = _ASS_TIME.match(text.strip())
    if not m:
        return None
    frac = m.group(4)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(frac) / (10 ** len(frac))


def _band_of_alignment(n: int) -> str:
    return "bottom" if n in (1, 2, 3) else "middle" if n in (4, 5, 6) else "top"


def _band_of_legacy(n: int) -> Optional[str]:
    # SSA \a: 1-3 bottom, 5-7 top, 9-11 middle (+4 = top, +8 = middle)
    if 1 <= n <= 3:
        return "bottom"
    if 5 <= n <= 7:
        return "top"
    if 9 <= n <= 11:
        return "middle"
    return None


def parse_ass_events(text: str) -> List[Dict[str, Any]]:
    """Dialogue events of an ASS document (ffmpeg's text-subtitle output) with start, end and vertical band."""
    play_res_y: Optional[float] = None
    style_fields: List[str] = []
    style_band: Dict[str, str] = {}
    event_fields: List[str] = []
    section = ""
    events: List[Dict[str, Any]] = []
    for raw in text.splitlines():
        ln = raw.strip()
        if ln.startswith("[") and ln.endswith("]"):
            section = ln.lower()
            continue
        if ln.lower().startswith("playresy:"):
            try:
                play_res_y = float(ln.split(":", 1)[1])
            except ValueError:
                play_res_y = None
        elif section in ("[v4+ styles]", "[v4 styles]") and ln.lower().startswith("format:"):
            style_fields = [f.strip().lower() for f in ln.split(":", 1)[1].split(",")]
        elif section in ("[v4+ styles]", "[v4 styles]") and ln.lower().startswith("style:") and style_fields:
            vals = [v.strip() for v in ln.split(":", 1)[1].split(",", len(style_fields) - 1)]
            row = dict(zip(style_fields, vals))
            try:
                n = int(row.get("alignment", "2"))
            except ValueError:
                n = 2
            band = _band_of_alignment(n) if section == "[v4+ styles]" else (_band_of_legacy(n) or "bottom")
            style_band[row.get("name", "Default")] = band
        elif section == "[events]" and ln.lower().startswith("format:"):
            event_fields = [f.strip().lower() for f in ln.split(":", 1)[1].split(",")]
        elif section == "[events]" and ln.lower().startswith("dialogue:") and event_fields:
            vals = ln.split(":", 1)[1].split(",", len(event_fields) - 1)
            row = dict(zip(event_fields, (v.strip() if i < len(event_fields) - 1 else v for i, v in enumerate(vals))))
            start, end = _ass_seconds(row.get("start", "")), _ass_seconds(row.get("end", ""))
            if start is None or end is None or end <= start:
                continue
            body = row.get("text", "")
            band = style_band.get(row.get("style", "Default"), "bottom")
            m = _AN.search(body)
            if m:
                band = _band_of_alignment(int(m.group(1)))
            else:
                lm = _A_LEGACY.search(body)
                if lm and _band_of_legacy(int(lm.group(1))):
                    band = _band_of_legacy(int(lm.group(1))) or band
            pm = _POS.search(body)
            if pm and play_res_y:
                y = float(pm.group(2)) / play_res_y
                band = "top" if y < 1 / 3 else "middle" if y < 2 / 3 else "bottom"
            events.append({"start": round(start, 3), "end": round(end, 3), "position": band})
    return events


def _covered_seconds(events: List[Dict[str, Any]]) -> float:
    total, cur_s, cur_e = 0.0, None, None
    for e in sorted(events, key=lambda x: x["start"]):
        if cur_e is None or e["start"] > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = e["start"], e["end"]
        else:
            cur_e = max(cur_e, e["end"])
    if cur_e is not None:
        total += cur_e - cur_s
    return total


def subtitle_stream_profile(events: List[Dict[str, Any]], duration: Optional[float]) -> Dict[str, Any]:
    positions = {"bottom": 0, "middle": 0, "top": 0}
    for e in events:
        positions[e["position"]] += 1
    dominant = max(positions, key=lambda k: (positions[k], k == "bottom")) if events else None
    minutes = duration / 60.0 if duration else None
    return {"events": len(events), "events_per_minute": round(len(events) / minutes, 6) if minutes else None,
            "coverage_share": round(min(1.0, _covered_seconds(events) / duration), 6) if duration else None,
            "event_seconds": distribution([round(e["end"] - e["start"], 3) for e in events]),
            "positions": positions, "dominant_position": dominant}


# ---- audio
def audio_activity(silences: List[Tuple[float, Optional[float]]], duration: Optional[float]) -> Dict[str, Any]:
    if not duration:
        return {"silence_share": None, "active_share": None, "longest_silence": None, "silence_count": len(silences)}
    lengths = [((e if e is not None else duration) - max(0.0, s)) for s, e in silences]
    silent = min(duration, sum(max(0.0, x) for x in lengths))
    return {"silence_share": round(silent / duration, 6), "active_share": round(1 - silent / duration, 6),
            "longest_silence": round(max(lengths), 6) if lengths else 0.0, "silence_count": len(silences)}


class StyleAnalyzer(Analyzer):
    id = "style"
    algorithm_version = "1"
    supported_kinds = ("edit_style",)
    required_capabilities = ("ffprobe", "ffmpeg", "filter:scdet", "filter:ebur128", "filter:silencedetect")

    def plan(self, ctx, kind, parameters):
        return [PROBE_OP,
                {"executable": "ffmpeg", "purpose": f"scdet (threshold={parameters['threshold']}) on video stream {parameters['video_stream']}, null output"},
                {"executable": "ffmpeg", "purpose": "each text subtitle stream converted to ASS on stdout (event times and positions)"},
                {"executable": "ffmpeg", "purpose": f"ebur128 + silencedetect (noise={parameters['silence_threshold_db']}dB) on audio stream {parameters['audio_stream']}, null output"}]

    def _video(self, ctx: AnalysisContext, p: Dict[str, Any], parameters: Dict[str, Any]) -> Dict[str, Any]:
        vs = streams_of(p, "video")
        n = parameters["video_stream"]
        if n >= len(vs):
            return {"status": "not_performed", "reason": "no such video stream"}
        s = vs[n]
        fps = s["video"]["fps"]
        if not fps:
            return {"status": "not_performed", "reason": "video stream declares no frame rate", "stream_index": s["index"]}
        ctx.record("ffmpeg", f"scdet on video stream {n}")
        argv = ffmpeg_null_argv(ctx.exe("ffmpeg"), ctx.input_path, "-map", f"0:v:{n}", "-an", "-sn", "-vf",
                                f"scdet=threshold={parameters['threshold']},metadata=print:file=-", loglevel="error")
        r = run_argv(argv, ctx.timeout)
        if r.returncode != 0:
            raise AnalysisError("ANALYSIS_FAILED", "scdet failed", {"stderr_tail": "\n".join(r.stderr.strip().splitlines()[-5:])})
        frames_seen = [int(m) for m in re.findall(r"^frame:(\d+)\s", r.stdout, re.M)]
        frame_count = (max(frames_seen) + 1) if frames_seen else s["nb_frames"]
        if not frame_count:
            duration = s["duration"] if s["duration"] is not None else container_duration(p)
            frame_count = int(round(duration * fps)) if duration else 0
        if frame_count <= 0:
            return {"status": "not_performed", "reason": "video stream has no frames", "stream_index": s["index"]}
        out = video_profile([c["frame"] for c in parse_scdet(r.stdout)], frame_count, fps, parameters["min_shot_duration"])
        out.update({"stream_index": s["index"], "stream_ordinal": n, "threshold": parameters["threshold"],
                    "min_shot_duration": parameters["min_shot_duration"]})
        return out

    def _subtitles(self, ctx: AnalysisContext, p: Dict[str, Any], duration: Optional[float]) -> Dict[str, Any]:
        subs = streams_of(p, "subtitle")
        streams = []
        for ordinal, s in enumerate(subs):
            row: Dict[str, Any] = {"stream_index": s["index"], "stream_ordinal": ordinal, "codec": s["codec"], "language": s["language"]}
            if (s["codec"] or "") not in TEXT_SUBTITLE_CODECS:
                row.update({"status": "not_performed", "reason": "bitmap or unknown subtitle codec: no text events to read"})
                streams.append(row)
                continue
            ctx.record("ffmpeg", f"subtitle stream {ordinal} as ASS on stdout")
            argv = [ctx.exe("ffmpeg"), *FF_INPUT_PREFIX, "-loglevel", "error", "-i", ctx.input_path, "-map", f"0:s:{ordinal}",
                    "-c:s", "ass", "-f", "ass", "-"]
            r = run_argv(argv, ctx.timeout)
            if r.returncode != 0:
                row.update({"status": "not_performed", "reason": "subtitle stream could not be read as text"})
            else:
                row.update({"status": "measured", **subtitle_stream_profile(parse_ass_events(r.stdout), duration)})
            streams.append(row)
        measured = [x for x in streams if x["status"] == "measured"]
        first = measured[0] if measured else None
        return {"soft_stream_count": len(subs), "present": bool(measured and first["events"] > 0), "streams": streams,
                "events_per_minute": first["events_per_minute"] if first else None,
                "coverage_share": first["coverage_share"] if first else None,
                "dominant_position": first["dominant_position"] if first else None,
                "summary_stream_index": first["stream_index"] if first else None,
                "burned_in": "not_measured"}

    def _audio(self, ctx: AnalysisContext, p: Dict[str, Any], parameters: Dict[str, Any]) -> Dict[str, Any]:
        aus = streams_of(p, "audio")
        n = parameters["audio_stream"]
        if n >= len(aus):
            return {"status": "not_performed", "reason": "no such audio stream", "music_bed": "not_measured"}
        s = aus[n]
        duration = s["duration"] if s["duration"] is not None else container_duration(p)
        ctx.record("ffmpeg", f"ebur128 + silencedetect on audio stream {n}")
        argv = ffmpeg_null_argv(ctx.exe("ffmpeg"), ctx.input_path, "-map", f"0:a:{n}", "-vn", "-sn", "-af",
                                f"ebur128=peak=true:framelog=quiet,silencedetect=noise={parameters['silence_threshold_db']}dB:"
                                f"d={parameters['silence_min_duration']}")
        r = run_argv(argv, ctx.timeout)
        if r.returncode != 0:
            raise AnalysisError("ANALYSIS_FAILED", "ebur128 / silencedetect failed", {"stderr_tail": "\n".join(r.stderr.strip().splitlines()[-5:])})
        loud = parse_ebur128(r.stderr)
        out: Dict[str, Any] = {"status": "measured", "stream_index": s["index"], "stream_ordinal": n, "duration": duration,
                               "integrated_lufs": loud["integrated_lufs"], "loudness_range_lu": loud["loudness_range_lu"],
                               "true_peak_dbtp": loud["true_peak_dbtp"], "integrated_below_absolute_gate": loud["integrated_below_absolute_gate"],
                               "silence_threshold_db": parameters["silence_threshold_db"], "silence_min_duration": parameters["silence_min_duration"]}
        out.update(audio_activity(parse_silencedetect(r.stderr), duration))
        out["music_bed"] = "not_measured"
        return out

    def analyze(self, ctx: AnalysisContext, kind: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        p = ctx.probe()
        duration = container_duration(p)
        if not streams_of(p, "video") and not streams_of(p, "audio"):
            raise AnalysisError("UNSUPPORTED_FORMAT", "input has neither a video nor an audio stream")
        return {"profile_schema": PROFILE_SCHEMA, "duration": duration, "video": self._video(ctx, p, parameters),
                "subtitles": self._subtitles(ctx, p, duration), "audio": self._audio(ctx, p, parameters),
                "unmeasured": [dict(u) for u in UNMEASURED]}


# ---- comparison
NUMERIC_METRICS = (
    "video.cuts_per_minute", "video.cut_count", "video.shot_count",
    "video.shot_frames.median", "video.shot_frames.mean", "video.shot_frames.p10", "video.shot_frames.p90",
    "video.shot_frames.min", "video.shot_frames.max",
    "video.shot_seconds.median", "video.shot_seconds.mean",
    "subtitles.events_per_minute", "subtitles.coverage_share",
    "audio.integrated_lufs", "audio.loudness_range_lu", "audio.true_peak_dbtp", "audio.silence_share", "audio.longest_silence",
)
CATEGORICAL_METRICS = ("video.fps", "subtitles.present", "subtitles.dominant_position")


def profile_of(doc: Any) -> Dict[str, Any]:
    """Accept a profile (observation `data`), an Observation, a result, or a response document holding one edit_style
    observation, and return the profile."""
    if isinstance(doc, dict) and doc.get("profile_schema") == PROFILE_SCHEMA:
        return doc
    if isinstance(doc, dict) and isinstance(doc.get("data"), dict):
        return profile_of(doc["data"])
    if isinstance(doc, dict) and isinstance(doc.get("observation"), dict):
        return profile_of(doc["observation"])
    if isinstance(doc, dict) and isinstance(doc.get("observations"), list):
        found = [o for o in doc["observations"] if isinstance(o, dict) and o.get("kind") == "edit_style"]
        if len(found) == 1:
            return profile_of(found[0])
    raise AnalysisError("INVALID_INPUT", f"not an edit_style profile (expected profile_schema {PROFILE_SCHEMA})")


def _get(profile: Dict[str, Any], path: str) -> Any:
    cur: Any = profile
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def diff_profiles(reference: Any, candidate: Any) -> Dict[str, Any]:
    """Metric-by-metric difference, candidate minus reference. A metric missing on either side is `unavailable`
    (with which side), never zero."""
    ref, cand = profile_of(reference), profile_of(candidate)
    items: List[Dict[str, Any]] = []
    for m in NUMERIC_METRICS:
        a, b = _get(ref, m), _get(cand, m)
        if not isinstance(a, (int, float)) or isinstance(a, bool) or not isinstance(b, (int, float)) or isinstance(b, bool):
            missing = [side for side, v in (("reference", a), ("candidate", b)) if not isinstance(v, (int, float)) or isinstance(v, bool)]
            items.append({"metric": m, "status": "unavailable", "missing_in": missing, "reference": a, "candidate": b})
            continue
        diff = b - a
        items.append({"metric": m, "status": "compared", "reference": a, "candidate": b, "difference": round(diff, 6),
                      "relative": round(diff / abs(a), 6) if a != 0 else None, "equal": abs(diff) < 1e-9})
    for m in CATEGORICAL_METRICS:
        a, b = _get(ref, m), _get(cand, m)
        if a is None or b is None:
            items.append({"metric": m, "status": "unavailable", "missing_in": [s for s, v in (("reference", a), ("candidate", b)) if v is None],
                          "reference": a, "candidate": b})
        else:
            items.append({"metric": m, "status": "compared", "reference": a, "candidate": b, "equal": a == b})
    return {"schema": "media-analysis/edit-style-diff@1", "direction": "candidate - reference",
            "compared": sum(1 for i in items if i["status"] == "compared"),
            "different": [i["metric"] for i in items if i["status"] == "compared" and not i["equal"]],
            "items": items}
