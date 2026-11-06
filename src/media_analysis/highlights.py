"""Highlight candidates from a long recording (ADR-032): spans that stand on their own, each starting and ending on a
sentence boundary of the transcript, ranked by measured signals and listed with the reasons.

    media-analysis highlights <input> --transcript <file> [--signals <file>] [--keyword W]... --json

Inputs and signals:

    transcript      segments `{start, end, text, words?: [{start, end, text}], speaker_id? | speaker?}` (the shape
                    transcription-skill writes). Sentences come from the words when there are word times, otherwise
                    from runs of segments that end on sentence-final punctuation. Speaker changes come from the
                    segment speaker labels when present.
    energy          measured here: RMS level of consecutive windows of the audio (ffmpeg astats, the same method as
                    `noise_floor`); a span's lift is its mean window level minus the recording's median.
    applause,       spans passed in by the caller (`--signals`), for example from an audio classifier the caller runs;
    laughter        this Skill does not detect them itself (roadmap step 5). A reaction counts for a span when it
                    starts inside it or within `reaction_window` seconds after its end.
    keywords        caller-supplied words counted in the span's text (case-insensitive, whole words).

This lists candidates only. Nothing is cut, rendered or written except the JSON document; which candidate to use is
the caller's decision. Scores are a fixed, documented weighting of the measured signals, not a judgement of quality."""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Sequence

from .analyzers.base import AnalysisContext
from .analyzers.noise import parse_window_levels
from .capabilities import CapabilitySet
from .errors import AnalysisError
from .probe import container_duration, select_stream
from .runner import ffmpeg_null_argv, run_argv

SCHEMA = "media-analysis/highlight-candidates@1"
WEIGHTS = {"energy": 1.0, "applause": 1.0, "laughter": 1.0, "keywords": 1.0, "speaker_edges": 0.25}
ENERGY_HALF_DB = 6.0              # energy component = lift / (lift + 6 dB): 0.5 at +6 dB, never saturating, so a span
                                  # diluted with quieter sentences always scores below the loud core it contains
_TERMINAL = re.compile(r"[.!?。！？…]+[\"'”’)\]」』）]*$")
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)


