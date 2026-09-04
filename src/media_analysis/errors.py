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


class AnalysisError(Exception):
    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code!r}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.code]
