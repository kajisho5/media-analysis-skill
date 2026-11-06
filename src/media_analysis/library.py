"""B-roll library index (ADR-031): scan a folder of the user's own clips, describe each one with this Skill's own
measurements, keep the result in one JSON index, and search it by keyword and condition.

    media-analysis library index <folder> --index <file> [--tags <file>]
    media-analysis library search --index <file> [--query WORD ...] [--tag TAG ...] [conditions]

Every clip is measured through the AnalysisEngine (media_probe, scene_detection when there is video, loudness when
there is audio), so the same path policy, capability checks, timeouts and cache apply as for `run`. Tags are of two
kinds and are never mixed up:

    measured  derived from those measurements by fixed rules (orientation, resolution class, frame-rate class,
              duration class, audio present / silent, single / multi shot, codec, file extension) - facts, not guesses
    external  passed in by the caller (`--tags`), e.g. labels an AI model produced outside this Skill; recorded with
              the fingerprint of the file they were given for and dropped when that file changes

A re-scan compares each file's size and modification time with the index and measures only new or changed files;
unchanged files are not opened, hashed or analysed again. Removed files leave the index.

The index is the only file written, and it must be inside the workspace. Clips are never modified, copied or
transcoded; `representative_time` names a frame, it does not extract one."""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import VERSION
from .engine import AnalysisEngine
from .errors import AnalysisError
from .security import PathPolicy

INDEX_SCHEMA = "media-analysis/library-index@1"
MEDIA_EXTENSIONS = (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mxf", ".mts", ".m2ts", ".ts", ".mpg", ".mpeg",
                    ".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus")
ANALYSED_KINDS = ("media_probe", "scene_detection", "loudness")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def name_words(rel_path: str) -> List[str]:
    """Lower-case words of the path relative to the library root (folders and file name, extension excluded)."""
    stem = os.path.splitext(rel_path.replace("\\", "/"))[0]
    seen: List[str] = []
    for w in _WORD.findall(stem.lower()):
        if w not in seen:
            seen.append(w)
    return seen


# ---- measured description of one clip
def _resolution_class(width: Optional[int], height: Optional[int]) -> Optional[str]:
    if not width or not height:
        return None
    short = min(width, height)
    return "uhd" if short >= 2160 else "qhd" if short >= 1440 else "fhd" if short >= 1080 else "hd" if short >= 720 else "sd"


def _orientation(width: Optional[int], height: Optional[int], rotation: Optional[int]) -> Optional[str]:
    if not width or not height:
        return None
    if rotation in (90, -90, 270, -270):
        width, height = height, width
    return "landscape" if width > height else "portrait" if height > width else "square"


def _fps_class(fps: Optional[float]) -> Optional[str]:
    if not fps:
        return None
    for nominal, exact in ((23.976, 24000 / 1001), (29.97, 30000 / 1001), (59.94, 60000 / 1001)):
        if abs(fps - exact) < 0.01:
            return f"{nominal:g}fps"
    return f"{round(fps):d}fps"


def _duration_class(duration: Optional[float]) -> Optional[str]:
    if duration is None:
        return None
    return "short" if duration < 5 else "medium" if duration < 30 else "long"


