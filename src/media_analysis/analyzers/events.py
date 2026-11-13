"""audio_events: applause and laughter in one audio stream, as time ranges with a score (ADR-036).

Measured by an audio-event classifier (YAMNet, Apache-2.0 code and weights: 521 AudioSet classes from 0.975 s
log-mel patches) run on the CPU through onnxruntime. The converted model ships inside this package
(`media_analysis/models/yamnet_patches.onnx`, built by `tools/export_yamnet_onnx.py`), so nothing is downloaded at
run time; onnxruntime and numpy are the optional extra `events` (`pip install 'media-analysis-skill[events]'`).
Without them the kind is `status: UNKNOWN` with no spans: applause is never guessed from level or noise.

Method (fixed, documented in docs/tools.md):
  audio       ffmpeg decodes the stream (or one channel) to 16 kHz mono float; 0.4875 s of zeros are added at both
              ends so the first patch is centred on t = 0
  features    the reference YAMNet features: 25 ms periodic-Hann frames every 10 ms, 512-point FFT magnitude,
              64 HTK-mel bands 125-7500 Hz, log(mel + 0.001); a patch is 96 frames (0.975 s of audio)
  scores      one patch every `hop` seconds (rounded to 10 ms), run in fixed batches of 32 patches; patch i is centred
              on i * hop. An event's score is the probability that any of its classes is present,
              1 - prod(1 - p_class) (applause: Applause, Clapping; laughter: Laughter, Baby laughter, Giggle, Snicker,
              Belly laugh, Chuckle / chortle)
  spans       a span opens at a patch at or above the event's threshold and stays open while patches are at or above
              half of it (hysteresis); a dip below that longer than `merge_gap` closes it; spans are the patch
              centres +/- hop / 2, clamped to the audio; spans closer than `merge_gap` are merged and spans shorter
              than `min_duration` are dropped. Each span carries the mean and the maximum score of its patches.
Measurement only: whether a span is a good clip is the caller's decision."""
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
STFT_WINDOW = 400                  # 25 ms
STFT_HOP = 160                     # 10 ms
FFT_LENGTH = 512
MEL_BANDS = 64
MEL_MIN_HZ = 125.0
MEL_MAX_HZ = 7500.0
LOG_OFFSET = 0.001
PATCH_FRAMES = 96
PATCH_SECONDS = ((PATCH_FRAMES - 1) * STFT_HOP + STFT_WINDOW) / SAMPLE_RATE      # 0.975 s of audio per patch
EDGE_PAD = int(round(PATCH_SECONDS / 2 * SAMPLE_RATE))                           # 7800 samples: patch 0 centred on t = 0
BATCH = 32                         # fixed batch size: the same patches give the same scores whatever the file length
CHUNK_SAMPLES = SAMPLE_RATE * 10   # decoded audio is consumed 10 s at a time, so memory does not grow with the file
MODEL_FILE = "yamnet_patches.onnx"
MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
INSTALL_HINT = "pip install 'media-analysis-skill[events]'"

# AudioSet class indices of the model's 521 outputs (yamnet_class_map.csv), per measured event
EVENT_CLASSES: Dict[str, Tuple[Tuple[int, str], ...]] = {
    "applause": ((62, "Applause"), (58, "Clapping")),
    "laughter": ((13, "Laughter"), (14, "Baby laughter"), (15, "Giggle"), (16, "Snicker"), (17, "Belly laugh"),
                 (18, "Chuckle, chortle")),
}
EVENTS = tuple(EVENT_CLASSES)


def model_path() -> str:
    return os.path.join(MODEL_DIR, MODEL_FILE)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def events_status() -> Dict[str, Any]:
    """Whether the optional extra is usable here: {available, missing: [...], detail, runtime, model}. Finds the
    modules without importing them (an import can print to stderr, and doctor / conformance keep stderr clean)."""
    missing: List[str] = []
    runtime = None
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
    detail = "" if not missing else f"optional extra 'events' not installed ({', '.join(missing)} missing): {INSTALL_HINT}"
    return {"available": not missing, "missing": missing, "detail": detail, "runtime": runtime, "model": MODEL_FILE}


def hz_to_mel(np, f):
    return 1127.0 * np.log1p(f / 700.0)


def mel_matrix(np):
    """[257, 64] linear-to-mel weights: triangular HTK-mel filters, the DC bin weighted 0 (the reference features')."""
    bins = FFT_LENGTH // 2 + 1
    linear = np.linspace(0.0, SAMPLE_RATE / 2.0, bins)[1:]
    spec_mel = hz_to_mel(np, linear)[:, None]
    edges = np.linspace(hz_to_mel(np, MEL_MIN_HZ), hz_to_mel(np, MEL_MAX_HZ), MEL_BANDS + 2)
    lower, centre, upper = edges[:-2], edges[1:-1], edges[2:]
    w = np.maximum(0.0, np.minimum((spec_mel - lower) / (centre - lower), (upper - spec_mel) / (upper - centre)))
    return np.pad(w, [[1, 0], [0, 0]]).astype(np.float32)


