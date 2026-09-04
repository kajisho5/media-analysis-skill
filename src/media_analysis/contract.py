"""Skill / Tool / AnalysisKind / AnalysisRequest / Observation contract.

Stable identifiers (docs/tools.md, versioning policy in README):
  skill id   : "media-analysis"           tool id : "media-analysis/<tool>"
  source     : "media-analysis/<tool>@<version>"   (video-production-agent Observation.source = "<tool>@<version>")

Every AnalysisKind is served by exactly one tool. Only tools that are implemented in this package are declared;
there are no placeholder tools or kinds.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import SKILL_ID, VERSION
from .canonical import stable_hash
from .errors import AnalysisError

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SOURCE_RE = re.compile(r"^media-analysis/[a-z]+@\d+\.\d+\.\d+$")

# ---- parameter schemas: name -> {type, default, min, max}
_NUM = "number"
_INT = "integer"
_BOOL = "boolean"

PARAMETER_SCHEMAS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "media_probe": {},
    "stream_layout": {},
    "video_format": {"stream": {"type": _INT, "default": 0, "min": 0, "max": 63}},
    "audio_format": {"stream": {"type": _INT, "default": 0, "min": 0, "max": 63}},
    "duration": {},
    "silence": {
        "stream": {"type": _INT, "default": 0, "min": 0, "max": 63},
        "threshold_db": {"type": _NUM, "default": -40.0, "min": -120.0, "max": 0.0},
        "min_duration": {"type": _NUM, "default": 0.5, "min": 0.01, "max": 3600.0},
        "edge_tolerance": {"type": _NUM, "default": 0.1, "min": 0.0, "max": 10.0},
    },
    "loudness": {"stream": {"type": _INT, "default": 0, "min": 0, "max": 63}},
    "integrity": {"max_error_lines": {"type": _INT, "default": 200, "min": 1, "max": 100000}},
    "scene_detection": {
        "stream": {"type": _INT, "default": 0, "min": 0, "max": 63},
        "threshold": {"type": _NUM, "default": 10.0, "min": 0.0, "max": 100.0},
        "min_scene_duration": {"type": _NUM, "default": 0.5, "min": 0.0, "max": 3600.0},
    },
    "timing": {
        "gap_factor": {"type": _NUM, "default": 2.5, "min": 1.1, "max": 100.0},
        "av_mismatch_tolerance": {"type": _NUM, "default": 0.1, "min": 0.0, "max": 60.0},
    },
}

# kind -> tool (one tool serves one or more kinds)
KIND_TO_TOOL: Dict[str, str] = {
    "media_probe": "probe",
    "stream_layout": "streams",
    "video_format": "video",
    "audio_format": "audio",
    "duration": "timing",
    "silence": "silence",
    "loudness": "loudness",
    "integrity": "integrity",
    "scene_detection": "scenes",
    "timing": "timing",
}
ANALYSIS_KINDS = tuple(KIND_TO_TOOL)

TOOL_DESCRIPTIONS: Dict[str, str] = {
    "probe": "Container and primary stream facts (format, duration, size, bitrate, video/audio summary)",
    "streams": "Every stream with index, type, codec, language, disposition, dimensions, rate, channels",
    "video": "Video format of one video stream (resolution, fps, frame count, pixel format, colour, SAR/DAR, CFR/VFR)",
    "audio": "Audio format of one audio stream (sample rate, channels, layout, codec, sample format, bitrate)",
    "silence": "Silence segments of one audio stream with leading / internal / trailing classification",
    "loudness": "EBU R128 integrated loudness, loudness range and true peak of one audio stream",
    "integrity": "Full decode with error counting, per-stream frame counts and timestamp monotonicity; PASS / WARN / FAIL",
    "scenes": "Deterministic visual scene-cut detection (scdet score), not semantic scene understanding",
    "timing": "Duration and timing facts: start times, per-stream durations, A/V mismatch, timestamp discontinuities",
}

TOOL_CAPABILITIES: Dict[str, List[str]] = {
    "probe": ["ffprobe"],
    "streams": ["ffprobe"],
    "video": ["ffprobe"],
    "audio": ["ffprobe"],
    "silence": ["ffprobe", "ffmpeg", "filter:silencedetect"],
    "loudness": ["ffprobe", "ffmpeg", "filter:ebur128"],
    "integrity": ["ffprobe", "ffmpeg"],
    "scenes": ["ffprobe", "ffmpeg", "filter:scdet"],
    "timing": ["ffprobe"],
}


def tool_id(tool: str) -> str:
    return f"{SKILL_ID}/{tool}"


def source_for(tool: str, version: str = VERSION) -> str:
    return f"{tool_id(tool)}@{version}"


def skill_contract() -> Dict[str, Any]:
    """Machine-readable Skill contract (`media-analysis contract`). Mirrors video-production-agent SkillPackage /
    ToolSpec fields so an adapter can map it one-to-one."""
    tools = []
    for tool in sorted(set(KIND_TO_TOOL.values())):
        kinds = [k for k, t in KIND_TO_TOOL.items() if t == tool]
        tools.append({
            "tool_id": tool_id(tool), "skill_id": SKILL_ID, "version": VERSION, "description": TOOL_DESCRIPTIONS[tool],
            "required_capabilities": list(TOOL_CAPABILITIES[tool]), "inputs": ["input"], "produces_output": False,
            "deterministic": True, "kinds": kinds, "parameters": {k: PARAMETER_SCHEMAS[k] for k in kinds},
            "result_keys": ["observation"],
        })
    return {
        "skill_id": SKILL_ID, "name": "media-analysis", "package": "media-analysis-skill", "version": VERSION,
        "description": "Deterministic media observation / analysis. Measures facts about media files; never interprets, decides or edits.",
        "role": "observation / analysis",
        "capabilities": ["ffprobe"],   # the whole package needs ffprobe; ffmpeg + filters are per tool
        "tools": tools,
        "analysis_kinds": list(ANALYSIS_KINDS),
        "observation_source_format": "media-analysis/<tool>@<version>",
        "schema_version": "1",
    }


# ---- AnalysisRequest
@dataclass
class AnalysisRequest:
    asset_id: str
    input: str
    kind: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    analysis_id: Optional[str] = None
    timeout: Optional[float] = None
    output_policy: Dict[str, Any] = field(default_factory=dict)

    ALLOWED_KEYS = ("analysis_id", "asset_id", "input", "kind", "parameters", "timeout", "output_policy")
    FORBIDDEN_KEYS = ("command", "argv", "args", "shell", "cmd", "exec")

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AnalysisRequest":
        if not isinstance(d, dict):
            raise AnalysisError("INVALID_INPUT", "request must be a JSON object")
        bad = [k for k in d if k in cls.FORBIDDEN_KEYS]
        if bad:
            raise AnalysisError("INVALID_INPUT", "request must not carry command / argv style fields", {"fields": bad})
        unknown = [k for k in d if k not in cls.ALLOWED_KEYS]
        if unknown:
            raise AnalysisError("INVALID_INPUT", "unknown request fields", {"fields": unknown})
        for k in ("asset_id", "input", "kind"):
            if k not in d:
                raise AnalysisError("INVALID_INPUT", f"request field {k!r} is required")
        req = cls(asset_id=d["asset_id"], input=d["input"], kind=d["kind"], parameters=d.get("parameters") or {},
                  analysis_id=d.get("analysis_id"), timeout=d.get("timeout"), output_policy=d.get("output_policy") or {})
        req.validate()
        return req

    def validate(self) -> None:
        if not isinstance(self.asset_id, str) or not ID_RE.match(self.asset_id):
            raise AnalysisError("INVALID_INPUT", "asset_id must match " + ID_RE.pattern, {"asset_id": self.asset_id})
        if self.analysis_id is not None and (not isinstance(self.analysis_id, str) or not ID_RE.match(self.analysis_id)):
            raise AnalysisError("INVALID_INPUT", "analysis_id must match " + ID_RE.pattern, {"analysis_id": self.analysis_id})
        if not isinstance(self.input, str) or not self.input:
            raise AnalysisError("INVALID_INPUT", "input must be a non-empty path string")
        if self.kind not in ANALYSIS_KINDS:
            raise AnalysisError("INVALID_INPUT", f"unknown analysis kind {self.kind!r}", {"allowed": list(ANALYSIS_KINDS)})
        if not isinstance(self.parameters, dict):
            raise AnalysisError("INVALID_INPUT", "parameters must be an object")
        self.parameters = validate_parameters(self.kind, self.parameters)
        if self.timeout is not None:
            if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or not (0 < self.timeout <= 86400):
                raise AnalysisError("INVALID_INPUT", "timeout must be a number of seconds in (0, 86400]")
            self.timeout = float(self.timeout)
        if not isinstance(self.output_policy, dict):
            raise AnalysisError("INVALID_INPUT", "output_policy must be an object")
        unknown = [k for k in self.output_policy if k != "round"]
        if unknown:
            raise AnalysisError("INVALID_INPUT", "unknown output_policy fields", {"fields": unknown})
        rnd = self.output_policy.get("round", 3)
        if isinstance(rnd, bool) or not isinstance(rnd, int) or not (0 <= rnd <= 9):
            raise AnalysisError("INVALID_INPUT", "output_policy.round must be an integer in [0, 9]")
        self.output_policy = {"round": rnd}

    @property
    def tool(self) -> str:
        return KIND_TO_TOOL[self.kind]

    def to_dict(self) -> Dict[str, Any]:
        return {"analysis_id": self.analysis_id, "asset_id": self.asset_id, "input": self.input, "kind": self.kind,
                "parameters": dict(self.parameters), "timeout": self.timeout, "output_policy": dict(self.output_policy)}


def validate_parameters(kind: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Apply defaults, reject unknown names and out-of-range values. Returns the *effective* parameters, which are
    what the analysis identity and cache key are computed from."""
    schema = PARAMETER_SCHEMAS[kind]
    unknown = [k for k in params if k not in schema]
    if unknown:
        raise AnalysisError("INVALID_INPUT", f"unknown parameters for {kind}", {"fields": unknown, "allowed": list(schema)})
    out: Dict[str, Any] = {}
    for name, spec in schema.items():
        val = params.get(name, spec["default"])
        t = spec["type"]
        if t == _BOOL:
            if not isinstance(val, bool):
                raise AnalysisError("INVALID_INPUT", f"parameter {name} must be a boolean")
        elif t == _INT:
            if isinstance(val, bool) or not isinstance(val, int):
                raise AnalysisError("INVALID_INPUT", f"parameter {name} must be an integer")
        else:
            if isinstance(val, bool) or not isinstance(val, (int, float)) or val != val or val in (float("inf"), float("-inf")):
                raise AnalysisError("INVALID_INPUT", f"parameter {name} must be a finite number")
            val = float(val)
        if t != _BOOL and not (spec["min"] <= val <= spec["max"]):
            raise AnalysisError("INVALID_INPUT", f"parameter {name} out of range [{spec['min']}, {spec['max']}]", {"value": val})
        out[name] = val
    return out


