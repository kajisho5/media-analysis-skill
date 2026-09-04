"""loudness: EBU R128 measurement with the ebur128 filter (integrated, LRA, true peak). Measurement only; no
normalisation (that is ffmpeg-skill/loudness)."""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from ..errors import AnalysisError
from ..probe import select_stream
from ..runner import ffmpeg_null_argv, run_argv
from .base import PROBE_OP, AnalysisContext, Analyzer

_RE = {
    "integrated_lufs": re.compile(r"Integrated loudness:\s*\n\s*I:\s*(-?[0-9.]+|-inf|nan) LUFS"),
    "integrated_threshold_lufs": re.compile(r"Integrated loudness:\s*\n\s*I:.*\n\s*Threshold:\s*(-?[0-9.]+|-inf|nan) LUFS"),
    "loudness_range_lu": re.compile(r"LRA:\s*(-?[0-9.]+|-inf|nan) LU"),
    "lra_low_lufs": re.compile(r"LRA low:\s*(-?[0-9.]+|-inf|nan) LUFS"),
    "lra_high_lufs": re.compile(r"LRA high:\s*(-?[0-9.]+|-inf|nan) LUFS"),
    "true_peak_dbtp": re.compile(r"True peak:\s*\n\s*Peak:\s*(-?[0-9.]+|-inf|nan) dBFS"),
}


def _num(v: str) -> Optional[float]:
    if v in ("-inf", "nan", "inf"):
        return None
    return float(v)


def parse_ebur128(stderr: str) -> Dict[str, Any]:
    """Parse the ebur128 summary block. Values ffmpeg reports as -inf / nan (fully silent input) become null and are
    flagged in `unmeasurable` instead of being turned into numbers."""
    if "Integrated loudness:" not in stderr:
        raise AnalysisError("ANALYSIS_FAILED", "ebur128 summary not found in ffmpeg output")
    out: Dict[str, Any] = {}
    unmeasurable = []
    for key, rx in _RE.items():
        m = rx.search(stderr)
        if not m:
            out[key] = None
            unmeasurable.append(key)
            continue
        out[key] = _num(m.group(1))
        if out[key] is None:
            unmeasurable.append(key)
    out["unmeasurable"] = unmeasurable
    # ffmpeg reports -70.0 LUFS (the BS.1770 absolute gate) for input with no gated blocks; that is a floor, not a level
    out["integrated_below_absolute_gate"] = out["integrated_lufs"] is not None and out["integrated_lufs"] <= -70.0
    return out


class LoudnessAnalyzer(Analyzer):
    id = "loudness"
    supported_kinds = ("loudness",)
    required_capabilities = ("ffprobe", "ffmpeg", "filter:ebur128")

    def plan(self, ctx, kind, parameters):
        return [PROBE_OP, {"executable": "ffmpeg", "purpose": f"ebur128 (peak=true) on audio stream {parameters['stream']}, null output"}]

    def analyze(self, ctx: AnalysisContext, kind: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        p = ctx.probe()
        s = select_stream(p, "audio", parameters["stream"])
        ctx.record("ffmpeg", f"ebur128 on audio stream {parameters['stream']}")
        argv = ffmpeg_null_argv(ctx.exe("ffmpeg"), ctx.input_path, "-map", f"0:a:{parameters['stream']}", "-vn", "-af", "ebur128=peak=true:framelog=quiet")
        r = run_argv(argv, ctx.timeout)
        if r.returncode != 0:
            raise AnalysisError("ANALYSIS_FAILED", "ebur128 failed", {"stderr_tail": "\n".join(r.stderr.strip().splitlines()[-5:])})
        data = parse_ebur128(r.stderr)
        data.update({"stream_index": s["index"], "stream_ordinal": parameters["stream"], "standard": "EBU R128 (ITU-R BS.1770)",
                     "measurement": "ffmpeg ebur128 filter, absolute gate -70 LUFS, true peak 4x oversampled"})
        return data
