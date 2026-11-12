"""speech_regions: where someone speaks in one audio stream, as time ranges with a speech probability, the speech share
and the longest pause (ADR-035).

Measured by a voice-activity model (silero-vad, MIT code and weights) run on the CPU through onnxruntime. The model
file ships inside this package (`media_analysis/models/`), so nothing is downloaded at run time; onnxruntime and
numpy are the optional extra `vad` (`pip install 'media-analysis-skill[vad]'`). Without them the kind is reported
`status: UNKNOWN` with no regions: speech is never guessed from silence or level (that is what `silence` measures).

The model gives one probability per 32 ms window (512 samples at 16 kHz). Regions are the runs at or above
`threshold`, closed at the first window below `threshold - 0.15` once `min_silence_duration` has passed from it
without a return to `threshold` (the engine's own segmentation rule); runs
shorter than `min_speech_duration` are dropped and each region is widened by `speech_pad`. Measurement only: which
pause is a good cut point is the caller's decision."""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..errors import AnalysisError
from ..probe import select_stream
from ..runner import FF_INPUT_PREFIX, stream_records
from .base import PROBE_OP, AnalysisContext, Analyzer

SAMPLE_RATE = 16000
WINDOW = 512                       # samples per model call at 16 kHz
CONTEXT = 64                       # samples of the previous window the model expects in front of each window
FRAME_SECONDS = WINDOW / SAMPLE_RATE
RELEASE_GAP = 0.15                 # a region ends below threshold - RELEASE_GAP (hysteresis)
MODEL_FILE = "silero_vad_16k_op15.onnx"
MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
INSTALL_HINT = "pip install 'media-analysis-skill[vad]'"


def model_path() -> str:
    return os.path.join(MODEL_DIR, MODEL_FILE)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def vad_status() -> Dict[str, Any]:
    """Whether the optional extra is usable here: {available, missing: [...], detail, runtime, model}. Imports only;
    no model is loaded."""
    missing: List[str] = []
    runtime = None
    # found, not imported: importing onnxruntime can print device-discovery warnings on stderr, and doctor /
    # conformance must keep stderr clean. A runtime that is found but cannot load is reported UNKNOWN at analysis.
    for name in ("numpy", "onnxruntime"):
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(name)
    if "onnxruntime" not in missing:
        try:
            runtime = f"onnxruntime {importlib.metadata.version('onnxruntime')}"
        except Exception:
            runtime = "onnxruntime (version unknown)"
    if not os.path.isfile(model_path()):
        missing.append(f"model file {MODEL_FILE}")
    detail = "" if not missing else f"optional extra 'vad' not installed ({', '.join(missing)} missing): {INSTALL_HINT}"
    return {"available": not missing, "missing": missing, "detail": detail, "runtime": runtime, "model": MODEL_FILE}


class SileroVad:
    """One-stream streaming VAD: feed 512-sample float32 windows in order, get one speech probability each."""

    def __init__(self, path: str):
        import numpy as np
        import onnxruntime
        self._np = np
        onnxruntime.set_default_logger_severity(3)      # errors only: warnings would land on the CLI's stderr
        opts = onnxruntime.SessionOptions()
        opts.intra_op_num_threads = 1          # one thread: the same input gives the same probabilities run after run
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3
        self.session = onnxruntime.InferenceSession(path, sess_options=opts, providers=["CPUExecutionProvider"])
        self.runtime = f"onnxruntime {onnxruntime.__version__}"
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT), dtype=np.float32)
        self._sr = np.array(SAMPLE_RATE, dtype=np.int64)

    def window(self, samples: bytes) -> float:
        np = self._np
        x = np.frombuffer(samples, dtype="<f4").astype(np.float32).reshape(1, WINDOW)
        x = np.concatenate([self._context, x], axis=1)
        out, self._state = self.session.run(None, {"input": x, "state": self._state, "sr": self._sr})
        self._context = x[:, -CONTEXT:]
        return float(out[0][0])


def segment(probs: Sequence[float], threshold: float, min_speech: float, min_silence: float, pad: float,
            frame: float = FRAME_SECONDS) -> List[Dict[str, Any]]:
    """Speech regions from per-window probabilities (pure function, no model). Each region: start / end in seconds
    (padded, clamped to the analysed length, overlapping ones merged), duration, mean and max probability of the
    windows it covers before padding."""
    release = max(0.01, threshold - RELEASE_GAP)
    n = len(probs)
    runs: List[Tuple[int, int]] = []
    start: Optional[int] = None
    low: Optional[int] = None
    for i, p in enumerate(probs):
        if start is None:
            if p >= threshold:
                start, low = i, None
            continue
        if p >= threshold:
            low = None
        elif p < release:
            if low is None:
                low = i
            if (i + 1 - low) * frame >= min_silence - 1e-9:
                runs.append((start, low))
                start, low = None, None
    if start is not None:
        runs.append((start, low if low is not None else n))
    runs = [(a, b) for a, b in runs if (b - a) * frame >= min_speech - 1e-9]
    total = n * frame
    regions: List[Dict[str, Any]] = []
    for a, b in runs:
        s, e = max(0.0, a * frame - pad), min(total, b * frame + pad)
        window = probs[a:b]
        if regions and s <= regions[-1]["_end"]:
            last = regions[-1]
            last["_end"] = max(last["_end"], e)
            last["_probs"] += list(window)
        else:
            regions.append({"_start": s, "_end": e, "_probs": list(window)})
    out = []
    for r in regions:
        ps = r["_probs"]
        out.append({"start": round(r["_start"], 4), "end": round(r["_end"], 4), "duration": round(r["_end"] - r["_start"], 4),
                    "probability": round(sum(ps) / len(ps), 4), "max_probability": round(max(ps), 4)})
    return out


