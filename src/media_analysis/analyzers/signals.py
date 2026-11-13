"""highlight_signals: speech-energy peaks, speaking-rate changes and screen-change bursts, each as time ranges with a
score (ADR-036). Plain signal measurement with ffmpeg filters and the standard library: no model, no judgement of
what is a highlight (the caller, e.g. `media-analysis highlights` or the agent, ranks and picks).

  speech_energy_peaks    the audio band-passed to the speech band (300-3400 Hz) and measured as RMS level per 10 ms
                         frame (ffmpeg astats); frames are pooled into `energy_window` windows (power mean). The
                         baseline is the median level of the active windows (level >= active_floor_db). A peak is a
                         run of windows at or above baseline + peak_db; score = the largest lift over the baseline (dB).
  speaking_rate_changes  syllable nuclei counted on the same 10 ms envelope (smoothed over 50 ms): a nucleus is a local
                         maximum at or above active_floor_db, at least nucleus_db above the lowest level since the
                         previous nucleus and at least 0.08 s after it. The rate is nuclei per second in a sliding
                         `rate_window` (every `rate_hop` s; windows less than half active have no rate); the baseline is
                         the median rate. A change is a run of windows whose rate differs from the baseline by at least
                         `rate_change` (relative), trimmed to the windows past half-way between the baseline and the
                         run's extreme rate; score = extreme rate / baseline. It is an acoustic rate, not words.
  screen_change_bursts   picture cuts from ffmpeg scdet (score >= scene_threshold); a burst is a run of cuts with gaps
                         of at most burst_gap seconds and at least burst_min_cuts cuts, from the first cut to the last;
                         score = cuts per second inside it.
A section whose stream does not exist is `not_performed` with the reason; the other sections are still measured."""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..errors import AnalysisError
from ..probe import container_duration, streams_of
from ..runner import ffmpeg_null_argv, run_argv
from .base import PROBE_OP, AnalysisContext, Analyzer
from .scenes import parse_scdet

FRAME = 0.01                       # envelope frame, seconds
SAMPLE_RATE = 16000
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME)
SPEECH_BAND = (300, 3400)
SMOOTH_FRAMES = 5                  # 50 ms moving average before nuclei are picked
MIN_NUCLEUS_GAP = 0.08             # seconds between syllable nuclei
_RMS_RE = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?inf|nan|-?[0-9.]+(?:e[-+]?[0-9]+)?)")


def parse_frame_levels(text: str) -> List[float]:
    """RMS level (dBFS) per 10 ms frame in stream order; digital silence is -inf, a nan frame counts as -inf."""
    out = []
    for v in _RMS_RE.findall(text):
        out.append(float("-inf") if v in ("nan", "-inf") else float(v))
    return out


def _power(db: float) -> float:
    return 0.0 if math.isinf(db) else 10.0 ** (db / 10.0)


def _db(power: float) -> float:
    return float("-inf") if power <= 0 else 10.0 * math.log10(power)


