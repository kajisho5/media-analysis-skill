"""Observation cache (reuse only; not job / resume state).

Key = sha256 of canonical {asset_fingerprint, analyzer, analyzer_version, kind, parameters} — the analysis identity.
An entry is invalid (miss) when the asset fingerprint, analyzer version or parameters recorded inside it differ from
the request, when its result hash does not match its stored observation, or when the file cannot be parsed.
Entries live as JSON files inside the workspace (PathPolicy)."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import VERSION
from .canonical import canonical_json, stable_hash
from .errors import AnalysisError
from .security import PathPolicy

CACHE_FORMAT = "media-analysis-cache/1"


class ObservationCache:
    def __init__(self, directory: str, policy: PathPolicy):
        self.dir = policy.resolve_write_dir(directory)
        self.hits = 0
        self.misses = 0
        self.invalid = 0

    def _path(self, key: str) -> Path:
        if not key or any(c not in "0123456789abcdef" for c in key):
            raise AnalysisError("CACHE_INVALID", "cache key must be a hex digest")
        return self.dir / key[:2] / f"{key}.json"

    def get(self, key: str, *, asset_fingerprint: str, analyzer: str, analyzer_version: str, kind: str, parameters: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
        """(observation, status) with status hit | miss | invalid. An invalid entry is removed and counts as a miss."""
        path = self._path(key)
        if not path.exists():
            self.misses += 1
            return None, "miss"
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            meta = entry["metadata"]
            obs = entry["observation"]
            ok = (entry.get("format") == CACHE_FORMAT and meta["key"] == key and meta["asset_fingerprint"] == asset_fingerprint
                  and meta["analyzer"] == analyzer and meta["analyzer_version"] == analyzer_version and meta["kind"] == kind
                  and canonical_json(meta["parameters"]) == canonical_json(parameters) and meta["result_hash"] == stable_hash(obs))
        except (ValueError, KeyError, TypeError, OSError):
            ok = False
        if not ok:
            self.invalid += 1
            self.misses += 1
            try:
                path.unlink()
            except OSError:
                pass
            return None, "invalid"
        self.hits += 1
        return obs, "hit"

    def put(self, key: str, observation: Dict[str, Any], *, asset_fingerprint: str, analyzer: str, analyzer_version: str, kind: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        path = self._path(key)
        meta = {"key": key, "analyzer": analyzer, "analyzer_version": analyzer_version, "kind": kind, "parameters": parameters,
                "asset_fingerprint": asset_fingerprint, "created_at": observation["observed_at"], "result_hash": stable_hash(observation),
                "package_version": VERSION}
        entry = {"format": CACHE_FORMAT, "metadata": meta, "observation": observation}
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n")
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return meta

    def stats(self) -> Dict[str, Any]:
        return {"directory": str(self.dir), "hits": self.hits, "misses": self.misses, "invalid": self.invalid}
