"""Contract self-validation: does a contract document (as printed by `contract --json`, possibly saved earlier by an
adapter) still describe *this* installation? Detects drift between registry, analyzers, kinds, capabilities, schema
versions and invocation contract. Used by `media-analysis contract --check FILE`, by `doctor`, and by the tests /
contract evals. Pure function over the document; runs no media analysis."""
from __future__ import annotations

from typing import Any, List

from . import SKILL_ID, VERSION
from .contract import ANALYSIS_KINDS, KIND_TO_TOOL, PARAMETER_SCHEMAS, TOOL_CAPABILITIES, skill_contract, tool_id
from .errors import ERROR_CODES, EXIT_CODES
from .schemas import OBSERVATION_SCHEMA_VERSION, REQUEST_SCHEMA_VERSION, RESPONSE_SCHEMA_VERSION

SUPPORTED_CONTRACT_SCHEMAS = ("media-analysis/contract@1",)
CANONICAL_INVOCATION = ["media-analysis", "run", "<request.json | ->", "--json"]


def check_contract(doc: Any) -> List[str]:
    """Problems found when comparing `doc` with the implementation (empty list = the document is exactly this
    installation's contract). Each problem names the drifted item so an adapter can report it."""
    problems: List[str] = []
    if not isinstance(doc, dict):
        return ["contract: document is not an object"]
    schema = doc.get("schema")
    if schema not in SUPPORTED_CONTRACT_SCHEMAS:
        return [f"contract: unsupported schema {schema!r}; supported: {', '.join(SUPPORTED_CONTRACT_SCHEMAS)}"]
    live = skill_contract()

    def eq(path: str, expected: Any, actual: Any) -> None:
        if actual != expected:
            problems.append(f"{path}: expected {expected!r}, got {actual!r}")

    eq("skill_id", SKILL_ID, doc.get("skill_id"))
    eq("version", VERSION, doc.get("version"))
    eq("package", live["package"], doc.get("package"))
    eq("provenance", "OBSERVED", doc.get("provenance"))
    eq("observation_source_format", live["observation_source_format"], doc.get("observation_source_format"))
    eq("schema_versions", {"contract": "1", "request": REQUEST_SCHEMA_VERSION, "response": RESPONSE_SCHEMA_VERSION, "observation": OBSERVATION_SCHEMA_VERSION},
       doc.get("schema_versions"))
    # kinds and mapping
    kinds = doc.get("analysis_kinds")
    if kinds != list(ANALYSIS_KINDS):
        problems.append(f"analysis_kinds: expected {list(ANALYSIS_KINDS)}, got {kinds!r}")
    expected_map = {k: tool_id(t) for k, t in KIND_TO_TOOL.items()}
    if doc.get("kind_to_tool") != expected_map:
        problems.append(f"kind_to_tool: differs from implementation ({sorted(set((doc.get('kind_to_tool') or {}).items()) ^ set(expected_map.items()))})")
    # tools: exact match against the live registry-derived specs
    tools = doc.get("tools")
    if not isinstance(tools, list):
        problems.append("tools: missing or not a list")
    else:
        declared = {t.get("tool_id"): t for t in tools if isinstance(t, dict)}
        live_tools = {t["tool_id"]: t for t in live["tools"]}
        for missing in sorted(set(live_tools) - set(declared)):
            problems.append(f"tools: implemented tool {missing} is not declared")
        for extra in sorted(set(declared) - set(live_tools)):
            problems.append(f"tools: declared tool {extra} is not implemented")
        for tid in sorted(set(declared) & set(live_tools)):
            d, l = declared[tid], live_tools[tid]
            for field in ("skill_id", "version", "kinds", "required_capabilities", "produces_output", "deterministic", "parameters", "supports", "provenance"):
                if d.get(field) != l.get(field):
                    problems.append(f"tools[{tid}].{field}: expected {l.get(field)!r}, got {d.get(field)!r}")
            if d.get("required_capabilities") != TOOL_CAPABILITIES.get(tid.split("/", 1)[1]):
                problems.append(f"tools[{tid}].required_capabilities: differs from TOOL_CAPABILITIES")
        covered = {k for t in declared.values() for k in (t.get("kinds") or [])}
        if covered != set(ANALYSIS_KINDS):
            problems.append(f"tools: kinds covered by tools {sorted(covered)} != analysis kinds {sorted(ANALYSIS_KINDS)}")
    eq("capability_names", live["capability_names"], doc.get("capability_names"))
    eq("capabilities", live["capabilities"], doc.get("capabilities"))
    # execution / invocation
    ex = doc.get("execution") or {}
    eq("execution.mode", "local_subprocess", ex.get("mode"))
    eq("execution.canonical_invocation", CANONICAL_INVOCATION, ex.get("canonical_invocation"))
    for flag in ("media_processing", "network", "ai"):
        eq(f"execution.{flag}", False, ex.get(flag))
    # schemas and errors
    schemas = doc.get("schemas") or {}
    for name in ("request", "batch", "result", "response", "observation"):
        if name not in schemas:
            problems.append(f"schemas.{name}: missing")
        elif schemas[name] != live["schemas"][name]:
            problems.append(f"schemas.{name}: differs from implementation")
    if (schemas.get("request") or {}).get("x-parameters-by-kind") not in (None, PARAMETER_SCHEMAS) and "schemas.request: differs from implementation" not in problems:
        problems.append("schemas.request.x-parameters-by-kind: differs from PARAMETER_SCHEMAS")
    errs = doc.get("errors") or {}
    eq("errors.codes", list(ERROR_CODES), errs.get("codes"))
    eq("errors.exit_codes", dict(EXIT_CODES), errs.get("exit_codes"))
    for section in ("cache", "budget", "identity"):
        eq(section, live[section], doc.get(section))
    return problems