class LogMel:
    """Streaming log-mel frames: feed float32 samples in order, get every complete 25 ms / 10 ms frame once."""

    def __init__(self, np):
        self.np = np
        self.mel = mel_matrix(np)
        self.window = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(STFT_WINDOW) / STFT_WINDOW)).astype(np.float32)
        self.buf = np.zeros(0, dtype=np.float32)

    def push(self, samples):
        np = self.np
        self.buf = np.concatenate([self.buf, samples.astype(np.float32)])
        n = 0 if len(self.buf) < STFT_WINDOW else 1 + (len(self.buf) - STFT_WINDOW) // STFT_HOP
        if n == 0:
            return np.zeros((0, MEL_BANDS), dtype=np.float32)
        idx = np.arange(STFT_WINDOW)[None, :] + STFT_HOP * np.arange(n)[:, None]
        mag = np.abs(np.fft.rfft(self.buf[idx] * self.window, FFT_LENGTH, axis=1)).astype(np.float32)
        self.buf = self.buf[n * STFT_HOP:]
        return np.log(mag @ self.mel + LOG_OFFSET).astype(np.float32)


class YamnetScores:
    """Event scores from the shipped model: push log-mel frames, collect one score per patch and event. Each batch's
    521 class scores are reduced to the events' scores at once, so memory per patch is one float per event."""

    def __init__(self, path: str, hop_frames: int):
        import numpy as np
        import onnxruntime
        self.np = np
        onnxruntime.set_default_logger_severity(3)      # errors only: warnings would land on the CLI's stderr
        opts = onnxruntime.SessionOptions()
        opts.intra_op_num_threads = 1          # one thread: the same input gives the same scores run after run
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3
        self.session = onnxruntime.InferenceSession(path, sess_options=opts, providers=["CPUExecutionProvider"])
        self.runtime = f"onnxruntime {onnxruntime.__version__}"
        self.hop = hop_frames
        self.frames = np.zeros((0, MEL_BANDS), dtype=np.float32)
        self.pending: List[Any] = []
        self.scores: Dict[str, List[float]] = {e: [] for e in EVENTS}

    def push(self, frames) -> None:
        np = self.np
        self.frames = np.concatenate([self.frames, frames])
        while len(self.frames) >= PATCH_FRAMES:
            self.pending.append(self.frames[:PATCH_FRAMES])
            self.frames = self.frames[self.hop:]
            if len(self.pending) == BATCH:
                self._run()

    def _run(self) -> None:
        np = self.np
        n = len(self.pending)
        batch = np.stack(self.pending + [np.zeros((PATCH_FRAMES, MEL_BANDS), dtype=np.float32)] * (BATCH - n))
        out = self.session.run(None, {"patches": batch})[0]
        for event, cls in EVENT_CLASSES.items():
            self.scores[event].extend(event_scores(np, out[:n], [i for i, _ in cls]))
        self.pending = []

    def finish(self):
        if self.pending:
            self._run()
        return self.scores


def event_scores(np, scores: Sequence[Any], classes: Sequence[int]) -> List[float]:
    """Per patch: probability that any of `classes` is present, 1 - prod(1 - p)."""
    if not len(scores):
        return []
    s = np.asarray(scores, dtype=np.float64)[:, list(classes)]
    return [float(v) for v in 1.0 - np.prod(1.0 - s, axis=1)]


def spans(values: Sequence[float], hop: float, threshold: float, min_duration: float, merge_gap: float,
          total: float) -> List[Dict[str, Any]]:
    """Spans from per-patch scores (pure function, no model). Patch i is centred on i * hop."""
    release = threshold / 2.0
    runs: List[Tuple[int, int]] = []
    start: Optional[int] = None
    last = 0
    for i, v in enumerate(values):
        if start is None:
            if v >= threshold:
                start, last = i, i
            continue
        if v >= release:
            last = i
        elif (i - last) * hop > merge_gap + 1e-9:
            runs.append((start, last))
            start = None
    if start is not None:
        runs.append((start, last))
    merged: List[List[Any]] = []
    for a, b in runs:
        s, e = max(0.0, a * hop - hop / 2), min(total, b * hop + hop / 2)
        if merged and s - merged[-1][1] <= merge_gap + 1e-9:
            merged[-1][1] = max(merged[-1][1], e)
            merged[-1][2] = b
            continue
        merged.append([s, e, b, a])
    out = []
    for s, e, b, a in merged:
        if e - s < min_duration - 1e-9:
            continue
        window = list(values[a:b + 1])
        out.append({"start": round(s, 4), "end": round(e, 4), "duration": round(e - s, 4),
                    "score": round(sum(window) / len(window), 4), "max_score": round(max(window), 4)})
    return out


