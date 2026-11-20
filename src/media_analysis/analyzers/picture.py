"""picture_quality (ADR-040): per-window facts about the picture, measured and never judged.

- sharpness: the median of ffmpeg's `blurdetect` estimate on frames scaled to `width` pixels (higher is blurrier); a frame
  with no edges (a black frame) gives no value, never 0.
- exposure: the mean luma and its 10th / 90th percentiles from ffmpeg's `signalstats` (YAVG, YLOW, YHIGH; 8-bit codes).
- camera motion: the global shift between successive frames sampled at `motion_rate` and scaled to `motion_width`. Each
  frame is split into 4 x 3 blocks; the column and row projection profiles of each block are matched (mean absolute
  difference, shifts up to 12 % of the width, refined to a sub-pixel shift by a parabola through the best three), and the
  median over the blocks with texture is the frame's shift, so a subject moving through part of the frame is not camera
  motion as long as most blocks show the background. `motion_agreement` (the share of textured blocks within 1 px of the median)
  says how far the blocks agreed: when a subject covers most of the frame the median can follow it. The camera path is the running sum of the shifts; jitter is the RMS distance between the path and its centred
  moving average over `smooth` seconds (shake, not a pan), speed the mean shift per second; both in percent of the width.

No threshold is applied: whether a window is soft, dark or shaky is the caller's decision."""
from __future__ import annotations

import math
import re
import statistics
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..errors import AnalysisError
from ..probe import container_duration, select_stream
from ..runner import FF_INPUT_PREFIX, ffmpeg_null_argv, run_argv, stream_records
from .base import PROBE_OP, AnalysisContext, Analyzer

BLOCKS = (4, 3)               # columns x rows of the motion grid
MAX_SHIFT = 0.12              # search range, a fraction of the width (and of the height, vertically)
AGREE_PX = 1.0               # a block agrees with the frame's shift when its own shift is within this many pixels of it
MIN_TEXTURE = 1.0             # mean absolute deviation of a block profile (8-bit codes) below which the block has no texture

_FRAME = re.compile(r"^frame:(\d+)\s+pts:(-?\d+)\s+pts_time:(-?[0-9.]+)")
_KV = re.compile(r"^lavfi\.(blur|signalstats\.YAVG|signalstats\.YLOW|signalstats\.YHIGH)=(\S+)")


def parse_frame_stats(stdout: str) -> List[Dict[str, Optional[float]]]:
    """`blurdetect,signalstats,metadata=print` output -> one record per frame: time, blur (None when not a number), luma
    mean / p10 / p90."""
    out: List[Dict[str, Optional[float]]] = []
    cur: Optional[Dict[str, Optional[float]]] = None
    for ln in stdout.splitlines():
        m = _FRAME.match(ln)
        if m:
            cur = {"time": float(m.group(3)), "blur": None, "luma_mean": None, "luma_p10": None, "luma_p90": None}
            out.append(cur)
            continue
        k = _KV.match(ln.strip())
        if k and cur is not None:
            try:
                v = float(k.group(2))
            except ValueError:
                continue
            if math.isnan(v) or math.isinf(v):
                continue
            cur[{"blur": "blur", "signalstats.YAVG": "luma_mean", "signalstats.YLOW": "luma_p10", "signalstats.YHIGH": "luma_p90"}[k.group(1)]] = v
    return out


# ---- camera motion ---------------------------------------------------------------------------------------------------
def block_profiles(frame: bytes, w: int, h: int) -> List[Tuple[List[float], List[float]]]:
    """Per block of the 4 x 3 grid: (column profile, row profile), each the mean of the block's gray codes along the other axis."""
    bx, by = BLOCKS
    bw, bh = w // bx, h // by
    out = []
    for j in range(by):
        rows = [frame[(j * bh + y) * w: (j * bh + y) * w + w] for y in range(bh)]
        for i in range(bx):
            seg = [r[i * bw: i * bw + bw] for r in rows]
            cols = [sum(r[x] for r in seg) / bh for x in range(bw)]
            rws = [sum(r) / bw for r in seg]
            out.append((cols, rws))
    return out


