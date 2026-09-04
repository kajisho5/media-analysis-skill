"""AnalysisEngine: request -> validation -> path policy -> capability check -> budget -> cache -> analyzer ->
verification -> Observation. This is the single entry point used by the CLI (and by any future adapter)."""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, Optional, Union

from .analyzers.base import AnalysisContext
from .budget import Budget, BudgetTracker
from .cache import ObservationCache
from .canonical import sha256_file
from .capabilities import CapabilitySet, detect
from .contract import AnalysisRequest, analysis_identity, make_observation, tool_id
from .errors import AnalysisError
from .probe import container_duration
from .registry import AnalyzerRegistry, default_registry
from .security import PathPolicy
from .verify import verify_observation


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def fingerprint_file(path: str) -> Dict[str, Any]:
    st = os.stat(path)
    return {"sha256": sha256_file(path), "size": st.st_size}


class AnalysisEngine:
    def __init__(self, caps: Optional[CapabilitySet] = None, registry: Optional[AnalyzerRegistry] = None, policy: Optional[PathPolicy] = None,
                 cache: Optional[ObservationCache] = None, budget: Optional[Budget] = None, clock: Callable[[], str] = now_iso):
        self.caps = caps if caps is not None else detect()
        self.registry = registry or default_registry()
        self.policy = policy or PathPolicy()
        self.cache = cache
        self.tracker = BudgetTracker(budget or Budget())
        self.clock = clock
        self.executions: list = []   # analyzer executions performed by this engine (cache hits excluded)

    # ---- helpers
    def _request(self, request: Union[AnalysisRequest, Dict[str, Any]]) -> AnalysisRequest:
        if isinstance(request, AnalysisRequest):
            request.validate()
            return request
        return AnalysisRequest.from_dict(request)

    def _prepare(self, req: AnalysisRequest) -> Dict[str, Any]:
        path = self.policy.resolve_input(req.input)
        analyzer = self.registry.for_kind(req.kind)
        missing = self.caps.missing(list(analyzer.required_capabilities))
        return {"path": str(path), "analyzer": analyzer, "missing": missing}

    # ---- dry run
    def plan(self, request: Union[AnalysisRequest, Dict[str, Any]]) -> Dict[str, Any]:
        """Validate and describe what `analyze` would do. Runs no ffprobe / ffmpeg."""
        req = self._request(request)
        prep = self._prepare(req)
        analyzer = prep["analyzer"]
        ctx = AnalysisContext(prep["path"], self.caps, req.timeout)
        return {
            "dry_run": True,
            "request": req.to_dict(),
            "input": prep["path"],
            "analyzer": analyzer.to_dict(),
            "required_capabilities": list(analyzer.required_capabilities),
            "capabilities": {c: self.caps.get(c).status for c in analyzer.required_capabilities},
            "missing_capabilities": prep["missing"],
            "executable": not prep["missing"],
            "parameters": req.parameters,
            "operations": analyzer.plan(ctx, req.kind, req.parameters),
            "budget": self.tracker.state(),
            "cache": "enabled" if self.cache else "disabled",
        }

    # ---- analysis
    def analyze(self, request: Union[AnalysisRequest, Dict[str, Any]]) -> Dict[str, Any]:
        req = self._request(request)
        prep = self._prepare(req)
        analyzer = prep["analyzer"]
        if prep["missing"]:
            raise AnalysisError("ANALYZER_UNAVAILABLE", f"{tool_id(analyzer.id)} needs capabilities that are missing", {"missing": prep["missing"]})
        fp = fingerprint_file(prep["path"])
        identity = analysis_identity(fp["sha256"], analyzer.id, analyzer.version, req.kind, req.parameters)
        analysis_id = req.analysis_id or ("analysis-" + identity[:16])
        asset = {"path": prep["path"], "fingerprint": fp["sha256"], "size": fp["size"]}
        cache_kw = {"asset_fingerprint": fp["sha256"], "analyzer": tool_id(analyzer.id), "analyzer_version": analyzer.version, "kind": req.kind, "parameters": req.parameters}

        if self.cache is not None:
            cached = self.cache.get(identity, **cache_kw)
            if cached is not None:
                obs = dict(cached)
                obs["asset_id"] = req.asset_id            # the caller's label for this asset; not part of the identity
                obs["analysis_id"] = analysis_id
                verify_observation(obs, expected_asset_id=req.asset_id, expected_kind=req.kind, expected_analysis_id=analysis_id)
                return {"observation": _round(obs, req.output_policy["round"]), "cache": "hit", "cache_key": identity, "budget": self.tracker.state()}

        self.tracker.check()
        timeout = self.tracker.effective_timeout(req.timeout)
        ctx = AnalysisContext(prep["path"], self.caps, timeout)
        t0 = time.monotonic()
        try:
            data = analyzer.analyze(ctx, req.kind, req.parameters)
        finally:
            seconds = round(time.monotonic() - t0, 6)
            self.tracker.charge(seconds)
            self.executions.append({"analyzer": tool_id(analyzer.id), "kind": req.kind, "seconds": seconds, "operations": list(ctx.operations)})
        if not isinstance(data, dict):
            raise AnalysisError("INVALID_RESULT", f"{tool_id(analyzer.id)} returned {type(data).__name__}, not an object")
        obs = make_observation(identity=identity, asset_id=req.asset_id, kind=req.kind, tool=analyzer.id, analyzer_version=analyzer.version, data=data,
                               analysis_id=analysis_id, observed_at=self.clock(), parameters=req.parameters, asset=asset, seconds=seconds)
        duration = container_duration(ctx._probe) if ctx._probe else None
        verify_observation(obs, expected_asset_id=req.asset_id, expected_kind=req.kind, expected_analysis_id=analysis_id, duration=duration)
        if self.cache is not None:
            self.cache.put(identity, obs, **cache_kw)
        return {"observation": _round(obs, req.output_policy["round"]), "cache": "miss" if self.cache is not None else "disabled", "cache_key": identity, "budget": self.tracker.state()}


def _round(obj: Any, digits: int) -> Any:
    if isinstance(obj, float):
        return round(obj, digits)
    if isinstance(obj, dict):
        return {k: _round(v, digits) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round(v, digits) for v in obj]
    return obj

