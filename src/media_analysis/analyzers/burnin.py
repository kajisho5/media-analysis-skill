"""Burned-in (open) subtitle measurement for `edit_style` (ADR-033).

Frames are sampled every `interval` seconds, scaled to an analysis width (`ANALYSIS_WIDTH`) and turned into two grey
planes by ffmpeg in one pass: the luma itself and an edge map (`edgedetect`), side by side. Everything after that is
deterministic arithmetic on those bytes, in this module:

    text rows     rows whose edge map changes from "no edge" to "edge" many times (glyph strokes give many short
                  vertical edges per row; a picture edge, a horizon or a box border gives few long ones)
    text lines    runs of text rows whose height is a plausible text size (`MIN_LINE_RATIO`..`MAX_LINE_RATIO` of the
                  frame height); within a line, the columns carrying edges are grouped into regions (gaps shorter
                  than 1.5 line heights join, like the spaces between words)
    position      a region's centre: top / middle / bottom third, left / center / right third
    text height   rows around the line in which a tenth of the region's pixels are far from the background level
                  (ink plus any outline), over the frame height; the edge-based line height when the background is
                  unknown. For real text that is cap / ascender top to descender bottom
    approx chars  region width / (0.55 x line height), the usual advance of a proportional font; an estimate
    band          the strip just above and below the line differs in level from the picture further out, and is flat:
                  a box drawn behind the text
    outline       next to the text strokes there are pixels on the other side of the background level from the ink
                  (dark rims on light text or the reverse)

This finds text-like structure; it does not read text (no OCR) and cannot tell a subtitle from a sign or a caption in
the picture. Busy, high-detail pictures (foliage, crowds, fine patterns) can produce text-like rows; the result says
what was measured and the caller decides what it means."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..errors import AnalysisError
from ..runner import FF_INPUT_PREFIX, run_argv

ANALYSIS_WIDTH = 480
EDGE_LEVEL = 64                    # edge-map bytes at or above this are edges
MIN_ROW_TRANSITIONS = 8            # a text row has at least this many separate edge runs
MIN_LINE_RATIO = 0.02              # text line height bounds, as a share of the frame height
MAX_LINE_RATIO = 0.20
JOIN_GAP_LINES = 1.5               # columns closer than this many line heights belong to one region
MIN_REGION_LINES = 1.5             # a region is at least this many line heights wide
GLYPH_ADVANCE = 0.55               # approx chars = width / (GLYPH_ADVANCE * line height)
BAND_LEVEL_DIFF = 24               # band: margin strip vs outer strip, luma difference
BAND_FLATNESS = 12                 # band: margin strip spread (p90 - p10) at most this
OUTLINE_LEVEL_DIFF = 40
OUTLINE_SHARE = 0.08
SWITCH_SHIFT_LINES = 0.5           # a region moving more than this many line heights is a new subtitle

_BINARY = bytes(255 if i >= EDGE_LEVEL else 0 for i in range(256))


def analysis_size(width: int, height: int) -> Tuple[int, int]:
    w = ANALYSIS_WIDTH if width >= ANALYSIS_WIDTH else width - (width % 2)
    h = max(2, int(round(w * height / float(width) / 2.0)) * 2)
    return w, h


def _pct(values: List[int], p: float) -> int:
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(p / 100.0 * len(s))))]


def text_lines(edges: bytes, w: int, h: int) -> List[Tuple[int, int]]:
    """(y0, y1) half-open row ranges that look like a line of text."""
    counts = [edges[y * w:(y + 1) * w].count(b"\x00\xff") for y in range(h)]
    lines: List[Tuple[int, int]] = []
    y = 0
    while y < h:
        if counts[y] < MIN_ROW_TRANSITIONS:
            y += 1
            continue
        y0 = y
        while y < h and (counts[y] >= MIN_ROW_TRANSITIONS or (y + 1 < h and counts[y + 1] >= MIN_ROW_TRANSITIONS and counts[y] > 0)):
            y += 1
        peak = max(counts[y0:y])
        # trim the soft top / bottom rows (anti-aliasing, the rim of a stroke) to the rows carrying a quarter of the peak
        while y0 < y and counts[y0] < peak / 4.0:
            y0 += 1
        y1 = y
        while y1 > y0 and counts[y1 - 1] < peak / 4.0:
            y1 -= 1
        if MIN_LINE_RATIO * h <= (y1 - y0) <= MAX_LINE_RATIO * h:
            lines.append((y0, y1))
    return lines


def line_regions(edges: bytes, w: int, y0: int, y1: int) -> List[Tuple[int, int]]:
    """(x0, x1) column ranges of one text line that carry edges, joined across word-sized gaps."""
    lh = y1 - y0
    cols = bytearray(w)
    for y in range(y0, y1):
        row = edges[y * w:(y + 1) * w]
        for x, v in enumerate(row):
            if v:
                cols[x] = 1
    regions: List[Tuple[int, int]] = []
    start: Optional[int] = None
    last = -1
    for x in range(w):
        if cols[x]:
            if start is None:
                start = x
            elif x - last > JOIN_GAP_LINES * lh:
                regions.append((start, last + 1))
                start = x
            last = x
    if start is not None:
        regions.append((start, last + 1))
    return [(a, b) for a, b in regions if (b - a) >= MIN_REGION_LINES * lh]


def _strip(luma: bytes, w: int, h: int, x0: int, x1: int, y0: int, y1: int) -> List[int]:
    y0, y1 = max(0, y0), min(h, y1)
    x0, x1 = max(0, x0), min(w, x1)
    out: List[int] = []
    for y in range(y0, y1):
        out += luma[y * w + x0:y * w + x1]
    return out


def decorations(luma: bytes, edges: bytes, w: int, h: int, box: Tuple[int, int, int, int]) -> Dict[str, Any]:
    """Band and outline of one region (x0, y0, x1, y1); None when there is not enough picture around it to tell."""
    x0, y0, x1, y1 = box
    lh = y1 - y0
    m = max(2, lh // 3)
    margin = _strip(luma, w, h, x0, x1, y0 - m, y0) + _strip(luma, w, h, x0, x1, y1, y1 + m)
    outer = _strip(luma, w, h, x0, x1, y0 - 3 * lh, y0 - 2 * lh) + _strip(luma, w, h, x0, x1, y1 + 2 * lh, y1 + 3 * lh)
    band: Optional[bool] = None
    bg: Optional[int] = None
    if margin and outer:
        mg, out = _pct(margin, 50), _pct(outer, 50)
        band = abs(mg - out) >= BAND_LEVEL_DIFF and (_pct(margin, 90) - _pct(margin, 10)) <= BAND_FLATNESS
        bg = mg
    elif margin:
        bg = _pct(margin, 50)
    outline: Optional[bool] = None
    inside = _strip(luma, w, h, x0, x1, y0, y1)
    if bg is not None and inside:
        far = [v for v in inside if abs(v - bg) >= OUTLINE_LEVEL_DIFF]
        if far:
            above = sum(1 for v in far if v > bg)
            ink_bright = above * 2 >= len(far)
            rim = sum(1 for v in far if (v < bg) == ink_bright)          # far from the background, on the other side of it
            outline = rim >= OUTLINE_SHARE * len(far)
        else:
            outline = False
    return {"band": band, "outline": outline, "background": bg}


def ink_rows(luma: bytes, w: int, h: int, box: Tuple[int, int, int, int], bg: Optional[int]) -> int:
    """Height in rows of the text measured on the picture itself: the longest run of rows, around the edge-based line,
    in which at least a tenth of the region's pixels are far from the background level (ink and outline). The edge map
    is blurred by edgedetect's smoothing, so this is the tighter figure; 0 when the background level is unknown."""
    if bg is None:
        return 0
    x0, y0, x1, y1 = box
    lh = y1 - y0
    need = max(1, (x1 - x0) // 10)
    best = run = 0
    for y in range(max(0, y0 - lh // 2), min(h, y1 + lh // 2)):
        row = luma[y * w + x0:y * w + x1]
        if sum(1 for v in row if abs(v - bg) >= OUTLINE_LEVEL_DIFF) >= need:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def frame_regions(luma: bytes, edges: bytes, w: int, h: int) -> List[Dict[str, Any]]:
    edges = edges.translate(_BINARY)
    out = []
    for y0, y1 in text_lines(edges, w, h):
        for x0, x1 in line_regions(edges, w, y0, y1):
            deco = decorations(luma, edges, w, h, (x0, y0, x1, y1))
            bg = deco.pop("background")
            rows = ink_rows(luma, w, h, (x0, y0, x1, y1), bg)
            lh = rows if rows else y1 - y0
            cy, cx = (y0 + y1) / 2.0 / h, (x0 + x1) / 2.0 / w
            out.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "height_ratio": round(lh / float(h), 6),
                        "height_basis": "pixels" if rows else "edges",
                        "width_ratio": round((x1 - x0) / float(w), 6),
                        "vertical": "top" if cy < 1 / 3 else "middle" if cy < 2 / 3 else "bottom",
                        "horizontal": "left" if cx < 1 / 3 else "center" if cx < 2 / 3 else "right",
                        "approx_chars": int(round((x1 - x0) / (GLYPH_ADVANCE * lh))), **deco})
    return out


def _same_text(a: List[Dict[str, Any]], b: List[Dict[str, Any]]) -> bool:
    if len(a) != len(b):
        return False
    for ra, rb in zip(sorted(a, key=lambda r: (r["y0"], r["x0"])), sorted(b, key=lambda r: (r["y0"], r["x0"]))):
        lh = max(1, ra["y1"] - ra["y0"])
        if max(abs(ra[k] - rb[k]) for k in ("x0", "y0", "x1", "y1")) > SWITCH_SHIFT_LINES * lh:
            return False
    return True


def summarise(frames: List[List[Dict[str, Any]]], interval: float, duration: Optional[float]) -> Dict[str, Any]:
    n = len(frames)
    with_text = [f for f in frames if f]
    switches = 0
    prev: List[Dict[str, Any]] = []
    for f in frames:
        if f and not _same_text(prev, f):
            switches += 1
        prev = f
    regions = [r for f in frames for r in f]
    vertical = {"top": 0, "middle": 0, "bottom": 0}
    horizontal = {"left": 0, "center": 0, "right": 0}
    for r in regions:
        vertical[r["vertical"]] += 1
        horizontal[r["horizontal"]] += 1
    heights = sorted(r["height_ratio"] for r in regions)
    chars = sorted(sum(r["approx_chars"] for r in f) for f in with_text)

    def share(key: str) -> Optional[float]:
        known = [r[key] for r in regions if r[key] is not None]
        return round(sum(1 for v in known if v) / len(known), 6) if known else None

    def mid(xs):
        if not xs:
            return None
        k = len(xs) // 2
        return xs[k] if len(xs) % 2 else round((xs[k - 1] + xs[k]) / 2.0, 6)

    minutes = (duration / 60.0) if duration else None
    return {
        "status": "measured", "interval": interval, "frames_sampled": n, "frames_with_text": len(with_text),
        "present": bool(with_text), "coverage_share": round(len(with_text) / n, 6) if n else None,
        "switches": switches, "switches_per_minute": round(switches / minutes, 6) if minutes else None,
        "positions": {"vertical": vertical, "horizontal": horizontal},
        "dominant_position": max(vertical, key=lambda k: (vertical[k], k == "bottom")) if regions else None,
        "text_height_ratio": {"median": mid(heights), "min": heights[0], "max": heights[-1]} if heights else None,
        "regions_per_frame": {"mean": round(len(regions) / len(with_text), 6), "max": max(len(f) for f in with_text)} if with_text else None,
        "approx_chars_per_frame": {"median": mid(chars), "max": chars[-1]} if chars else None,
        "band_share": share("band"), "outline_share": share("outline"),
    }


def measure(ffmpeg: str, input_path: str, stream_ordinal: int, width: int, height: int, interval: float, duration: Optional[float],
            timeout: Optional[float]) -> Dict[str, Any]:
    w, h = analysis_size(width, height)
    graph = (f"[0:v:{stream_ordinal}]fps=1/{interval}:round=down,scale={w}:{h}:flags=area,format=gray,split[l][e];"
             f"[e]edgedetect=low=0.08:high=0.2[g];[l][g]hstack[out]")
    argv = [ffmpeg, *FF_INPUT_PREFIX, "-loglevel", "error", "-i", input_path, "-filter_complex", graph, "-map", "[out]",
            "-an", "-sn", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    r = run_argv(argv, timeout, binary=True)
    if r.returncode != 0:
        raise AnalysisError("ANALYSIS_FAILED", "burned-in subtitle frame sampling failed", {"stderr_tail": "\n".join(r.stderr.strip().splitlines()[-5:])})
    raw = r.stdout_bytes or b""
    size = 2 * w * h
    frames = []
    for k in range(len(raw) // size):
        frame = raw[k * size:(k + 1) * size]
        luma = b"".join(frame[y * 2 * w:y * 2 * w + w] for y in range(h))
        edges = b"".join(frame[y * 2 * w + w:(y + 1) * 2 * w] for y in range(h))
        frames.append(frame_regions(luma, edges, w, h))
    out = summarise(frames, interval, duration)
    out.update({"analysis_width": w, "analysis_height": h,
                "method": "frames every interval s, edge map (ffmpeg edgedetect); rows with many separate edge runs form text lines; "
                          "no OCR, text-like structure only (ADR-033)"})
    return out
