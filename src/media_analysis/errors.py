"""Structured error model. Every failure crossing the Skill boundary is an AnalysisError with a code from
ERROR_CODES, never a bare string exception."""
from __future__ import annotations

from typing import Any, Dict, Optional

ERROR_CODES = (
    "INVALID_INPUT",        # request / parameters do not satisfy the contract
    "FILE_NOT_FOUND",       # input path does not exist or is not a regular file
    "PATH_NOT_ALLOWED",     # input outside allowed roots, or a write outside the workspace
    "UNSUPPORTED_FORMAT",   # ffprobe could not open the file as media
    "ANALYZER_UNAVAILABLE", # required capability (ffmpeg / ffprobe / filter) missing
    "ANALYZER_TIMEOUT",     # analyzer exceeded its timeout
    "ANALYSIS_FAILED",      # analyzer ran but did not produce a result
    "INVALID_RESULT",       # analyzer output failed verification
    "BUDGET_EXCEEDED",      # analysis budget exhausted before the analyzer ran
    "CACHE_INVALID",        # cache entry unreadable or inconsistent
    "VERIFICATION_FAILED",  # observation failed the final verification (asset / kind / source / schema)
    "CACHE_MISS",           # cache_policy "only" and no valid cache entry (no analyzer is run)
)

# process exit codes for the CLI (stable part of the contract)
EXIT_CODES = {code: i + 2 for i, code in enumerate(ERROR_CODES)}

# Failure classes for a caller's retry policy (AI-video-production-OS docs/FAILURE_RECOVERY.md section 2):
#   FATAL      terminal for this request: retrying the unchanged request rejects identically (validation, security,
#              deterministic analysis failure, verification)
#   RETRYABLE  a bounded retry of the same request may succeed (timeout, transient process failure)
#   BLOCKED    the environment or the caller's budget must change first (missing executable / filter, budget exhausted)
ERROR_CLASSES = ("FATAL", "RETRYABLE", "BLOCKED")
ERROR_CLASS_OF = {
    "INVALID_INPUT": "FATAL",
    "FILE_NOT_FOUND": "FATAL",
    "PATH_NOT_ALLOWED": "FATAL",
    "UNSUPPORTED_FORMAT": "FATAL",
    "ANALYZER_UNAVAILABLE": "BLOCKED",
    "ANALYZER_TIMEOUT": "RETRYABLE",
    "ANALYSIS_FAILED": "RETRYABLE",
    "INVALID_RESULT": "FATAL",
    "BUDGET_EXCEEDED": "BLOCKED",
    "CACHE_INVALID": "FATAL",
    "VERIFICATION_FAILED": "FATAL",
    "CACHE_MISS": "FATAL",
}


class AnalysisError(Exception):
    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code!r}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = dict(details or {})

    @property
    def error_class(self) -> str:
        return ERROR_CLASS_OF[self.code]

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details, "class": self.error_class}

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.code]
