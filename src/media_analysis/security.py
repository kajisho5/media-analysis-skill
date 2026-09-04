"""Path policy: what may be read, where may be written.

- Inputs must be existing regular files. Symlinks are resolved before any check, so a link cannot escape a root.
- If allowed_input_roots is set, the resolved input must live under one of them (PATH_NOT_ALLOWED otherwise).
- Every write (cache) must resolve to a location inside `workspace`.
- Paths are handed to ffprobe / ffmpeg as resolved absolute paths after "-i", so a file named "-v" or
  "concat:..." can never be interpreted as an option or a protocol.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from .errors import AnalysisError


class PathPolicy:
    def __init__(self, workspace: Optional[str] = None, allowed_input_roots: Optional[List[str]] = None):
        self.workspace = Path(workspace or os.getcwd()).resolve()
        self.allowed_input_roots = [Path(r).resolve() for r in allowed_input_roots] if allowed_input_roots else None

    def resolve_input(self, path: str) -> Path:
        if not isinstance(path, str) or not path or "\x00" in path:
            raise AnalysisError("INVALID_INPUT", "input must be a non-empty path string")
        p = Path(path)
        try:
            resolved = p.resolve(strict=True)
        except FileNotFoundError:
            raise AnalysisError("FILE_NOT_FOUND", f"input not found: {path}")
        except (OSError, RuntimeError) as e:
            raise AnalysisError("INVALID_INPUT", f"cannot resolve input path: {e}")
        if not resolved.is_file():
            raise AnalysisError("FILE_NOT_FOUND", f"input is not a regular file: {path}")
        if self.allowed_input_roots is not None and not any(_under(resolved, r) for r in self.allowed_input_roots):
            raise AnalysisError("PATH_NOT_ALLOWED", f"input is outside the allowed input roots: {path}",
                                {"allowed_input_roots": [str(r) for r in self.allowed_input_roots]})
        return resolved

    def resolve_write_dir(self, path: str) -> Path:
        """A directory this package may create files in. Must be inside the workspace."""
        target = Path(path)
        if not target.is_absolute():
            target = self.workspace / target
        # resolve the deepest existing ancestor so symlinks are honoured even before the dir exists
        probe = target
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        resolved = probe.resolve() / target.relative_to(probe) if probe != target else target.resolve()
        if not _under(resolved, self.workspace):
            raise AnalysisError("PATH_NOT_ALLOWED", f"write location is outside the workspace: {path}", {"workspace": str(self.workspace)})
        return resolved


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