def summarise(regions: List[Dict[str, Any]], analysed: float) -> Dict[str, Any]:
    speech = sum(r["duration"] for r in regions)
    pauses = [{"start": a["end"], "end": b["start"], "duration": round(b["start"] - a["end"], 4)} for a, b in zip(regions, regions[1:])]
    longest = max(pauses, key=lambda p: (p["duration"], -p["start"])) if pauses else None
    return {
        "regions": regions, "region_count": len(regions),
        "speech_seconds": round(speech, 4), "speech_share": round(speech / analysed, 4) if analysed > 0 else None,
        "pause_count": len(pauses), "longest_pause": longest,
        "leading_non_speech": regions[0]["start"] if regions else None,
        "trailing_non_speech": round(analysed - regions[-1]["end"], 4) if regions else None,
    }


def unknown(reason: str, missing: List[str]) -> Dict[str, Any]:
    return {"status": "UNKNOWN", "reason": reason, "missing": missing, "install": INSTALL_HINT,
            "regions": None, "region_count": None, "speech_seconds": None, "speech_share": None, "pause_count": None,
            "longest_pause": None, "leading_non_speech": None, "trailing_non_speech": None}


class VadAnalyzer(Analyzer):
    id = "vad"
    algorithm_version = "1"
    supported_kinds = ("speech_regions",)
    required_capabilities = ("ffprobe", "ffmpeg")

    def plan(self, ctx, kind, parameters):
        st = vad_status()
        which = "all channels downmixed" if parameters["channel"] < 0 else f"channel {parameters['channel']}"
        ops = [PROBE_OP]
        if st["available"]:
            # the model runs in this process (onnxruntime), not as a child process, so it is not an operation of its own
            ops.append({"executable": "ffmpeg", "purpose": f"decode audio stream {parameters['stream']} ({which}) to 16 kHz float, piped "
                                                           f"to the voice-activity model {MODEL_FILE} (onnxruntime, CPU)"})
        return ops

    def cacheable(self, data: Dict[str, Any]) -> bool:
        # an UNKNOWN result describes this environment, not the file: installing the extra must not hit a stale entry
        return data.get("status") != "UNKNOWN"

    def analyze(self, ctx: AnalysisContext, kind: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        p = ctx.probe()
        s = select_stream(p, "audio", parameters["stream"])
        channels = s["audio"]["channels"]
        channel = parameters["channel"]
        if channel >= 0 and channels is not None and channel >= channels:
            raise AnalysisError("INVALID_INPUT", f"channel {channel} does not exist (stream has {channels})", {"available": channels})
        common = {"stream_index": s["index"], "stream_ordinal": parameters["stream"], "channel": channel, "channels": channels}
        st = vad_status()
        if not st["available"]:
            return {**unknown(st["detail"], st["missing"]), **common, "engine": None}
        try:
            vad = SileroVad(model_path())
        except Exception as e:     # a broken runtime install is an environment problem, reported as UNKNOWN, never a guess
            return {**unknown(f"voice-activity model could not be loaded: {type(e).__name__}: {e}"[:300], ["working onnxruntime"]), **common,
                    "engine": None}
        chain = ([] if channel < 0 else [f"pan=mono|c0=c{channel}"]) + [f"aresample={SAMPLE_RATE}"]
        argv = [ctx.exe("ffmpeg"), *FF_INPUT_PREFIX, "-loglevel", "error", "-i", ctx.input_path, "-map", f"0:a:{parameters['stream']}",
                "-vn", "-sn", "-dn", "-af", ",".join(chain), "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-acodec", "pcm_f32le", "-"]
        ctx.record("ffmpeg", f"decode audio stream {parameters['stream']} to 16 kHz mono float for the voice-activity model")
        probs: List[float] = []
        r = stream_records(argv, ctx.timeout, WINDOW * 4, lambda rec: probs.append(vad.window(rec)))
        if r.returncode != 0:
            raise AnalysisError("ANALYSIS_FAILED", "audio decode for voice activity failed", {"stderr_tail": "\n".join(r.stderr.strip().splitlines()[-5:])})
        analysed = len(probs) * FRAME_SECONDS
        regions = segment(probs, parameters["threshold"], parameters["min_speech_duration"], parameters["min_silence_duration"], parameters["speech_pad"])
        data = {"status": "MEASURED", **summarise(regions, analysed), **common,
                "analysed_seconds": round(analysed, 4), "window_seconds": FRAME_SECONDS, "window_count": len(probs),
                "threshold": parameters["threshold"], "release_threshold": round(max(0.01, parameters["threshold"] - RELEASE_GAP), 4),
                "engine": {"name": "silero-vad", "model": MODEL_FILE, "model_sha256": _sha256(model_path()), "runtime": vad.runtime,
                           "device": "cpu", "licence": "MIT"},
                "method": "voice-activity model probability per 32 ms window of 16 kHz mono audio; regions = runs >= threshold, "
                          "ended after min_silence_duration below threshold - 0.15, shorter than min_speech_duration dropped, "
                          "padded by speech_pad (ADR-035)"}
        return data
