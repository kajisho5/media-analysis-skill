"""Analyzer contract.

    Analyzer.id                  tool name ("probe", "silence", ...); tool id is "media-analysis/<id>"
    Analyzer.version             analyzer version (== package version; bump when a measurement changes)
    Analyzer.supported_kinds     AnalysisKinds served
    Analyzer.required_capabilities
    Analyzer.plan(ctx, kind, parameters)     structured description of the operations it would run (dry-run)
    Analyzer.analyze(ctx, kind, parameters)  measurement -> data dict (no Observation envelope, no interpretation)

An analyzer never sees the request's asset_id, never writes files, never receives argv from outside."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .. import VERSION
from ..capabilities import CapabilitySet
from ..errors import AnalysisError
from ..probe import parse_ffprobe, run_ffprobe


class AnalysisContext:
    """Per-request execution context: resolved input path, executables, timeout, and a lazily-run probe shared by
    every step of the same analysis (ffprobe runs at most once per request)."""

    def __init__(self, input_path: str, caps: CapabilitySet, timeout: Optional[float]):
        self.input_path = input_path
        self.caps = caps
        self.timeout = timeout
        self._probe: Optional[Dict[str, Any]] = None
        self.operations: List[Dict[str, Any]] = []   # what ran (executable + purpose), never argv

    def exe(self, name: str) -> str:
        cap = self.caps.get(name)
        if cap.status != "AVAILABLE" or not cap.path:
            raise AnalysisError("ANALYZER_UNAVAILABLE", f"{name} is not available")
        return cap.path

    def probe(self) -> Dict[str, Any]:
        if self._probe is None:
            self.operations.append({"executable": "ffprobe", "purpose": "format and stream metadata"})
            self._probe = parse_ffprobe(run_ffprobe(self.exe("ffprobe"), self.input_path, self.timeout))
        return self._probe

    def record(self, executable: str, purpose: str) -> None:
        self.operations.append({"executable": executable, "purpose": purpose})


class Analyzer:
    id: str = "abstract"
    version: str = VERSION
    supported_kinds: Tuple[str, ...] = ()
    required_capabilities: Tuple[str, ...] = ()

    def plan(self, ctx: AnalysisContext, kind: str, parameters: Dict[str, Any]) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def analyze(self, ctx: AnalysisContext, kind: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "tool_id": f"media-analysis/{self.id}", "version": self.version,
                "supported_kinds": list(self.supported_kinds), "required_capabilities": list(self.required_capabilities)}


PROBE_OP = {"executable": "ffprobe", "purpose": "format and stream metadata"}
