"""Observation verification: an analyzer result is returned as an Observation only after these checks pass.

- required fields, kind / asset / analysis id identity, source format
- data is a JSON object; every number is finite; timestamps are within [0, duration + tolerance]
- no key named like a command / argv / shell field anywhere in the observation
- no secret-looking key and no value of a secret-looking environment variable anywhere in the observation
"""
from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, Iterable, List, Optional

from .canonical import canonical_json
from .contract import ANALYSIS_KINDS, OBSERVATION_REQUIRED, SOURCE_RE
from .errors import AnalysisError

FORBIDDEN_KEYS = {"command", "commands", "argv", "cmd", "cmdline", "shell", "exec"}
SECRET_KEY_RE = re.compile(r"(secret|token|password|passwd|api[_-]?key|private[_-]?key|credential|authorization)", re.I)
SECRET_ENV_RE = re.compile(r"(SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|APIKEY|PRIVATE_KEY|CREDENTIAL|AUTH)", re.I)
TIME_KEYS = {"start", "end", "time", "start_time", "representative_time", "at", "next", "first_pts", "last_pts"}


def walk(obj: Any, path: str = "$") -> Iterable[tuple]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path, k, v
            yield from walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")


def secret_env_values(env: Optional[Dict[str, str]] = None, min_len: int = 8) -> List[str]:
    env = os.environ if env is None else env
    return [v for k, v in env.items() if SECRET_ENV_RE.search(k) and isinstance(v, str) and len(v) >= min_len]


def verify_observation(obs: Dict[str, Any], *, expected_asset_id: str, expected_kind: str, expected_analysis_id: str,
                       duration: Optional[float] = None, env: Optional[Dict[str, str]] = None, time_tolerance: float = 1.0) -> None:
    problems: List[str] = []
    if not isinstance(obs, dict):
        raise AnalysisError("VERIFICATION_FAILED", "observation is not an object")
    for f in OBSERVATION_REQUIRED:
        if f not in obs:
            problems.append(f"missing field {f}")
    if problems:
        raise AnalysisError("VERIFICATION_FAILED", "observation schema", {"problems": problems})
    if obs["asset_id"] != expected_asset_id:
        problems.append(f"asset_id {obs['asset_id']!r} != {expected_asset_id!r}")
    if obs["kind"] != expected_kind or obs["kind"] not in ANALYSIS_KINDS:
        problems.append(f"kind {obs['kind']!r} != {expected_kind!r}")
    if obs["analysis_id"] != expected_analysis_id:
        problems.append(f"analysis_id {obs['analysis_id']!r} != {expected_analysis_id!r}")
    if not isinstance(obs["source"], str) or not SOURCE_RE.match(obs["source"]):
        problems.append(f"source {obs['source']!r} is not media-analysis/<tool>@<version>")
    if str(obs["source"]).lower().startswith("ai") or any(v == "AI_GENERATED" for _, _, v in walk(obs)):
        problems.append("observation must not claim an AI source")
    if not isinstance(obs["data"], dict):
        problems.append("data is not an object")
    if not isinstance(obs["id"], str) or not obs["id"]:
        problems.append("id is empty")
    if not isinstance(obs["observed_at"], str) or not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", obs["observed_at"]):
        problems.append("observed_at is not an ISO-8601 UTC timestamp")
    limit = (duration + time_tolerance) if duration is not None else None
    for path, key, val in walk(obs):
        if isinstance(key, str) and key.lower() in FORBIDDEN_KEYS:
            problems.append(f"forbidden key {key!r} at {path}")
        if isinstance(key, str) and SECRET_KEY_RE.search(key):
            problems.append(f"secret-looking key {key!r} at {path}")
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            problems.append(f"non-finite number at {path}.{key}")
        if key in TIME_KEYS and isinstance(val, (int, float)) and not isinstance(val, bool) and path.startswith("$.data"):
            if val < -time_tolerance or (limit is not None and val > limit):
                problems.append(f"timestamp {val} at {path}.{key} outside [0, {limit}]")
    if not any("non-finite" in p for p in problems):
        text = canonical_json(obs)
        for secret in secret_env_values(env):
            if secret in text:
                problems.append("observation contains the value of a secret environment variable")
                break
    if problems:
        raise AnalysisError("VERIFICATION_FAILED", "observation failed verification", {"problems": problems[:20]})
