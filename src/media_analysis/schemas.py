"""Transport-independent JSON schemas of the machine interface (request, response, observation) and a minimal
validator for them. The schemas are the contract published by `media-analysis contract --json`; the same documents
are what a future MCP transport would carry. Standard library only: the validator supports the subset of JSON
Schema this contract uses (type, required, properties, additionalProperties, enum, items, pattern, minimum,
maximum, const, nullable via type lists)."""
from __future__ import annotations

import re
from typing import Any, Dict, List

REQUEST_SCHEMA_VERSION = "1"
RESPONSE_SCHEMA_VERSION = "1"
OBSERVATION_SCHEMA_VERSION = "1"

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
SOURCE_PATTERN = r"^media-analysis/[a-z]+@\d+\.\d+\.\d+$"
CACHE_POLICIES = ("use", "bypass", "only")
CACHE_STATUSES = ("hit", "miss", "invalid", "bypass", "disabled")
RESULT_STATUSES = ("ok", "error")
RESPONSE_STATUSES = ("ok", "partial", "error")

BUDGET_SCHEMA: Dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "max_analysis_calls": {"type": ["integer", "null"], "minimum": 0},
        "timeout": {"type": ["number", "null"], "minimum": 0},
        "max_total_seconds": {"type": ["number", "null"], "minimum": 0},
    },
}