def _median(values: Sequence[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


# ---- speech-energy peaks
def energy_peaks(levels: Sequence[float], window: float, peak_db: float, active_floor_db: float) -> Dict[str, Any]:
    k = max(1, int(round(window / FRAME)))
    win = k * FRAME
    wl = [_db(sum(_power(v) for v in levels[i:i + k]) / len(levels[i:i + k])) for i in range(0, len(levels), k)]
    active = [v for v in wl if v >= active_floor_db]
    if not active:
        return {"status": "measured", "spans": [], "span_count": 0, "baseline_dbfs": None, "active_windows": 0, "window": win,
                "window_count": len(wl), "note": "no window reaches active_floor_db"}
    base = _median(active)
    spans: List[Dict[str, Any]] = []
    i = 0
    total = len(levels) * FRAME
    while i < len(wl):
        if wl[i] >= base + peak_db:
            j = i
            while j + 1 < len(wl) and wl[j + 1] >= base + peak_db:
                j += 1
            lifts = [wl[t] - base for t in range(i, j + 1)]
            top = max(range(i, j + 1), key=lambda t: wl[t])
            spans.append({"start": round(i * win, 4), "end": round(min(total, (j + 1) * win), 4),
                          "duration": round(min(total, (j + 1) * win) - i * win, 4), "score": round(max(lifts), 3),
                          "mean_lift_db": round(sum(lifts) / len(lifts), 3), "peak_level_dbfs": round(wl[top], 3),
                          "peak_time": round(top * win + win / 2, 4)})
            i = j + 1
        else:
            i += 1
    return {"status": "measured", "spans": spans, "span_count": len(spans), "baseline_dbfs": round(base, 3),
            "active_windows": len(active), "window": win, "window_count": len(wl)}


# ---- speaking-rate changes
def nuclei(levels: Sequence[float], active_floor_db: float, nucleus_db: float) -> List[float]:
    """Syllable-nucleus times (seconds, frame centres) on the 50 ms-smoothed envelope."""
    p = [_power(v) for v in levels]
    half = SMOOTH_FRAMES // 2
    env = []
    for i in range(len(p)):
        seg = p[max(0, i - half):i + half + 1]
        env.append(_db(sum(seg) / len(seg)))
    out: List[float] = []
    low = float("inf")
    last_t = -1.0
    last_v = float("-inf")
    for i, v in enumerate(env):
        low = min(low, v)
        left = env[i - 1] if i > 0 else float("-inf")
        right = env[i + 1] if i + 1 < len(env) else float("-inf")
        if not (v >= left and v > right) or v < active_floor_db:
            continue
        t = (i + 0.5) * FRAME
        if v - low < nucleus_db:
            continue
        if out and t - last_t < MIN_NUCLEUS_GAP:
            if v > last_v:            # two maxima too close: keep the higher one
                out[-1], last_t, last_v = t, t, v
            continue
        out.append(t)
        last_t, last_v, low = t, v, v
    return [round(t, 4) for t in out]


def rate_changes(levels: Sequence[float], window: float, hop: float, change: float, active_floor_db: float,
                 nucleus_db: float) -> Dict[str, Any]:
    total = len(levels) * FRAME
    times = nuclei(levels, active_floor_db, nucleus_db)
    rows: List[Tuple[float, Optional[float]]] = []
    if total >= window:
        k = 0
        while True:
            c = window / 2 + k * hop
            if c + window / 2 > total + 1e-9:
                break
            a, b = c - window / 2, c + window / 2
            fa, fb = int(round(a / FRAME)), int(round(b / FRAME))
            act = sum(1 for v in levels[fa:fb] if v >= active_floor_db) / max(1, fb - fa)
            n = sum(1 for t in times if a <= t < b)
            rows.append((round(c, 4), round(n / window, 4) if act >= 0.5 else None))
            k += 1
    rates = [r for _, r in rows if r is not None]
    base = {"status": "measured", "nucleus_count": len(times), "window": window, "hop": hop, "change": change,
            "window_count": len(rows), "rated_windows": len(rates)}
    if not rates or _median(rates) <= 0:
        return {**base, "spans": [], "span_count": 0, "baseline_rate": None, "note": "no window is active enough to rate"}
    med = _median(rates)
    spans: List[Dict[str, Any]] = []
    i = 0
    while i < len(rows):
        r = rows[i][1]
        d = 0 if r is None else (1 if r >= med * (1 + change) else (-1 if r <= med * (1 - change) else 0))
        if d == 0:
            i += 1
            continue
        j = i
        while j + 1 < len(rows):
            rn = rows[j + 1][1]
            if rn is None or not ((rn >= med * (1 + change)) if d > 0 else (rn <= med * (1 - change))):
                break
            j += 1
        run = rows[i:j + 1]
        ext = max(r for _, r in run) if d > 0 else min(r for _, r in run)
        mid = med + (ext - med) / 2
        core = [(c, r) for c, r in run if (r >= mid if d > 0 else r <= mid)]
        s, e = max(0.0, core[0][0] - hop / 2), min(total, core[-1][0] + hop / 2)
        spans.append({"start": round(s, 4), "end": round(e, 4), "duration": round(e - s, 4), "direction": "faster" if d > 0 else "slower",
                      "rate": round(ext, 4), "score": round(ext / med, 4)})
        i = j + 1
    return {**base, "spans": spans, "span_count": len(spans), "baseline_rate": round(med, 4)}


# ---- screen-change bursts
def cut_bursts(cut_times: Sequence[float], gap: float, min_cuts: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    run: List[float] = []
    for t in list(cut_times) + [None]:
        if t is not None and (not run or t - run[-1] <= gap + 1e-9):
            run.append(t)
            continue
        if len(run) >= min_cuts:
            d = run[-1] - run[0]
            out.append({"start": round(run[0], 4), "end": round(run[-1], 4), "duration": round(d, 4), "cut_count": len(run),
                        "score": round(len(run) / d, 4) if d > 0 else None})
        run = [t] if t is not None else []
    return out


class SignalsAnalyzer(Analyzer):
    id = "signals"
    algorithm_version = "1"
    supported_kinds = ("highlight_signals",)
    required_capabilities = ("ffprobe", "ffmpeg", "filter:astats", "filter:scdet")

    def plan(self, ctx, kind, parameters):
        return [PROBE_OP,
                {"executable": "ffmpeg", "purpose": f"speech-band ({SPEECH_BAND[0]}-{SPEECH_BAND[1]} Hz) astats RMS per 10 ms on audio stream "
                                                    f"{parameters['audio_stream']}, metadata to stdout, null output"},
                {"executable": "ffmpeg", "purpose": f"scdet (threshold={parameters['scene_threshold']}) on video stream {parameters['video_stream']}, "
                                                    "metadata to stdout, null output"}]

    def _levels(self, ctx: AnalysisContext, n: int) -> List[float]:
        chain = [f"aresample={SAMPLE_RATE}", "aformat=channel_layouts=mono", f"highpass=f={SPEECH_BAND[0]}", f"lowpass=f={SPEECH_BAND[1]}",
                 f"asetnsamples=n={FRAME_SAMPLES}:p=0", "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level",
                 "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-"]
        ctx.record("ffmpeg", f"speech-band RMS per 10 ms on audio stream {n}")
        argv = ffmpeg_null_argv(ctx.exe("ffmpeg"), ctx.input_path, "-map", f"0:a:{n}", "-vn", "-sn", "-af", ",".join(chain), loglevel="error")
        r = run_argv(argv, ctx.timeout)
        if r.returncode != 0:
            raise AnalysisError("ANALYSIS_FAILED", "speech-band level measurement failed", {"stderr_tail": "\n".join(r.stderr.strip().splitlines()[-5:])})
        return parse_frame_levels(r.stdout)

    def _cuts(self, ctx: AnalysisContext, n: int, threshold: float) -> List[Dict[str, float]]:
        ctx.record("ffmpeg", f"scdet on video stream {n}")
        argv = ffmpeg_null_argv(ctx.exe("ffmpeg"), ctx.input_path, "-map", f"0:V:{n}", "-an", "-sn", "-vf",
                                f"scdet=threshold={threshold},metadata=print:file=-", loglevel="error")
        r = run_argv(argv, ctx.timeout)
        if r.returncode != 0:
            raise AnalysisError("ANALYSIS_FAILED", "scdet failed", {"stderr_tail": "\n".join(r.stderr.strip().splitlines()[-5:])})
        return [c for c in parse_scdet(r.stdout) if c["time"] > 0]

    def analyze(self, ctx: AnalysisContext, kind: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        p = ctx.probe()
        aus, vs = streams_of(p, "audio"), streams_of(p, "video")
        if not aus and not vs:
            raise AnalysisError("UNSUPPORTED_FORMAT", "input has neither a video nor an audio stream")
        data: Dict[str, Any] = {"duration": container_duration(p)}
        na, nv = parameters["audio_stream"], parameters["video_stream"]
        if na < len(aus):
            levels = self._levels(ctx, na)
            data["audio_stream_index"] = aus[na]["index"]
            data["analysed_seconds"] = round(len(levels) * FRAME, 4)
            data["speech_energy_peaks"] = {**energy_peaks(levels, parameters["energy_window"], parameters["peak_db"], parameters["active_floor_db"]),
                                           "peak_db": parameters["peak_db"], "band_hz": list(SPEECH_BAND), "unit": "dB over the baseline"}
            data["speaking_rate_changes"] = {**rate_changes(levels, parameters["rate_window"], parameters["rate_hop"], parameters["rate_change"],
                                                            parameters["active_floor_db"], parameters["nucleus_db"]),
                                             "unit": "rate / baseline rate (syllable nuclei per second)"}
        else:
            reason = {"status": "not_performed", "reason": "no such audio stream"}
            data.update({"audio_stream_index": None, "analysed_seconds": None, "speech_energy_peaks": dict(reason),
                         "speaking_rate_changes": dict(reason)})
        if nv < len(vs):
            cuts = self._cuts(ctx, nv, parameters["scene_threshold"])
            bursts = cut_bursts([c["time"] for c in cuts], parameters["burst_gap"], parameters["burst_min_cuts"])
            data["video_stream_index"] = vs[nv]["index"]
            data["screen_change_bursts"] = {"status": "measured", "spans": bursts, "span_count": len(bursts), "cut_count": len(cuts),
                                            "cut_times": [c["time"] for c in cuts], "threshold": parameters["scene_threshold"],
                                            "burst_gap": parameters["burst_gap"], "burst_min_cuts": parameters["burst_min_cuts"],
                                            "unit": "cuts per second"}
        else:
            data["video_stream_index"] = None
            data["screen_change_bursts"] = {"status": "not_performed", "reason": "no such video stream"}
        data["method"] = ("speech-band (300-3400 Hz) RMS per 10 ms (astats): energy peaks over the median active window level, "
                          "syllable-nucleus rate changes against the median rate; scdet cut bursts (ADR-036). Measured, not judged")
        return data
