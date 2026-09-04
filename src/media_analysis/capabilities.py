"""Runtime capability detection (`media-analysis doctor`).

Capability names follow the video-production-agent CapabilityResolver vocabulary so a future adapter can pass them
through unchanged: "ffmpeg", "ffprobe", "filter:<name>". A capability is AVAILABLE only when it was actually
detected in this process's environment; nothing is assumed."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .runner import run_argv, which

CAPABILITY_STATUS = ("AVAILABLE", "MISSING", "UNKNOWN")
FILTERS_OF_INTEREST = ("ebur128", "silencedetect", "scdet")


@dataclass
class Capability:
    name: str
    status: str
    version: Optional[str] = None
    path: Optional[str] = None
    detail: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {"name": self.name, "status": self.status, "version": self.version, "path": self.path, "detail": self.detail}


@dataclass
class CapabilitySet:
    items: Dict[str, Capability] = field(default_factory=dict)

    def get(self, name: str) -> Capability:
        return self.items.get(name) or Capability(name, "UNKNOWN", detail="not probed")

    def available(self, name: str) -> bool:
        return self.get(name).status == "AVAILABLE"

    def missing(self, names: List[str]) -> List[str]:
        return [n for n in names if not self.available(n)]

    def to_dict(self) -> Dict[str, object]:
        return {k: self.items[k].to_dict() for k in sorted(self.items)}


_VERSION_RE = re.compile(r"^ff(?:mpeg|probe) version (\S+)")


def _tool_version(exe: str, timeout: float) -> Optional[str]:
    try:
        r = run_argv([exe, "-version"], timeout=timeout)
    except Exception:
        return None
    m = _VERSION_RE.match(r.stdout)
    return m.group(1) if m else None


def _filters(exe: str, timeout: float) -> Optional[set]:
    try:
        r = run_argv([exe, "-hide_banner", "-filters"], timeout=timeout)
    except Exception:
        return None
    names = set()
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] and all(c in ".TSC" for c in parts[0]):
            names.add(parts[1])
    return names


def detect(timeout: float = 10.0) -> CapabilitySet:
    caps = CapabilitySet()
    for tool in ("ffmpeg", "ffprobe"):
        path = which(tool)
        if not path:
            caps.items[tool] = Capability(tool, "MISSING", detail="not found on PATH")
            continue
        ver = _tool_version(path, timeout)
        if ver is None:
            caps.items[tool] = Capability(tool, "MISSING", path=path, detail="found on PATH but `-version` failed")
        else:
            caps.items[tool] = Capability(tool, "AVAILABLE", version=ver, path=path)
    ff = caps.items["ffmpeg"]
    filters = _filters(ff.path, timeout) if ff.status == "AVAILABLE" else None
    for f in FILTERS_OF_INTEREST:
        name = f"filter:{f}"
        if filters is None:
            caps.items[name] = Capability(name, "MISSING", detail="ffmpeg unavailable")
        elif f in filters:
            caps.items[name] = Capability(name, "AVAILABLE", version=ff.version)
        else:
            caps.items[name] = Capability(name, "MISSING", detail="filter not compiled into this ffmpeg")
    return caps
