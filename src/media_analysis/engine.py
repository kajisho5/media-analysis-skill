"""AnalysisEngine: request -> validation -> path policy -> capability check -> budget -> cache -> analyzer ->
verification -> Observation. Single entry point for the CLI and for any adapter. `run()` produces the response
document of the machine interface (schemas.RESPONSE_SCHEMA); `analyze()` / `plan()` produce one result each."""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Optional, Union

from . import SKILL_ID, VERSION
from .analyzers.base import AnalysisContext
from .budget import Budget, BudgetTracker
from .cache import ObservationCache
from .canonical import sha256_file
from .capabilities import CapabilitySet, detect
from .contract import AnalysisRequest, analysis_identity, make_observation, tool_id
from .errors import AnalysisError
from .probe import container_duration
from .registry import AnalyzerRegistry, default_registry
from .schemas import RESPONSE_SCHEMA_VERSION
from .security import PathPolicy
from .verify import verify_observation

RESPONSE_SCHEMA_ID = f"media-analysis/response@{RESPONSE_SCHEMA_VERSION}"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def fingerprint_file(path: str) -> Dict[str, Any]:
    st = os.stat(path)
    return {"sha256": sha256_file(path), "size": st.st_size}


def _usage(calls: int, seconds: float, operations: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    return {"analyzer_calls": calls, "seconds": seconds, "operations": list(operations or [])}


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

    @staticmethod
    def _result(req: AnalysisRequest, analysis_id: Optional[str], **fields: Any) -> Dict[str, Any]:
        base = {"analysis_id": analysis_id, "asset_id": req.asset_id, "kind": req.kind, "status": "ok",
                "cache": {"status": "disabled", "policy": req.cache_policy, "key": None}, "usage": _usage(0, 0.0)}
        base.update(fields)
        return base

    # ---- dry run
    def plan(self, request: Union[AnalysisRequest, Dict[str, Any]]) -> Dict[str, Any]:
        """Validate and describe what `analyze` would do. Runs no ffprobe / ffmpeg, computes no fingerprint."""
        req = self._request(request)
        prep = self._prepare(req)
        analyzer = prep["analyzer"]
        ctx = AnalysisContext(prep["path"], self.caps, req.timeout)
        plan = {
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
        return self._result(req, req.analysis_id, plan=plan)

    # ---- analysis
    def analyze(self, request: Union[AnalysisRequest, Dict[str, Any]]) -> Dict[str, Any]:
        """One result document (schemas.RESULT_SCHEMA). Raises AnalysisError on failure."""
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
        use_cache = self.cache is not None and req.cache_policy != "bypass"
        cache_status = "disabled" if self.cache is None else ("bypass" if req.cache_policy == "bypass" else "miss")

        if use_cache:
            cached, cache_status = self.cache.get(identity, **cache_kw)
            if cached is not None:
                obs = dict(cached)
                obs["asset_id"] = req.asset_id            # the caller's label for this asset; not part of the identity
                obs["analysis_id"] = analysis_id
                verify_observation(obs, expected_asset_id=req.asset_id, expected_kind=req.kind, expected_analysis_id=analysis_id)
                return self._result(req, analysis_id, observation=_round(obs, req.output_policy["round"]),
                                    cache={"status": "hit", "policy": req.cache_policy, "key": identity}, usage=_usage(0, 0.0))
            if req.cache_policy == "only":
                raise AnalysisError("CACHE_MISS", "cache_policy 'only' and no valid cache entry", {"cache_key": identity, "cache_status": cache_status})
        elif req.cache_policy == "only":
            raise AnalysisError("CACHE_MISS", "cache_policy 'only' but no cache is configured", {"cache_key": identity})

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
        if use_cache:
            self.cache.put(identity, obs, **cache_kw)
        return self._result(req, analysis_id, observation=_round(obs, req.output_policy["round"]),
                            cache={"status": cache_status, "policy": req.cache_policy, "key": identity}, usage=_usage(1, seconds, ctx.operations))

    # ---- machine interface
    def run(self, document: Any, dry_run: bool = False) -> Dict[str, Any]:
        """Execute a request document (one request, a list, or a batch {"requests": [...], "budget": {...}}) and
        return the response document. Per-request failures become error results; the response status is ok /
        partial / error. A document that is not a valid batch is an error response with no results."""
        try:
            requests, budget = self._parse_document(document)
        except AnalysisError as e:
            return self._response([], warnings=[], error=e)
        if budget is not None:
            self.tracker = BudgetTracker(budget)
        results = []
        for doc in requests:
            try:
                results.append(self.plan(doc) if dry_run else self.analyze(doc))
            except AnalysisError as e:
                results.append(self._error_result(doc, e))
        return self._response(results, warnings=[], dry_run=dry_run)

    @staticmethod
    def _parse_document(document: Any):
        if isinstance(document, list):
            return document, None
        if isinstance(document, dict) and "requests" in document:
            unknown = [k for k in document if k not in ("requests", "budget", "schema")]
            if unknown:
                raise AnalysisError("INVALID_INPUT", "unknown batch fields", {"fields": unknown})
            reqs = document["requests"]
            if not isinstance(reqs, list) or not reqs:
                raise AnalysisError("INVALID_INPUT", "batch.requests must be a non-empty list")
            budget = Budget.from_dict(document["budget"]) if document.get("budget") is not None else None
            return reqs, budget
        if isinstance(document, dict):
            return [document], None
        raise AnalysisError("INVALID_INPUT", "request document must be an object or a list of objects")

    @staticmethod
    def _error_result(doc: Any, e: AnalysisError) -> Dict[str, Any]:
        d = doc if isinstance(doc, dict) else {}
        ident = lambda v: v if isinstance(v, str) else None  # noqa: E731
        return {"analysis_id": ident(d.get("analysis_id")), "asset_id": ident(d.get("asset_id")), "kind": ident(d.get("kind")), "status": "error",
                "error": e.to_dict(), "error_kind": e.code,
                "cache": {"status": "disabled" if e.code != "CACHE_MISS" else e.details.get("cache_status", "miss"), "policy": d.get("cache_policy") if d.get("cache_policy") in ("use", "bypass", "only") else "use",
                          "key": e.details.get("cache_key") if e.code == "CACHE_MISS" else None},
                "usage": _usage(0, 0.0)}

    @classmethod
    def error_response(cls, error: AnalysisError) -> Dict[str, Any]:
        """Response document for a failure that happened before any engine existed (e.g. CLI argument errors)."""
        return {"schema": RESPONSE_SCHEMA_ID, "skill": {"id": SKILL_ID, "version": VERSION}, "status": "error", "dry_run": False, "results": [], "observations": [],
                "usage": {"analyzer_calls": 0, "cache_hits": 0, "seconds": 0.0}, "budget": BudgetTracker(Budget()).state(), "warnings": [],
                "error": error.to_dict(), "error_kind": error.code}

    def _response(self, results: List[Dict[str, Any]], warnings: List[str], error: Optional[AnalysisError] = None, dry_run: bool = False) -> Dict[str, Any]:
        ok = [r for r in results if r["status"] == "ok"]
        status = "error" if error is not None or (results and not ok) else ("partial" if len(ok) < len(results) else "ok")
        doc: Dict[str, Any] = {
            "schema": RESPONSE_SCHEMA_ID, "skill": {"id": SKILL_ID, "version": VERSION}, "status": status, "dry_run": dry_run,
            "results": results, "observations": [r["observation"] for r in ok if "observation" in r],
            "usage": {"analyzer_calls": sum(r["usage"]["analyzer_calls"] for r in results), "cache_hits": sum(1 for r in results if r["cache"]["status"] == "hit"),
                      "seconds": round(sum(r["usage"]["seconds"] for r in results), 6)},
            "budget": self.tracker.state(), "warnings": list(warnings),
        }
        if error is not None:
            doc["error"] = error.to_dict()
            doc["error_kind"] = error.code
        return doc


def exit_code_for(response: Dict[str, Any]) -> int:
    """0 when every result is ok; otherwise the exit code of the first error (batch-level error first)."""
    if response.get("error_kind"):
        return AnalysisError(response["error_kind"], "").exit_code
    for r in response.get("results", []):
        if r["status"] == "error":
            return AnalysisError(r["error_kind"], "").exit_code
    return 0


def _round(obj: Any, digits: int) -> Any:
    if isinstance(obj, float):
        return round(obj, digits)
    if isinstance(obj, dict):
        return {k: _round(v, digits) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round(v, digits) for v in obj]
    return obj