def analysis_identity(asset_fingerprint: str, tool: str, analyzer_version: str, kind: str, parameters: Dict[str, Any]) -> str:
    """Same input + analyzer + analyzer version + kind + parameters -> same identity. Used for the derived
    analysis id and the cache key (docs/architecture.md, Determinism)."""
    return stable_hash({"asset_fingerprint": asset_fingerprint, "analyzer": tool_id(tool), "analyzer_version": analyzer_version,
                        "kind": kind, "parameters": parameters})


# ---- Observation
OBSERVATION_REQUIRED = ("id", "asset_id", "kind", "data", "source", "analysis_id", "observed_at")


def make_observation(*, identity: str, asset_id: str, kind: str, tool: str, analyzer_version: str, data: Dict[str, Any],
                     analysis_id: str, observed_at: str, parameters: Dict[str, Any], asset: Dict[str, Any], seconds: float) -> Dict[str, Any]:
    return {
        "id": "obs_" + identity[:16],
        "asset_id": asset_id,
        "kind": kind,
        "data": data,
        "source": source_for(tool, analyzer_version),
        "analysis_id": analysis_id,
        "observed_at": observed_at,
        "analysis": {"identity": identity, "analyzer": tool_id(tool), "analyzer_version": analyzer_version, "parameters": parameters, "seconds": seconds},
        "asset": asset,
    }
