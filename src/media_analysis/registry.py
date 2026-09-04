"""Static analyzer registry: the fixed set of analyzers this package ships. No dynamic loading, no plugins."""
from __future__ import annotations

from typing import Dict, List

from .analyzers import (Analyzer, AudioAnalyzer, IntegrityAnalyzer, LoudnessAnalyzer, ProbeAnalyzer, SceneAnalyzer,
                        SilenceAnalyzer, StreamAnalyzer, TimingAnalyzer, VideoAnalyzer)
from .capabilities import CapabilitySet
from .contract import KIND_TO_TOOL, TOOL_CAPABILITIES
from .errors import AnalysisError


class AnalyzerRegistry:
    def __init__(self, analyzers: List[Analyzer]):
        self._by_id: Dict[str, Analyzer] = {}
        self._by_kind: Dict[str, Analyzer] = {}
        for a in analyzers:
            if a.id in self._by_id:
                raise ValueError(f"duplicate analyzer id {a.id}")
            self._by_id[a.id] = a
            for k in a.supported_kinds:
                if k in self._by_kind:
                    raise ValueError(f"kind {k} served by two analyzers")
                self._by_kind[k] = a
        # the contract and the registry must agree exactly (no declared-but-unimplemented tools / kinds)
        if set(self._by_kind) != set(KIND_TO_TOOL) or any(KIND_TO_TOOL[k] != a.id for k, a in self._by_kind.items()):
            raise ValueError("analyzer registry does not match contract KIND_TO_TOOL")
        for a in analyzers:
            if list(a.required_capabilities) != TOOL_CAPABILITIES[a.id]:
                raise ValueError(f"analyzer {a.id} capabilities differ from contract")

    def for_kind(self, kind: str) -> Analyzer:
        try:
            return self._by_kind[kind]
        except KeyError:
            raise AnalysisError("INVALID_INPUT", f"unknown analysis kind {kind!r}")

    def get(self, analyzer_id: str) -> Analyzer:
        return self._by_id[analyzer_id]

    def all(self) -> List[Analyzer]:
        return [self._by_id[k] for k in sorted(self._by_id)]

    def availability(self, caps: CapabilitySet) -> List[Dict[str, object]]:
        rows = []
        for a in self.all():
            missing = caps.missing(list(a.required_capabilities))
            rows.append({**a.to_dict(), "status": "available" if not missing else "unavailable", "missing_capabilities": missing})
        return rows


def default_registry() -> AnalyzerRegistry:
    return AnalyzerRegistry([ProbeAnalyzer(), StreamAnalyzer(), VideoAnalyzer(), AudioAnalyzer(), SilenceAnalyzer(),
                             LoudnessAnalyzer(), IntegrityAnalyzer(), SceneAnalyzer(), TimingAnalyzer()])