def request_schema(kinds: List[str], parameter_schemas: Dict[str, Dict[str, Dict[str, Any]]]) -> Dict[str, Any]:
    """One AnalysisRequest. `parameters` is validated per kind by the engine (contract.validate_parameters); the
    schema lists every parameter name per kind under `x-parameters-by-kind` so an adapter can build requests."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "media-analysis AnalysisRequest", "version": REQUEST_SCHEMA_VERSION,
        "type": "object", "additionalProperties": False,
        "required": ["asset_id", "input", "kind"],
        "properties": {
            "analysis_id": {"type": ["string", "null"], "pattern": ID_PATTERN, "description": "caller's id; derived from the analysis identity when omitted"},
            "asset_id": {"type": "string", "pattern": ID_PATTERN, "description": "caller's label for the asset; echoed, not part of the identity"},
            "input": {"type": "string", "minLength": 1, "description": "path of one media file (absolute, or relative to the process working directory)"},
            "kind": {"type": "string", "enum": list(kinds)},
            "parameters": {"type": "object", "description": "kind-specific parameters; unknown names, wrong types and out-of-range values are INVALID_INPUT"},
            "timeout": {"type": ["number", "null"], "exclusiveMinimum": 0, "maximum": 86400, "description": "seconds per analyzer execution"},
            "cache_policy": {"type": "string", "enum": list(CACHE_POLICIES), "default": "use",
                             "description": "use: read and write; bypass: never read or write; only: read, CACHE_MISS when absent"},
            "output_policy": {"type": "object", "additionalProperties": False, "properties": {"round": {"type": "integer", "minimum": 0, "maximum": 9, "default": 3}}},
        },
        "x-forbidden-properties": ["command", "argv", "args", "shell", "cmd", "exec"],
        "x-parameters-by-kind": parameter_schemas,
    }


BATCH_SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "media-analysis request batch", "version": REQUEST_SCHEMA_VERSION,
    "description": "Document accepted by `media-analysis run`: a single AnalysisRequest, a list of them, or this batch envelope.",
    "type": "object", "additionalProperties": False,
    "required": ["requests"],
    "properties": {
        "requests": {"type": "array", "minItems": 1, "items": {"$ref": "#/request"}},
        "budget": BUDGET_SCHEMA,
    },
}

OBSERVATION_SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "media-analysis Observation", "version": OBSERVATION_SCHEMA_VERSION,
    "type": "object", "additionalProperties": False,
    "required": ["id", "asset_id", "kind", "data", "source", "analysis_id", "observed_at", "analysis", "asset"],
    "properties": {
        "id": {"type": "string", "pattern": r"^obs_[0-9a-f]{16}$"},
        "asset_id": {"type": "string", "pattern": ID_PATTERN},
        "kind": {"type": "string"},
        "data": {"type": "object", "description": "measured values only; finite numbers; no command / argv keys"},
        "source": {"type": "string", "pattern": SOURCE_PATTERN, "description": "media-analysis/<tool>@<version>; provenance is always OBSERVED"},
        "analysis_id": {"type": "string", "pattern": ID_PATTERN},
        "observed_at": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"},
        "analysis": {
            "type": "object", "additionalProperties": False,
            "required": ["identity", "analyzer", "analyzer_version", "parameters", "seconds"],
            "properties": {
                "identity": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                "analyzer": {"type": "string", "pattern": r"^media-analysis/[a-z]+$"},
                "analyzer_version": {"type": "string"},
                "parameters": {"type": "object", "description": "effective parameters (defaults applied)"},
                "seconds": {"type": "number", "minimum": 0},
            },
        },
        "asset": {
            "type": "object", "additionalProperties": False,
            "required": ["path", "fingerprint", "size"],
            "properties": {"path": {"type": "string"}, "fingerprint": {"type": "string", "pattern": r"^[0-9a-f]{64}$"}, "size": {"type": "integer", "minimum": 0}},
        },
    },
}

ERROR_SCHEMA: Dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["code", "message", "details"],
    "properties": {"code": {"type": "string"}, "message": {"type": "string"}, "details": {"type": "object"},
                   "class": {"type": "string", "enum": ["FATAL", "RETRYABLE", "BLOCKED"],
                             "description": "retry guidance: FATAL never retry unchanged; RETRYABLE bounded retry may succeed; BLOCKED environment / budget must change"}},
}

RESULT_SCHEMA: Dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["analysis_id", "asset_id", "kind", "status", "cache", "usage"],
    "properties": {
        "analysis_id": {"type": ["string", "null"]},
        "asset_id": {"type": ["string", "null"]},
        "kind": {"type": ["string", "null"]},
        "status": {"type": "string", "enum": list(RESULT_STATUSES)},
        "observation": {"$ref": "#/observation"},
        "plan": {"type": "object", "description": "dry-run only: analyzer, capabilities and operations that would run"},
        "error": ERROR_SCHEMA,
        "error_kind": {"type": "string"},
        "error_class": {"type": "string", "enum": ["FATAL", "RETRYABLE", "BLOCKED"]},
        "cache": {"type": "object", "additionalProperties": False, "required": ["status", "policy", "key"],
                  "properties": {"status": {"type": "string", "enum": list(CACHE_STATUSES)}, "policy": {"type": "string", "enum": list(CACHE_POLICIES)},
                                 "key": {"type": ["string", "null"], "pattern": r"^[0-9a-f]{64}$"}}},
        "usage": {"type": "object", "additionalProperties": False, "required": ["analyzer_calls", "seconds", "operations"],
                  "properties": {"analyzer_calls": {"type": "integer", "minimum": 0, "maximum": 1}, "seconds": {"type": "number", "minimum": 0},
                                 "operations": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["executable", "purpose"],
                                                                            "properties": {"executable": {"type": "string", "enum": ["ffprobe", "ffmpeg"]}, "purpose": {"type": "string"}}}}}},
    },
}

RESPONSE_SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "media-analysis response", "version": RESPONSE_SCHEMA_VERSION,
    "description": "stdout document of `media-analysis run ... --json` (also used for `analyze` / `probe` with --json)",
    "type": "object", "additionalProperties": False,
    "required": ["schema", "skill", "status", "results", "observations", "usage", "budget", "warnings"],
    "properties": {
        "schema": {"type": "string", "const": f"media-analysis/response@{RESPONSE_SCHEMA_VERSION}"},
        "skill": {"type": "object", "additionalProperties": False, "required": ["id", "version"], "properties": {"id": {"type": "string", "const": "media-analysis"}, "version": {"type": "string"}}},
        "status": {"type": "string", "enum": list(RESPONSE_STATUSES), "description": "ok: every result ok; partial: some; error: none (or the batch itself was rejected)"},
        "dry_run": {"type": "boolean"},
        "results": {"type": "array", "items": {"$ref": "#/result"}},
        "observations": {"type": "array", "items": {"$ref": "#/observation"}, "description": "observations of the ok results, in request order"},
        "usage": {"type": "object", "additionalProperties": False, "required": ["analyzer_calls", "cache_hits", "seconds"],
                  "properties": {"analyzer_calls": {"type": "integer", "minimum": 0}, "cache_hits": {"type": "integer", "minimum": 0}, "seconds": {"type": "number", "minimum": 0}}},
        "budget": {"type": "object", "additionalProperties": False, "required": ["calls", "seconds", "budget"],
                   "properties": {"calls": {"type": "integer"}, "seconds": {"type": "number"}, "budget": BUDGET_SCHEMA}},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "error": ERROR_SCHEMA,
        "error_kind": {"type": "string"},
        "error_class": {"type": "string", "enum": ["FATAL", "RETRYABLE", "BLOCKED"]},
    },
}


# ---- minimal validator
def validate(doc: Any, schema: Dict[str, Any], refs: Dict[str, Dict[str, Any]] = None, path: str = "$") -> List[str]:
    """Return a list of violations (empty = valid). `refs` maps "#/name" to sub-schemas."""
    refs = refs or {}
    if "$ref" in schema:
        return validate(doc, refs[schema["$ref"]], refs, path)
    errs: List[str] = []
    types = schema.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        if not any(_is_type(doc, t) for t in allowed):
            return [f"{path}: expected {allowed}, got {type(doc).__name__}"]
    if "const" in schema and doc != schema["const"]:
        errs.append(f"{path}: expected {schema['const']!r}")
    if "enum" in schema and doc not in schema["enum"]:
        errs.append(f"{path}: {doc!r} not in {schema['enum']}")
    if isinstance(doc, str):
        if "pattern" in schema and not re.match(schema["pattern"], doc):
            errs.append(f"{path}: {doc!r} does not match {schema['pattern']}")
        if "minLength" in schema and len(doc) < schema["minLength"]:
            errs.append(f"{path}: shorter than {schema['minLength']}")
    if isinstance(doc, (int, float)) and not isinstance(doc, bool):
        if "minimum" in schema and doc < schema["minimum"]:
            errs.append(f"{path}: {doc} < {schema['minimum']}")
        if "exclusiveMinimum" in schema and doc <= schema["exclusiveMinimum"]:
            errs.append(f"{path}: {doc} <= {schema['exclusiveMinimum']}")
        if "maximum" in schema and doc > schema["maximum"]:
            errs.append(f"{path}: {doc} > {schema['maximum']}")
    if isinstance(doc, dict):
        props = schema.get("properties", {})
        for r in schema.get("required", []):
            if r not in doc:
                errs.append(f"{path}: missing required {r!r}")
        for k, v in doc.items():
            if k in props:
                errs += validate(v, props[k], refs, f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}: unexpected property {k!r}")
    if isinstance(doc, list):
        if "minItems" in schema and len(doc) < schema["minItems"]:
            errs.append(f"{path}: fewer than {schema['minItems']} items")
        if "items" in schema:
            for i, v in enumerate(doc):
                errs += validate(v, schema["items"], refs, f"{path}[{i}]")
    return errs


def _is_type(doc: Any, t: str) -> bool:
    if t == "object":
        return isinstance(doc, dict)
    if t == "array":
        return isinstance(doc, list)
    if t == "string":
        return isinstance(doc, str)
    if t == "integer":
        return isinstance(doc, int) and not isinstance(doc, bool)
    if t == "number":
        return isinstance(doc, (int, float)) and not isinstance(doc, bool)
    if t == "boolean":
        return isinstance(doc, bool)
    if t == "null":
        return doc is None
    raise ValueError(f"unknown schema type {t}")


def contract_refs(kinds: List[str], parameter_schemas: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {"#/request": request_schema(kinds, parameter_schemas), "#/observation": OBSERVATION_SCHEMA, "#/result": RESULT_SCHEMA}