def summarise(event: str, values: Sequence[float], hop: float, threshold: float, min_duration: float, merge_gap: float,
              total: float) -> Dict[str, Any]:
    found = spans(values, hop, threshold, min_duration, merge_gap, total)
    return {"spans": found, "span_count": len(found), "seconds": round(sum(s["duration"] for s in found), 4),
            "threshold": threshold, "release_threshold": round(threshold / 2.0, 4),
            "max_patch_score": round(max(values), 4) if len(values) else None,
            "classes": [{"index": i, "name": n} for i, n in EVENT_CLASSES[event]]}


def unknown(reason: str, missing: List[str]) -> Dict[str, Any]:
    return {"status": "UNKNOWN", "reason": reason, "missing": missing, "install": INSTALL_HINT,
            **{e: None for e in EVENTS}, "analysed_seconds": None, "patch_count": None}


class EventsAnalyzer(Analyzer):
    id = "events"
    algorithm_version = "1"
    supported_kinds = ("audio_events",)
    required_capabilities = ("ffprobe", "ffmpeg")

    def plan(self, ctx, kind, parameters):
        st = events_status()
        which = "all channels downmixed" if parameters["channel"] < 0 else f"channel {parameters['channel']}"
        ops = [PROBE_OP]
        if st["available"]:
            # the model runs in this process (onnxruntime), not as a child process, so it is not an operation of its own
            ops.append({"executable": "ffmpeg", "purpose": f"decode audio stream {parameters['stream']} ({which}) to 16 kHz float, piped "
                                                           f"to the audio-event classifier {MODEL_FILE} (onnxruntime, CPU)"})
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
        hop_frames = max(1, int(round(parameters["hop"] * SAMPLE_RATE / STFT_HOP)))
        hop = hop_frames * STFT_HOP / SAMPLE_RATE
        common = {"stream_index": s["index"], "stream_ordinal": parameters["stream"], "channel": channel, "channels": channels,
                  "hop": hop, "patch_seconds": PATCH_SECONDS}
        st = events_status()
        if not st["available"]:
            return {**unknown(st["detail"], st["missing"]), **common, "engine": None}
        try:
            model = YamnetScores(model_path(), hop_frames)
        except Exception as e:     # a broken runtime install is an environment problem, reported as UNKNOWN, never a guess
            return {**unknown(f"audio-event model could not be loaded: {type(e).__name__}: {e}"[:300], ["working onnxruntime"]), **common,
                    "engine": None}
        np = model.np
        feats = LogMel(np)
        chain = ([] if channel < 0 else [f"pan=mono|c0=c{channel}"]) + [f"aresample={SAMPLE_RATE}"]
        argv = [ctx.exe("ffmpeg"), *FF_INPUT_PREFIX, "-loglevel", "error", "-i", ctx.input_path, "-map", f"0:a:{parameters['stream']}",
                "-vn", "-sn", "-dn", "-af", ",".join(chain), "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-acodec", "pcm_f32le", "-"]
        ctx.record("ffmpeg", f"decode audio stream {parameters['stream']} to 16 kHz mono float for the audio-event classifier")
        decoded = [0]
        pad = np.zeros(EDGE_PAD, dtype=np.float32)
        model.push(feats.push(pad))

        def feed(block: bytes) -> None:
            x = np.frombuffer(block[:len(block) - len(block) % 4], dtype="<f4")
            decoded[0] += len(x)
            model.push(feats.push(x))

        r = stream_records(argv, ctx.timeout, CHUNK_SAMPLES * 4, feed, keep_tail=True)
        if r.returncode != 0:
            raise AnalysisError("ANALYSIS_FAILED", "audio decode for audio events failed", {"stderr_tail": "\n".join(r.stderr.strip().splitlines()[-5:])})
        analysed = decoded[0] / SAMPLE_RATE
        model.push(feats.push(pad))
        scores = model.finish()
        # patches whose centre lies beyond the audio (they only exist because of the end padding) are not reported
        n = min(len(scores[EVENTS[0]]), int(analysed / hop + 1e-9) + 1) if decoded[0] else 0
        data: Dict[str, Any] = {"status": "MEASURED", **common, "analysed_seconds": round(analysed, 4), "patch_count": n}
        for event in EVENTS:
            values = scores[event][:n]
            data[event] = summarise(event, values, hop, parameters[f"{event}_threshold"], parameters["min_duration"],
                                    parameters["merge_gap"], analysed)
        data["engine"] = {"name": "YAMNet", "model": MODEL_FILE, "model_sha256": _sha256(model_path()), "runtime": model.runtime,
                          "device": "cpu", "licence": "Apache-2.0", "classes": 521}
        data["method"] = ("audio-event classifier scores on 0.975 s log-mel patches every hop seconds (patch i centred on i * hop); "
                          "event score = 1 - prod(1 - p) over the event's classes; spans open at threshold, stay open down to "
                          "threshold / 2, merged across gaps up to merge_gap, shorter than min_duration dropped (ADR-036)")
        return data