def texture(profile: Sequence[float]) -> float:
    m = sum(profile) / len(profile)
    return sum(abs(v - m) for v in profile) / len(profile)


def profile_shift(a: Sequence[float], b: Sequence[float], max_shift: int) -> float:
    """The shift s (in samples) that best maps profile a onto b (b[i + s] ~ a[i]), by the least mean absolute difference of the
    mean-removed profiles over their overlap, refined by a parabola through the best cost and its neighbours."""
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    a0 = [v - ma for v in a]
    b0 = [v - mb for v in b]
    n = len(a0)
    s_max = max(1, min(max_shift, n // 2))
    costs: Dict[int, float] = {}
    for s in range(-s_max, s_max + 1):
        lo, hi = max(0, -s), min(n, n - s)
        costs[s] = sum(abs(a0[i] - b0[i + s]) for i in range(lo, hi)) / (hi - lo)
    best = min(sorted(costs), key=lambda s: (costs[s], abs(s)))
    if -s_max < best < s_max:
        left, mid, right = costs[best - 1], costs[best], costs[best + 1]
        d = left - 2 * mid + right
        if d > 0:
            return best + 0.5 * (left - right) / d
    return float(best)


def frame_motion(p: List[Tuple[List[float], List[float]]], q: List[Tuple[List[float], List[float]]], w: int, h: int) -> Optional[Tuple[float, float, float]]:
    """The global (dx, dy) in pixels between two frames, the median of the textured blocks' shifts, and the agreement: the share of those
    blocks whose shift is within AGREE_PX of the median (the mean over both axes). None when no block has texture. A low agreement says the
    blocks moved differently (a subject crossing much of the frame, or no single motion), so the median need not be the camera's."""
    sx = max(1, int(round(MAX_SHIFT * w)))
    sy = max(1, int(round(MAX_SHIFT * h)))
    xs = [profile_shift(a[0], b[0], sx) for a, b in zip(p, q) if texture(a[0]) >= MIN_TEXTURE and texture(b[0]) >= MIN_TEXTURE]
    ys = [profile_shift(a[1], b[1], sy) for a, b in zip(p, q) if texture(a[1]) >= MIN_TEXTURE and texture(b[1]) >= MIN_TEXTURE]
    if not xs and not ys:
        return None
    mx = statistics.median(xs) if xs else 0.0
    my = statistics.median(ys) if ys else 0.0
    shares = [sum(1 for v in vs if abs(v - m) <= AGREE_PX) / len(vs) for vs, m in ((xs, mx), (ys, my)) if vs]
    return (mx, my, sum(shares) / len(shares))


def frame_shift(p: List[Tuple[List[float], List[float]]], q: List[Tuple[List[float], List[float]]], w: int, h: int) -> Optional[Tuple[float, float]]:
    """The global (dx, dy) in pixels between two frames (see frame_motion); None when no block has texture."""
    m = frame_motion(p, q, w, h)
    return None if m is None else (m[0], m[1])


def camera_path(shifts: Sequence[Optional[Tuple[float, float]]]) -> List[Tuple[float, float]]:
    """Running sum of the frame shifts (a frame pair with no texture adds nothing); one point per frame."""
    x = y = 0.0
    path = [(0.0, 0.0)]
    for s in shifts:
        if s is not None:
            x, y = x + s[0], y + s[1]
        path.append((x, y))
    return path


def jitter_series(path: Sequence[Tuple[float, float]], half: int) -> List[float]:
    """Distance of each point of the path from its centred moving average (window 2 * half + 1, shrunk at the ends)."""
    out = []
    n = len(path)
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        mx = sum(p[0] for p in path[lo:hi]) / (hi - lo)
        my = sum(p[1] for p in path[lo:hi]) / (hi - lo)
        out.append(math.hypot(path[i][0] - mx, path[i][1] - my))
    return out


def _r(v: Optional[float], nd: int = 3) -> Optional[float]:
    return None if v is None else round(v, nd)


def summarize_windows(duration: float, window: float, stats: List[Dict[str, Optional[float]]], motion_times: List[float],
                      jitter: List[float], speed: List[Optional[float]], width: int,
                      agreement: Optional[List[Optional[float]]] = None) -> List[Dict[str, Any]]:
    """One record per window [k * window, (k + 1) * window) clipped to the duration: the medians / means of the frames in it."""
    n = max(1, int(math.ceil(duration / window - 1e-9))) if duration > 0 else 0
    out = []
    for k in range(n):
        s, e = k * window, min(duration, (k + 1) * window)
        fr = [f for f in stats if s <= f["time"] < e]
        blur = [f["blur"] for f in fr if f["blur"] is not None]
        lm = [f["luma_mean"] for f in fr if f["luma_mean"] is not None]
        lo = [f["luma_p10"] for f in fr if f["luma_p10"] is not None]
        hi = [f["luma_p90"] for f in fr if f["luma_p90"] is not None]
        idx = [i for i, t in enumerate(motion_times) if s <= t < e]
        jit = [jitter[i] for i in idx if i < len(jitter)]
        spd = [speed[i] for i in idx if i < len(speed) and speed[i] is not None]
        agr = [agreement[i] for i in idx if agreement is not None and i < len(agreement) and agreement[i] is not None]
        out.append({"start": round(s, 3), "end": round(e, 3), "frames": len(fr),
                    "blur": _r(statistics.median(blur)) if blur else None,
                    "luma_mean": _r(sum(lm) / len(lm), 2) if lm else None,
                    "luma_p10": _r(statistics.median(lo), 1) if lo else None,
                    "luma_p90": _r(statistics.median(hi), 1) if hi else None,
                    "jitter": _r(math.sqrt(sum(j * j for j in jit) / len(jit)) / width * 100.0) if jit else None,
                    "speed": _r(sum(spd) / len(spd) / width * 100.0, 2) if spd else None,
                    "motion_agreement": _r(sum(agr) / len(agr), 2) if agr else None})
    return out


class PictureAnalyzer(Analyzer):
    id = "picture"
    algorithm_version = "1"
    supported_kinds = ("picture_quality",)
    required_capabilities = ("ffprobe", "ffmpeg", "filter:blurdetect", "filter:signalstats")

    def plan(self, ctx, kind, parameters):
        p = parameters
        return [PROBE_OP,
                {"executable": "ffmpeg", "purpose": f"blurdetect + signalstats on video stream {p['stream']} at {p['sample_rate']:g} fps, {p['width']} px wide, metadata to stdout"},
                {"executable": "ffmpeg", "purpose": f"gray frames of video stream {p['stream']} at {p['motion_rate']:g} fps, {p['motion_width']} px wide, to stdout (camera motion)"}]

    def analyze(self, ctx: AnalysisContext, kind: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        p = parameters
        probe = ctx.probe()
        s = select_stream(probe, "video", p["stream"])
        duration = s["duration"] if s["duration"] is not None else container_duration(probe)
        if not duration or duration <= 0:
            raise AnalysisError("ANALYSIS_FAILED", "the video stream has no duration")
        vmap = ["-map", f"0:V:{p['stream']}", "-an", "-sn"]
        # 1. sharpness and exposure per sampled frame
        ctx.record("ffmpeg", f"blurdetect + signalstats on video stream {p['stream']}")
        argv = ffmpeg_null_argv(ctx.exe("ffmpeg"), ctx.input_path, *vmap, "-vf",
                                f"fps={p['sample_rate']:g},scale={p['width']}:-2,format=yuv420p,blurdetect,signalstats,metadata=print:file=-",
                                loglevel="error")
        r = run_argv(argv, ctx.timeout)
        if r.returncode != 0:
            raise AnalysisError("ANALYSIS_FAILED", "blurdetect / signalstats failed", {"stderr_tail": "\n".join(r.stderr.strip().splitlines()[-5:])})
        stats = parse_frame_stats(r.stdout)
        # 2. camera motion from small gray frames
        w = int(p["motion_width"]) // 2 * 2
        h = max(BLOCKS[1] * 2, int(round(w * (s.get("height") or 9) / (s.get("width") or 16))) // 2 * 2)
        profiles: List[List[Tuple[List[float], List[float]]]] = []
        shifts: List[Optional[Tuple[float, float]]] = []
        agree: List[Optional[float]] = [None]

        def on_frame(buf: bytes) -> None:
            prof = block_profiles(buf, w, h)
            if profiles:
                m = frame_motion(profiles[-1], prof, w, h)
                shifts.append(None if m is None else (m[0], m[1]))
                agree.append(None if m is None else m[2])
                profiles[-1] = prof
            else:
                profiles.append(prof)

        ctx.record("ffmpeg", f"gray frames of video stream {p['stream']} for camera motion")
        argv = [ctx.exe("ffmpeg"), *FF_INPUT_PREFIX, "-loglevel", "error", "-i", ctx.input_path, *vmap, "-vf",
                f"fps={p['motion_rate']:g},scale={w}:{h}:flags=area,format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
        r2 = stream_records(argv, ctx.timeout, w * h, on_frame)
        if r2.returncode != 0:
            raise AnalysisError("ANALYSIS_FAILED", "frame decode for camera motion failed", {"stderr_tail": "\n".join(r2.stderr.strip().splitlines()[-5:])})
        rate = float(p["motion_rate"])
        path = camera_path(shifts)
        half = max(1, int(round(float(p["smooth"]) * rate / 2)))
        jitter = jitter_series(path, half)
        speed: List[Optional[float]] = [None] + [None if sh is None else math.hypot(*sh) * rate for sh in shifts]
        times = [i / rate for i in range(len(path))]
        windows = summarize_windows(float(duration), float(p["window"]), stats, times, jitter, speed, w, agree)
        blur_all = [f["blur"] for f in stats if f["blur"] is not None]
        jit_all = jitter
        return {"stream_index": s["index"], "stream_ordinal": p["stream"], "duration": duration,
                "method": {"sharpness": f"ffmpeg blurdetect, median per window, frames scaled to {p['width']} px wide (higher is blurrier)",
                           "exposure": "ffmpeg signalstats YAVG (mean) / YLOW (10th percentile) / YHIGH (90th percentile), 8-bit luma codes",
                           "motion": f"block-median projection shift of {BLOCKS[0]}x{BLOCKS[1]} blocks between frames at {rate:g} fps, {w}x{h}; "
                                     f"jitter = RMS distance of the camera path from its {float(p['smooth']):g} s centred moving average, "
                                     "speed = mean shift per second, both in percent of the frame width; motion_agreement = mean share of textured blocks within "
                                     f"{AGREE_PX:g} px of the median shift (low: the blocks moved differently, the median need not be the camera)"},
                "parameters": {k: p[k] for k in ("window", "sample_rate", "width", "motion_rate", "motion_width", "smooth")},
                "sampled_frames": len(stats), "motion_frames": len(path) if profiles else 0,
                "windows": windows, "window_count": len(windows),
                "summary": {"blur_median": _r(statistics.median(blur_all)) if blur_all else None,
                            "luma_mean": _r(sum(f["luma_mean"] for f in stats if f["luma_mean"] is not None) / max(1, sum(1 for f in stats if f["luma_mean"] is not None)), 2)
                            if any(f["luma_mean"] is not None for f in stats) else None,
                            "jitter": _r(math.sqrt(sum(j * j for j in jit_all) / len(jit_all)) / w * 100.0) if len(jit_all) > 1 else None,
                            "frames_without_edges": sum(1 for f in stats if f["blur"] is None),
                            "frame_pairs_without_texture": sum(1 for sh in shifts if sh is None)}}
