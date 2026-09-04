"""Analysis budget: only limits this package can actually enforce.

    max_analysis_calls  analyzer executions allowed through one Engine (cache hits do not count)
    timeout             seconds per analyzer execution (default for requests without their own timeout)
    max_total_seconds   cumulative analyzer wall time through one Engine

When a limit would be exceeded the analyzer is not run and BUDGET_EXCEEDED is raised. Other budgets named in the
agent's specification (storage, GPU, API cost) are not measurable here and are deliberately absent."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .errors import AnalysisError


@dataclass
class Budget:
    max_analysis_calls: Optional[int] = None
    timeout: Optional[float] = 600.0
    max_total_seconds: Optional[float] = None

    def __post_init__(self) -> None:
        if self.max_analysis_calls is not None and (isinstance(self.max_analysis_calls, bool) or not isinstance(self.max_analysis_calls, int) or self.max_analysis_calls < 0):
            raise AnalysisError("INVALID_INPUT", "budget.max_analysis_calls must be a non-negative integer")
        for name in ("timeout", "max_total_seconds"):
            v = getattr(self, name)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0):
                raise AnalysisError("INVALID_INPUT", f"budget.{name} must be a positive number")

    def to_dict(self) -> Dict[str, Any]:
        return {"max_analysis_calls": self.max_analysis_calls, "timeout": self.timeout, "max_total_seconds": self.max_total_seconds}


class BudgetTracker:
    def __init__(self, budget: Budget):
        self.budget = budget
        self.calls = 0
        self.seconds = 0.0

    def check(self) -> None:
        b = self.budget
        if b.max_analysis_calls is not None and self.calls >= b.max_analysis_calls:
            raise AnalysisError("BUDGET_EXCEEDED", f"max_analysis_calls={b.max_analysis_calls} reached", self.state())
        if b.max_total_seconds is not None and self.seconds >= b.max_total_seconds:
            raise AnalysisError("BUDGET_EXCEEDED", f"max_total_seconds={b.max_total_seconds} reached", self.state())

    def effective_timeout(self, request_timeout: Optional[float]) -> Optional[float]:
        """The tighter of the request timeout, the budget timeout and the remaining total seconds."""
        candidates = [t for t in (request_timeout, self.budget.timeout) if t is not None]
        if self.budget.max_total_seconds is not None:
            candidates.append(max(0.001, self.budget.max_total_seconds - self.seconds))
        return min(candidates) if candidates else None

    def charge(self, seconds: float) -> None:
        self.calls += 1
        self.seconds += seconds

    def state(self) -> Dict[str, Any]:
        return {"calls": self.calls, "seconds": round(self.seconds, 3), "budget": self.budget.to_dict()}
