"""Canonical JSON and hashing. Every identity in this package (analysis id, cache key, result hash) is derived from
canonical JSON so that key order, whitespace and float formatting cannot change an identity."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, no NaN / Infinity, UTF-8 preserved."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_hash(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()
