"""noise_floor: short-window RMS levels of one audio stream (ffmpeg astats, reset every window) summarised as a noise
floor (a low percentile), a signal level (a high percentile) and their difference, a no-reference SNR estimate
(ADR-029). Measurement only: it never denoises (audio-production-skill / voice-enhance-skill do that) and never
judges whether the SNR is good enough (the caller's acceptance rule does)."""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional

from ..errors import AnalysisError
from ..probe import select_stream
from ..runner import ffmpeg_null_argv, run_argv
from .base import PROBE_OP, AnalysisContext, Analyzer

_RMS_RE = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?inf|nan|-?[0-9.]+(?:e[-+]?[0-9]+)?)")
REPORTED_PERCENTILES = (5, 10, 25, 50, 75, 90, 95)


def parse_window_levels(stderr: str) -> List[float]:
    """Per-window RMS levels in dBFS, in stream order. Digital silence is -inf (kept, never turned into a number);
    a nan window (no samples) is dropped."""
    out: List[float] = []
    for v in _RMS_RE.findall(stderr):
        if v == "nan":
            continue
        out.append(float(v))
    return out


def percentile(sorted_levels: List[float], p: float) -> float:
    """Nearest-rank percentile (no interpolation, so -inf windows never blend into a finite value)."""
    if not sorted_levels:
        raise ValueError("no levels")
    rank = max(1, int(math.ceil(p / 100.0 * len(sorted_levels))))
    return sorted_levels[min(rank, len(sorted_levels)) - 1]


def _db(v: float) -> Optional[float]:
    return None if math.isinf(v) or math.isnan(v) else round(v, 3)


def summarise(levels: List[float], noise_percentile: float, signal_percentile: float) -> Dict[str, Any]:
    if not levels:
        raise AnalysisError("ANALYSIS_FAILED", "astats reported no RMS windows (empty audio stream?)")
    s = sorted(levels)
    noise = percentile(s, noise_percentile)
    signal = percentile(s, signal_percentile)
    unmeasurable = []
    if math.isinf(noise):
        unmeasurable.append("noise_floor_dbfs")
    if math.isinf(signal):
        unmeasurable.append("signal_level_dbfs")
    snr = None if (math.isinf(noise) or math.isinf(signal)) else round(signal - noise, 3)
    if snr is None:
        unmeasurable.append("snr_db")
    return {
        "window_count": len(levels),
        "digital_silence_windows": sum(1 for v in levels if math.isinf(v)),
        "noise_floor_percentile": noise_percentile, "signal_percentile": signal_percentile,
        "noise_floor_dbfs": _db(noise), "signal_level_dbfs": _db(signal), "snr_db": snr,
        "percentiles_dbfs": {f"p{p}": _db(percentile(s, p)) for p in REPORTED_PERCENTILES},
        "min_window_rms_dbfs": _db(s[0]), "max_window_rms_dbfs": _db(s[-1]),
        "unmeasurable": unmeasurable,
    }


class NoiseAnalyzer(Analyzer):
    id = "noise"
    algorithm_version = "1"
    supported_kinds = ("noise_floor",)
    required_capabilities = ("ffprobe", "ffmpeg", "filter:astats")

    def plan(self, ctx, kind, parameters):
        which = "all channels" if parameters["channel"] < 0 else f"channel {parameters['channel']}"
        return [PROBE_OP, {"executable": "ffmpeg", "purpose": f"astats RMS per {parameters['window']}s window on audio stream "
                                                              f"{parameters['stream']} ({which}), null output"}]

    def analyze(self, ctx: AnalysisContext, kind: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        p = ctx.probe()
        s = select_stream(p, "audio", parameters["stream"])
        rate, channels = s["audio"]["sample_rate"], s["audio"]["channels"]
        if not rate:
            raise AnalysisError("UNSUPPORTED_FORMAT", "audio stream declares no sample rate", {"stream_index": s["index"]})
        channel = parameters["channel"]
        if channel >= 0 and channels is not None and channel >= channels:
            raise AnalysisError("INVALID_INPUT", f"channel {channel} does not exist (stream has {channels})", {"available": channels})
        window_samples = max(1, int(round(rate * parameters["window"])))
        chain = [] if channel < 0 else [f"pan=mono|c0=c{channel}"]
        chain += [f"asetnsamples=n={window_samples}:p=0", "astats=metadata=1:reset=1",
                  "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level"]
        ctx.record("ffmpeg", f"astats windowed RMS on audio stream {parameters['stream']}")
        # repeat+info: ffmpeg would otherwise fold identical consecutive log lines ("Last message repeated N times")
        argv = ffmpeg_null_argv(ctx.exe("ffmpeg"), ctx.input_path, "-map", f"0:a:{parameters['stream']}", "-vn", "-af", ",".join(chain),
                                loglevel="repeat+info")
        r = run_argv(argv, ctx.timeout)
        if r.returncode != 0:
            raise AnalysisError("ANALYSIS_FAILED", "astats failed", {"stderr_tail": "\n".join(r.stderr.strip().splitlines()[-5:])})
        data = summarise(parse_window_levels(r.stderr), parameters["noise_percentile"], parameters["signal_percentile"])
        data.update({
            "stream_index": s["index"], "stream_ordinal": parameters["stream"], "channel": channel, "channels": channels,
            "sample_rate": rate, "window": parameters["window"], "window_samples": window_samples,
            "method": "no-reference percentile estimate: RMS of consecutive non-overlapping windows; noise floor = "
                      "noise_percentile, signal level = signal_percentile, snr_db = signal - noise",
            "measurement": "ffmpeg astats Overall.RMS_level, reset every window (all channels pooled, or one channel via pan)",
        })
        return data
