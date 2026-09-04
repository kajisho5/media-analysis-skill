"""Packet timestamp scan via ffprobe (`-show_packets`). Shared by the video (CFR/VFR), integrity and timing analyzers.
Pure analysis functions take the parsed packet list so they are unit-testable without ffprobe."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .errors import AnalysisError
from .probe import to_float, to_int
from .runner import ffprobe_argv, run_argv

PACKET_ENTRIES = "packet=stream_index,pts_time,dts_time,duration_time,flags"


def run_packets(exe: str, path: str, timeout: Optional[float], stream_spec: Optional[str] = None) -> List[Dict[str, Any]]:
    opts = ["-print_format", "compact=p=0:nk=1", "-show_entries", PACKET_ENTRIES]
    if stream_spec:
        opts = ["-select_streams", stream_spec] + opts
    r = run_argv(ffprobe_argv(exe, path, *opts), timeout)
    if r.returncode != 0:
        raise AnalysisError("ANALYSIS_FAILED", "ffprobe packet scan failed", {"stderr_tail": "\n".join(r.stderr.strip().splitlines()[-5:])})
    return parse_packets(r.stdout)


def parse_packets(text: str) -> List[Dict[str, Any]]:
    out = []
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 5:
            continue
        idx = to_int(parts[0])
        if idx is None:
            continue
        out.append({"stream_index": idx, "pts": to_float(parts[1]), "dts": to_float(parts[2]), "duration": to_float(parts[3]), "flags": parts[4]})
    return out


def by_stream(packets: List[Dict[str, Any]]) -> Dict[int, List[Dict[str, Any]]]:
    groups: Dict[int, List[Dict[str, Any]]] = {}
    for p in packets:
        groups.setdefault(p["stream_index"], []).append(p)
    return groups


def timestamp_report(pkts: List[Dict[str, Any]], gap_factor: float) -> Dict[str, Any]:
    """Facts about one stream's packet timestamps (in decode order as ffprobe emits them):
    dts monotonicity, pts gaps larger than gap_factor x median interval, negative / missing timestamps."""
    dts = [p["dts"] for p in pkts]
    pts = [p["pts"] for p in pkts]
    missing_pts = sum(1 for v in pts if v is None)
    missing_dts = sum(1 for v in dts if v is None)
    dts_known = [v for v in dts if v is not None]
    non_monotonic_dts = sum(1 for a, b in zip(dts_known, dts_known[1:]) if b < a)
    pts_sorted = sorted(v for v in pts if v is not None)
    intervals = [round(b - a, 6) for a, b in zip(pts_sorted, pts_sorted[1:])]
    median = _median(intervals)
    gaps: List[Dict[str, float]] = []
    duplicates = 0
    if median and median > 0:
        for a, b in zip(pts_sorted, pts_sorted[1:]):
            d = b - a
            if d <= 0:
                duplicates += 1
            elif d > gap_factor * median:
                gaps.append({"at": round(a, 6), "next": round(b, 6), "gap": round(d, 6), "expected": round(median, 6)})
    return {
        "packet_count": len(pkts),
        "first_pts": pts_sorted[0] if pts_sorted else None,
        "last_pts": pts_sorted[-1] if pts_sorted else None,
        "first_dts": dts_known[0] if dts_known else None,
        "missing_pts": missing_pts,
        "missing_dts": missing_dts,
        "non_monotonic_dts": non_monotonic_dts,
        "negative_pts": sum(1 for v in pts_sorted if v < 0),
        "duplicate_pts": duplicates,
        "median_interval": round(median, 6) if median is not None else None,
        "min_interval": round(min(intervals), 6) if intervals else None,
        "max_interval": round(max(intervals), 6) if intervals else None,
        "gaps": gaps[:100],
        "gap_count": len(gaps),
        "keyframes": sum(1 for p in pkts if p["flags"] and p["flags"].startswith("K")),
    }


def frame_rate_mode(pkts: List[Dict[str, Any]], tolerance: float = 0.0015) -> Dict[str, Any]:
    """CFR / VFR from measured presentation intervals. 'constant' when every interval equals the median within
    `tolerance` seconds (one 90 kHz tick is ~11 us; 1.5 ms absorbs container rounding); 'variable' when any interval
    deviates more; 'unknown' when fewer than 3 usable timestamps exist."""
    pts = sorted(p["pts"] for p in pkts if p["pts"] is not None)
    intervals = [b - a for a, b in zip(pts, pts[1:])]
    if len(intervals) < 2:
        return {"mode": "unknown", "basis": "fewer than 3 timestamped packets", "packets": len(pkts)}
    med = _median(intervals) or 0.0
    deviating = sum(1 for d in intervals if abs(d - med) > tolerance)
    if deviating == 0:
        return {"mode": "constant", "basis": f"all {len(intervals)} presentation intervals equal within {tolerance}s", "packets": len(pkts),
                "measured_fps": round(1.0 / med, 3) if med > 0 else None}
    return {"mode": "variable", "basis": f"{deviating} of {len(intervals)} presentation intervals deviate from the median by more than {tolerance}s",
            "packets": len(pkts), "measured_fps": round(1.0 / med, 3) if med > 0 else None,
            "min_interval": round(min(intervals), 6), "max_interval": round(max(intervals), 6)}


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0