# ---- transcript -> sentences
def _num(v: Any, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise AnalysisError("INVALID_INPUT", f"transcript {what} must be a finite number", {"value": v})
    return float(v)


def _ends_sentence(text: str) -> bool:
    return bool(_TERMINAL.search(text.strip()))


def sentences_from_transcript(doc: Any) -> List[Dict[str, Any]]:
    """Sentences `{index, start, end, text, speaker, basis}` in time order. `basis` is `words` when the boundary times
    are word times, `segments` when they are segment times."""
    segs = doc.get("segments") if isinstance(doc, dict) else doc
    if not isinstance(segs, list) or not segs:
        raise AnalysisError("INVALID_INPUT", "transcript must be {segments: [...]} with at least one segment")
    rows = []
    for i, s in enumerate(segs):
        if not isinstance(s, dict) or not isinstance(s.get("text"), str):
            raise AnalysisError("INVALID_INPUT", "each transcript segment needs start, end and text", {"segment": i})
        start, end = _num(s.get("start"), "start"), _num(s.get("end"), "end")
        if end < start:
            raise AnalysisError("INVALID_INPUT", "transcript segment ends before it starts", {"segment": i})
        speaker = s.get("speaker_id", s.get("speaker"))
        words = s.get("words")
        if words is not None and not isinstance(words, list):
            raise AnalysisError("INVALID_INPUT", "transcript segment words must be a list", {"segment": i})
        rows.append({"start": start, "end": end, "text": s["text"].strip(), "speaker": speaker if isinstance(speaker, str) else None,
                     "words": words or None})
    rows.sort(key=lambda r: (r["start"], r["end"]))
    out: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None

    def close() -> None:
        nonlocal cur
        if cur is not None and cur["text"]:
            cur["text"] = " ".join(cur["text"].split())
            cur["index"] = len(out)
            out.append(cur)
        cur = None

    for r in rows:
        units = []
        if r["words"]:
            for w in r["words"]:
                if not isinstance(w, dict):
                    raise AnalysisError("INVALID_INPUT", "transcript word must be an object")
                text = w.get("text", w.get("word"))
                if not isinstance(text, str):
                    raise AnalysisError("INVALID_INPUT", "transcript word needs text")
                units.append((_num(w.get("start"), "word start"), _num(w.get("end"), "word end"), text.strip(), "words"))
        else:
            units.append((r["start"], r["end"], r["text"], "segments"))
        for start, end, text, basis in units:
            if not text:
                continue
            if cur is not None and r["speaker"] is not None and cur["speaker"] is not None and r["speaker"] != cur["speaker"]:
                close()                       # a speaker change always ends a sentence
            if cur is None:
                cur = {"start": round(start, 3), "end": round(end, 3), "text": text, "speaker": r["speaker"], "basis": basis}
            else:
                cur["end"] = round(max(cur["end"], end), 3)
                cur["text"] += " " + text
                if basis == "segments":
                    cur["basis"] = "segments"
            if _ends_sentence(text):
                close()
    close()
    return out


# ---- signals
def parse_signals(doc: Any) -> Dict[str, List[Dict[str, float]]]:
    """`{"applause": [{start, end, score?}], "laughter": [...]}` (either key optional)."""
    if not isinstance(doc, dict):
        raise AnalysisError("INVALID_INPUT", "signals must be an object {applause: [...], laughter: [...]}")
    unknown = [k for k in doc if k not in ("applause", "laughter", "source")]
    if unknown:
        raise AnalysisError("INVALID_INPUT", "unknown signal names", {"fields": unknown, "allowed": ["applause", "laughter", "source"]})
    out: Dict[str, List[Dict[str, float]]] = {}
    for name in ("applause", "laughter"):
        spans = doc.get(name)
        if spans is None:
            continue
        if not isinstance(spans, list):
            raise AnalysisError("INVALID_INPUT", f"signals.{name} must be a list of spans")
        rows = []
        for sp in spans:
            if not isinstance(sp, dict):
                raise AnalysisError("INVALID_INPUT", f"signals.{name} entries must be {{start, end}}")
            s, e = _num(sp.get("start"), f"{name} start"), _num(sp.get("end"), f"{name} end")
            if e < s:
                raise AnalysisError("INVALID_INPUT", f"signals.{name} span ends before it starts", {"span": sp})
            rows.append({"start": s, "end": e})
        out[name] = sorted(rows, key=lambda r: r["start"])
    return out


def measure_window_levels(ctx: AnalysisContext, stream: int, window: float) -> Dict[str, Any]:
    """RMS level (dBFS) of consecutive `window`-second windows of one audio stream (astats, reset every window)."""
    p = ctx.probe()
    s = select_stream(p, "audio", stream)
    rate = s["audio"]["sample_rate"]
    if not rate:
        raise AnalysisError("UNSUPPORTED_FORMAT", "audio stream declares no sample rate", {"stream_index": s["index"]})
    n = max(1, int(round(rate * window)))
    chain = [f"asetnsamples=n={n}:p=0", "astats=metadata=1:reset=1", "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level"]
    ctx.record("ffmpeg", f"astats windowed RMS on audio stream {stream}")
    argv = ffmpeg_null_argv(ctx.exe("ffmpeg"), ctx.input_path, "-map", f"0:a:{stream}", "-vn", "-sn", "-af", ",".join(chain), loglevel="repeat+info")
    r = run_argv(argv, ctx.timeout)
    if r.returncode != 0:
        raise AnalysisError("ANALYSIS_FAILED", "astats failed", {"stderr_tail": "\n".join(r.stderr.strip().splitlines()[-5:])})
    levels = parse_window_levels(r.stderr)
    if not levels:
        raise AnalysisError("ANALYSIS_FAILED", "astats reported no RMS windows")
    return {"levels": levels, "window": n / float(rate), "duration": container_duration(p), "stream_index": s["index"]}


def _median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


FLOOR_DB = -120.0        # digital silence (-inf) counts as this level in a mean


def _span_level(levels: List[float], window: float, start: float, end: float) -> Optional[float]:
    a, b = int(math.floor(start / window)), int(math.ceil(end / window))
    vals = [max(FLOOR_DB, v) for v in levels[a:b]]
    return sum(vals) / len(vals) if vals else None


def _count_keywords(text: str, keywords: List[str]) -> Dict[str, int]:
    words = [w.lower() for w in _WORDS.findall(text)]
    lowered = text.lower()
    out = {}
    for k in keywords:
        parts = [w.lower() for w in _WORDS.findall(k)]
        if not parts:
            continue
        if len(parts) == 1 and parts[0].isascii():
            n = sum(1 for w in words if w == parts[0])
        else:                                   # phrases and scripts without spaces: substring count
            n = lowered.count(k.lower().strip())
        if n:
            out[k] = n
    return out


def candidates(sentences: List[Dict[str, Any]], *, levels: Optional[List[float]], window: Optional[float],
               signals: Dict[str, List[Dict[str, float]]], keywords: List[str], min_length: float, max_length: float,
               reaction_window: float, top: int) -> List[Dict[str, Any]]:
    """Every run of whole sentences lasting [min_length, max_length] seconds is scored; the best non-overlapping runs
    are returned, highest score first (ties: earlier start)."""
    median = _median([max(FLOOR_DB, v) for v in levels]) if levels else None
    speakers = [s["speaker"] for s in sentences]
    has_speakers = any(x is not None for x in speakers)
    scored = []
    for i in range(len(sentences)):
        for j in range(i, len(sentences)):
            start, end = sentences[i]["start"], sentences[j]["end"]
            dur = end - start
            if dur > max_length:
                break
            if dur < min_length:
                continue
            comp: Dict[str, float] = {}
            reasons: List[str] = []
            if levels and window:
                lvl = _span_level(levels, window, start, end)
                lift = (lvl - median) if lvl is not None else 0.0
                comp["energy"] = round(lift / (lift + ENERGY_HALF_DB), 6) if lift > 0 else 0.0
                comp["energy_lift_db"] = round(lift, 3)
                if lift >= 1.0:
                    reasons.append(f"speech energy {lift:.1f} dB above the recording's median")
            for name in ("applause", "laughter"):
                if name not in signals:
                    continue
                hits = [sp for sp in signals[name] if start <= sp["start"] <= end + reaction_window]
                comp[name] = 1.0 if hits else 0.0
                if hits:
                    h = hits[-1]
                    where = "after the span" if h["start"] > end else "inside the span"
                    reasons.append(f"{name} at {h['start']:.1f}-{h['end']:.1f} s ({where})")
            if keywords:
                text = " ".join(s["text"] for s in sentences[i:j + 1])
                found = _count_keywords(text, keywords)
                total = sum(found.values())
                # density: keyword hits per sentence (capped at 1), so padding a span with other sentences lowers it
                comp["keywords"] = round(min(1.0, total / float(j - i + 1)), 6)
                comp["keyword_hits"] = total
                if found:
                    reasons.append("keywords: " + ", ".join(f"{k} x{n}" for k, n in sorted(found.items())))
            if has_speakers:
                # a span that begins / ends where the speaker changes (or the recording begins / ends) does not cut
                # into someone's turn
                starts_at_change = i > 0 and speakers[i - 1] != speakers[i]
                ends_at_change = j < len(sentences) - 1 and speakers[j + 1] != speakers[j]
                start_edge = starts_at_change or i == 0
                end_edge = ends_at_change or j == len(sentences) - 1
                turns = sum(1 for k in range(i + 1, j + 1) if speakers[k] != speakers[k - 1])
                comp["speaker_edges"] = 0.5 * start_edge + 0.5 * end_edge
                comp["speaker_turns_inside"] = turns
                if starts_at_change:
                    reasons.append(f"starts where the speaker changes ({speakers[i - 1]} -> {speakers[i]})")
                if ends_at_change:
                    reasons.append(f"ends where the speaker changes ({speakers[j]} -> {speakers[j + 1]})")
            score = sum(WEIGHTS[k] * comp[k] for k in WEIGHTS if k in comp)
            if not reasons:
                reasons.append("no measured signal stands out (listed for coverage)")
            scored.append({"start": start, "end": end, "duration": round(dur, 3), "score": round(score, 6), "components": comp,
                           "reasons": reasons, "first_sentence": i, "last_sentence": j, "sentence_count": j - i + 1})
    scored.sort(key=lambda c: (-c["score"], c["start"], c["end"]))
    picked: List[Dict[str, Any]] = []
    for c in scored:
        if any(c["start"] < p["end"] and p["start"] < c["end"] for p in picked):
            continue
        picked.append(c)
        if len(picked) >= top:
            break
    for rank, c in enumerate(picked, 1):
        c["rank"] = rank
        c["text"] = " ".join(s["text"] for s in sentences[c["first_sentence"]:c["last_sentence"] + 1])
    return picked


def highlight_candidates(input_path: str, caps: CapabilitySet, transcript: Any, *, signals: Optional[Dict[str, Any]] = None,
                         keywords: Optional[List[str]] = None, stream: int = 0, window: float = 0.5, min_length: float = 15.0,
                         max_length: float = 60.0, reaction_window: float = 3.0, top: int = 10,
                         timeout: Optional[float] = 600.0) -> Dict[str, Any]:
    for name, v, lo, hi in (("window", window, 0.05, 5.0), ("min_length", min_length, 1.0, 3600.0), ("max_length", max_length, 1.0, 7200.0),
                            ("reaction_window", reaction_window, 0.0, 60.0)):
        if not (lo <= v <= hi):
            raise AnalysisError("INVALID_INPUT", f"{name} out of range [{lo}, {hi}]", {"value": v})
    if max_length < min_length:
        raise AnalysisError("INVALID_INPUT", "max_length must not be shorter than min_length")
    if not (1 <= top <= 100):
        raise AnalysisError("INVALID_INPUT", "top must be in [1, 100]")
    missing = caps.missing(["ffprobe", "ffmpeg", "filter:astats"])
    if missing:
        raise AnalysisError("ANALYZER_UNAVAILABLE", "highlights needs capabilities that are missing", {"missing": missing})
    sents = sentences_from_transcript(transcript)
    sig = parse_signals(signals) if signals is not None else {}
    kws = [k for k in (keywords or []) if k.strip()]
    ctx = AnalysisContext(input_path, caps, timeout)
    energy = measure_window_levels(ctx, stream, window)
    picked = candidates(sents, levels=energy["levels"], window=energy["window"], signals=sig, keywords=kws, min_length=min_length,
                        max_length=max_length, reaction_window=reaction_window, top=top)
    not_measured = []
    for name in ("applause", "laughter"):
        if name not in sig:
            not_measured.append({"item": name, "reason": f"no {name} spans were given (--signals); this Skill does not detect {name} itself"})
    if not any(s["speaker"] for s in sents):
        not_measured.append({"item": "speaker_changes", "reason": "the transcript carries no speaker labels"})
    if not kws:
        not_measured.append({"item": "keywords", "reason": "no keywords were given"})
    return {"schema": SCHEMA, "input": input_path, "duration": energy["duration"], "audio_stream_index": energy["stream_index"],
            "sentence_count": len(sents), "sentence_boundaries": "words" if all(s["basis"] == "words" for s in sents) else "segments",
            "parameters": {"window": energy["window"], "min_length": min_length, "max_length": max_length, "reaction_window": reaction_window,
                           "top": top, "keywords": kws, "stream": stream},
            "weights": dict(WEIGHTS), "energy_half_db": ENERGY_HALF_DB,
            "signals_used": {"energy": True, "applause": "applause" in sig, "laughter": "laughter" in sig,
                             "speaker_changes": any(s["speaker"] for s in sents), "keywords": bool(kws)},
            "candidate_count": len(picked), "candidates": picked, "not_measured": not_measured, "operations": list(ctx.operations)}