def describe(probe: Dict[str, Any], scenes: Optional[Dict[str, Any]], loudness: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Clip metadata and measured tags from three Observations' data (None when the kind does not apply)."""
    c, v, a = probe.get("container") or {}, probe.get("video"), probe.get("audio")
    duration = c.get("duration")
    clip: Dict[str, Any] = {"duration": duration, "container": c.get("format"), "size": c.get("size")}
    tags: List[str] = []
    if v:
        w, h = v.get("width"), v.get("height")
        clip["video"] = {"codec": v.get("codec"), "width": w, "height": h, "fps": v.get("fps"),
                         "orientation": _orientation(w, h, v.get("rotation")), "resolution_class": _resolution_class(w, h),
                         "fps_class": _fps_class(v.get("fps"))}
        tags += [x for x in (clip["video"]["orientation"], clip["video"]["resolution_class"], clip["video"]["fps_class"], v.get("codec")) if x]
    else:
        clip["video"] = None
    if scenes:
        sc = scenes.get("scenes") or []
        longest = max(sc, key=lambda s: (s.get("duration") or 0.0, -s["index"])) if sc else None
        clip["shots"] = {"count": scenes.get("scene_count"), "cut_count": scenes.get("cut_count"),
                         "longest_shot_seconds": longest.get("duration") if longest else None}
        # the representative frame: the middle of the longest shot (a time; no image is extracted)
        if longest and longest.get("end") is not None:
            clip["representative_time"] = round((longest["start"] + longest["end"]) / 2.0, 3)
        else:
            clip["representative_time"] = round(duration / 2.0, 3) if duration else None
        clip["scene_type"] = "single_shot" if (scenes.get("cut_count") or 0) == 0 else "multi_shot"
    else:
        clip["shots"] = None
        clip["representative_time"] = None
        clip["scene_type"] = "audio_only" if a and not v else None
    if clip["scene_type"]:
        tags.append(clip["scene_type"].replace("_", "-"))
    if a:
        silent = bool(loudness and (loudness.get("integrated_below_absolute_gate") or loudness.get("integrated_lufs") is None))
        clip["audio"] = {"codec": a.get("codec"), "channels": a.get("channels"), "sample_rate": a.get("sample_rate"),
                         "integrated_lufs": loudness.get("integrated_lufs") if loudness else None, "silent": silent}
        tags += ["has-audio", "silent-audio"] if silent else ["has-audio"]
    else:
        clip["audio"] = None
        tags.append("no-audio")
    dc = _duration_class(duration)
    if dc:
        tags.append(dc)
    clip["measured_tags"] = sorted(set(tags))
    return clip


# ---- index file
def load_index(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise AnalysisError("INVALID_INPUT", f"library index cannot be read: {e}")
    if not isinstance(doc, dict) or doc.get("schema") != INDEX_SCHEMA or not isinstance(doc.get("clips"), list):
        raise AnalysisError("INVALID_INPUT", f"not a library index (expected schema {INDEX_SCHEMA})")
    return doc


def write_index(policy: PathPolicy, path: str, doc: Dict[str, Any]) -> Path:
    target = Path(path) if os.path.isabs(path) else policy.workspace / path
    folder = policy.resolve_write_dir(str(target.parent))
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / target.name
    fd, tmp = tempfile.mkstemp(dir=str(folder), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False, allow_nan=False)
            fh.write("\n")
        os.replace(tmp, str(final))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return final


def parse_external_tags(doc: Any) -> Dict[str, Dict[str, Any]]:
    """`{"<relative path>": ["tag", ...]}` or `[{"path": ..., "tags": [...], "source": "..."}]` -> path -> {tags, source}."""
    rows: List[Dict[str, Any]] = []
    if isinstance(doc, dict):
        rows = [{"path": k, "tags": v} for k, v in doc.items()]
    elif isinstance(doc, list):
        rows = doc
    else:
        raise AnalysisError("INVALID_INPUT", "tags document must be an object or a list")
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if not isinstance(r, dict) or not isinstance(r.get("path"), str) or not isinstance(r.get("tags"), list) \
                or not all(isinstance(t, str) and t.strip() for t in r["tags"]):
            raise AnalysisError("INVALID_INPUT", "each tags entry needs a path string and a list of non-empty tag strings", {"entry": r})
        src = r.get("source")
        if src is not None and not isinstance(src, str):
            raise AnalysisError("INVALID_INPUT", "tags entry source must be a string", {"entry": r})
        out[r["path"].replace("\\", "/")] = {"tags": sorted({t.strip().lower() for t in r["tags"]}), "source": src or "caller"}
    return out


def _scan(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for f in sorted(filenames):
            if not f.startswith(".") and os.path.splitext(f)[1].lower() in MEDIA_EXTENSIONS:
                yield Path(dirpath) / f


def _analyse(engine: AnalysisEngine, path: Path, rel: str) -> Dict[str, Any]:
    probe_res = engine.analyze({"asset_id": "clip", "input": str(path), "kind": "media_probe"})
    obs = probe_res["observation"]
    probe = obs["data"]
    scenes = engine.analyze({"asset_id": "clip", "input": str(path), "kind": "scene_detection"})["observation"]["data"] if probe.get("video") else None
    loud = engine.analyze({"asset_id": "clip", "input": str(path), "kind": "loudness"})["observation"]["data"] if probe.get("audio") else None
    clip = {"path": rel, "fingerprint": obs["asset"]["fingerprint"], **describe(probe, scenes, loud)}
    ext = os.path.splitext(rel)[1].lower().lstrip(".")
    clip["measured_tags"] = sorted(set(clip["measured_tags"]) | {ext})      # the file type as the user names it (mp4, mov, wav)
    clip["name_words"] = name_words(rel)
    clip["analysed_with"] = {"kinds": [k for k, d in zip(ANALYSED_KINDS, (probe, scenes, loud)) if d is not None], "skill_version": VERSION}
    return clip


def build_index(engine: AnalysisEngine, root: str, previous: Optional[Dict[str, Any]] = None,
                external: Optional[Dict[str, Dict[str, Any]]] = None, clock=None) -> Dict[str, Any]:
    """Scan `root` and return the new index plus a scan report. Files whose size and mtime match the previous index
    are carried over without being opened; new or changed files are analysed; files that fail are reported."""
    base = Path(root).resolve()
    if not base.is_dir():
        raise AnalysisError("FILE_NOT_FOUND", f"library folder not found: {root}")
    old = {c["path"]: c for c in (previous or {}).get("clips", [])} if previous and previous.get("root") == str(base) else {}
    old_external = {k: v for k, v in ((previous or {}).get("external_tags") or {}).items()} if old else {}
    report: Dict[str, List[Any]] = {"analysed": [], "reused": [], "removed": [], "failed": [], "external_tags_dropped": []}
    clips: List[Dict[str, Any]] = []
    for p in _scan(base):
        rel = p.relative_to(base).as_posix()
        try:
            st = p.stat()
        except OSError as e:
            report["failed"].append({"path": rel, "code": "FILE_NOT_FOUND", "message": str(e)})
            continue
        prev = old.get(rel)
        if prev and prev.get("file_size") == st.st_size and prev.get("mtime_ns") == st.st_mtime_ns:
            clips.append(prev)
            report["reused"].append(rel)
            continue
        try:
            clip = _analyse(engine, p, rel)
        except AnalysisError as e:
            report["failed"].append({"path": rel, "code": e.code, "message": e.message})
            continue
        clip["file_size"], clip["mtime_ns"] = st.st_size, st.st_mtime_ns
        clips.append(clip)
        report["analysed"].append(rel)
    present = {c["path"]: c for c in clips}
    report["removed"] = sorted(set(old) - set(present))
    # external tags: keep those whose file is unchanged, add the new ones against the file as it is now
    ext: Dict[str, Dict[str, Any]] = {}
    for rel, entry in old_external.items():
        if rel in present and entry.get("fingerprint") == present[rel]["fingerprint"]:
            ext[rel] = entry
        else:
            report["external_tags_dropped"].append(rel)
    unknown = []
    for rel, entry in (external or {}).items():
        if rel not in present:
            unknown.append(rel)
            continue
        ext[rel] = {"tags": sorted(set(entry["tags"])), "source": entry.get("source") or "caller", "fingerprint": present[rel]["fingerprint"]}
    if unknown:
        raise AnalysisError("INVALID_INPUT", "tags given for files that are not in the library", {"paths": sorted(unknown)})
    for c in clips:
        c["external_tags"] = ext.get(c["path"], {}).get("tags", [])
        c["tags"] = sorted(set(c["measured_tags"]) | set(c["external_tags"]))
    stamp = (clock or (lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))()
    doc = {"schema": INDEX_SCHEMA, "root": str(base), "updated_at": stamp, "skill_version": VERSION,
           "clip_count": len(clips), "clips": sorted(clips, key=lambda c: c["path"]), "external_tags": dict(sorted(ext.items())),
           "failed": report["failed"]}
    return {"index": doc, "report": {k: (sorted(v) if k in ("analysed", "reused", "removed", "external_tags_dropped") else v) for k, v in report.items()}}


# ---- search
def _num_ok(value: Optional[float], lo: Optional[float], hi: Optional[float]) -> bool:
    if lo is None and hi is None:
        return True
    if value is None:
        return False
    return (lo is None or value >= lo) and (hi is None or value <= hi)


def search(index: Dict[str, Any], query: Optional[List[str]] = None, tags: Optional[List[str]] = None, *,
           min_duration: Optional[float] = None, max_duration: Optional[float] = None, orientation: Optional[str] = None,
           min_height: Optional[int] = None, has_audio: Optional[bool] = None, scene_type: Optional[str] = None,
           limit: Optional[int] = None) -> Dict[str, Any]:
    """Clips that satisfy every condition. `tags`: each must be one of the clip's tags. `query`: each word must match
    a tag or a word of the clip's path (prefix match, case-insensitive). Ranked by the number of query words that
    matched a tag exactly, then by path, so the order is deterministic."""
    words = [w.lower() for q in (query or []) for w in _WORD.findall(q.lower())]
    want_tags = [t.strip().lower() for t in (tags or []) if t.strip()]
    hits = []
    for c in index["clips"]:
        ctags = set(c.get("tags") or [])
        if any(t not in ctags for t in want_tags):
            continue
        v = c.get("video") or {}
        if not _num_ok(c.get("duration"), min_duration, max_duration):
            continue
        if orientation and v.get("orientation") != orientation:
            continue
        if min_height is not None and not (v.get("height") and v["height"] >= min_height):
            continue
        if has_audio is not None and (c.get("audio") is not None) != has_audio:
            continue
        if scene_type and c.get("scene_type") != scene_type:
            continue
        matched, exact = [], 0
        ok = True
        for w in words:
            in_tags = [t for t in ctags if t == w or t.startswith(w)]
            in_name = [n for n in c.get("name_words") or [] if n.startswith(w)]
            if not in_tags and not in_name:
                ok = False
                break
            exact += 1 if w in ctags else 0
            matched.append({"word": w, "tags": sorted(in_tags), "name_words": sorted(in_name)})
        if not ok:
            continue
        hits.append({"path": c["path"], "score": exact + len(matched), "matched": matched, "duration": c.get("duration"),
                     "representative_time": c.get("representative_time"), "tags": c.get("tags")})
    hits.sort(key=lambda h: (-h["score"], h["path"]))
    if limit is not None:
        hits = hits[:limit]
    return {"schema": "media-analysis/library-search@1", "root": index.get("root"), "result_count": len(hits), "results": hits,
            "conditions": {"query": words, "tags": want_tags, "min_duration": min_duration, "max_duration": max_duration,
                           "orientation": orientation, "min_height": min_height, "has_audio": has_audio, "scene_type": scene_type}}
