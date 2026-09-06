"""media-analysis CLI.

stdout contract: with --json, stdout carries exactly one response document (schemas.RESPONSE_SCHEMA) and nothing
else, on success and on failure alike; without --json, stdout is human-readable text. stderr is diagnostics only.
Exit code: 0 when every result is ok, otherwise the exit code of the first error (errors.EXIT_CODES)."""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
from typing import Any, Dict, List, Optional

from . import PACKAGE_NAME, SKILL_ID, VERSION
from .budget import Budget
from .cache import ObservationCache
from .capabilities import detect
from .contract import ANALYSIS_KINDS, PARAMETER_SCHEMAS, skill_contract
from .contract_check import check_contract
from .engine import AnalysisEngine, exit_code_for
from .errors import AnalysisError
from .registry import default_registry
from .schemas import CACHE_POLICIES, contract_refs
from .security import PathPolicy


def _add_common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--json", action="store_true", help="machine-readable JSON on stdout (exactly one document)")


def _add_engine_opts(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--dry-run", action="store_true", help="validate and show the planned analyzer / operations; run nothing")
    ap.add_argument("--timeout", type=float, help="seconds per analyzer execution (default 600)")
    ap.add_argument("--max-analysis-calls", type=int, help="budget: analyzer executions allowed in this invocation")
    ap.add_argument("--max-total-seconds", type=float, help="budget: cumulative analyzer seconds allowed in this invocation")
    ap.add_argument("--cache-dir", help="observation cache directory (inside the workspace); omit to disable caching")
    ap.add_argument("--cache-policy", choices=list(CACHE_POLICIES), default="use", help="use (read+write) | bypass (neither) | only (read; CACHE_MISS otherwise)")
    ap.add_argument("--workspace", help="directory writes are confined to (default: current directory)")
    ap.add_argument("--allowed-input", action="append", help="restrict inputs to this root (repeatable); default: any readable regular file")


def _parse_param(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _params(items: Optional[List[str]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for it in items or []:
        if "=" not in it:
            raise AnalysisError("INVALID_INPUT", f"--param expects key=value, got {it!r}")
        k, v = it.split("=", 1)
        out[k.strip()] = _parse_param(v.strip())
    return out


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="media-analysis", description=f"{PACKAGE_NAME} {VERSION}: deterministic media observation (not an AI agent)")
    ap.add_argument("--version", action="version", version=f"{PACKAGE_NAME} {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="media_probe of one file")
    p.add_argument("input")
    p.add_argument("--asset-id", default="asset-1")
    _add_engine_opts(p)
    _add_common(p)

    a = sub.add_parser("analyze", help="run one or more analysis kinds on one file")
    a.add_argument("input")
    a.add_argument("--kind", action="append", required=True, choices=list(ANALYSIS_KINDS), help="analysis kind (repeatable)")
    a.add_argument("--param", action="append", metavar="KEY=VALUE", help="analysis parameter (JSON value), repeatable; applied to every --kind that accepts it")
    a.add_argument("--asset-id", default="asset-1")
    a.add_argument("--analysis-id", help="caller's analysis id (single --kind only); default derived from the analysis identity")
    a.add_argument("--round", type=int, default=3, help="decimal places for numbers in the output (0-9, default 3)")
    _add_engine_opts(a)
    _add_common(a)

    r = sub.add_parser("run", help="canonical machine interface: run an AnalysisRequest document (JSON file, or - for stdin)")
    r.add_argument("request", help="path to a request document (one request, a list, or {requests, budget}), or - for stdin")
    _add_engine_opts(r)
    _add_common(r)

    d = sub.add_parser("doctor", help="diagnose the environment against the Skill contract")
    d.add_argument("--cache-dir", help="also check that this cache directory is inside the workspace and writable")
    d.add_argument("--workspace", help="workspace to check (default: current directory)")
    d.add_argument("--allowed-input", action="append", help="input roots to report")
    _add_common(d)

    c = sub.add_parser("contract", help="print the Skill / Tool contract, or check a saved contract against this installation")
    c.add_argument("--check", metavar="FILE", help="validate a contract document (file, or - for stdin) against the implementation; exit 0 only when identical")
    _add_common(c)

    k = sub.add_parser("conformance", help="run AI-video-production-OS SKILL_SPEC.md section 8 self-checks against this installation")
    _add_common(k)
    return ap


def _engine(args: argparse.Namespace) -> AnalysisEngine:
    policy = PathPolicy(workspace=getattr(args, "workspace", None), allowed_input_roots=getattr(args, "allowed_input", None))
    budget = Budget(max_analysis_calls=getattr(args, "max_analysis_calls", None), timeout=getattr(args, "timeout", None) or 600.0,
                    max_total_seconds=getattr(args, "max_total_seconds", None))
    cache = ObservationCache(args.cache_dir, policy) if getattr(args, "cache_dir", None) else None
    return AnalysisEngine(policy=policy, cache=cache, budget=budget)


def _document(args: argparse.Namespace) -> Any:
    """Build the request document for the engine from the CLI arguments."""
    common = {"timeout": args.timeout, "cache_policy": args.cache_policy}
    if args.cmd == "probe":
        return {"asset_id": args.asset_id, "input": args.input, "kind": "media_probe", **common}
    if args.cmd == "analyze":
        if args.analysis_id and len(args.kind) > 1:
            raise AnalysisError("INVALID_INPUT", "--analysis-id can only be used with a single --kind")
        params = _params(args.param)
        unknown = [k for k in params if not any(k in PARAMETER_SCHEMAS[kind] for kind in args.kind)]
        if unknown:
            raise AnalysisError("INVALID_INPUT", "parameters not accepted by the requested kind(s)", {"fields": unknown})
        return [{"asset_id": args.asset_id, "input": args.input, "kind": kind, "parameters": {k: v for k, v in params.items() if k in PARAMETER_SCHEMAS[kind]},
                 "analysis_id": args.analysis_id, "output_policy": {"round": args.round}, **common} for kind in args.kind]
    if args.cmd == "run":
        try:
            text = sys.stdin.read() if args.request == "-" else open(args.request, "r", encoding="utf-8").read()
        except OSError as e:
            raise AnalysisError("FILE_NOT_FOUND", f"cannot read request document: {e}")
        try:
            return json.loads(text)
        except ValueError as e:
            raise AnalysisError("INVALID_INPUT", f"request document is not valid JSON: {e}")
    raise AnalysisError("INVALID_INPUT", f"unknown command {args.cmd}")


# ---- human rendering
def _human_result(res: Dict[str, Any]) -> str:
    if res["status"] == "error":
        e = res["error"]
        return f"[{res.get('kind') or '-'}] ERROR {e['code']}: {e['message']}" + (f" {json.dumps(e['details'])}" if e["details"] else "")
    if "plan" in res:
        plan = res["plan"]
        lines = [f"[dry-run] {plan['request']['kind']} on {plan['input']}", f"  analyzer: {plan['analyzer']['tool_id']}@{plan['analyzer']['version']}",
                 "  capabilities: " + ", ".join(f"{k}={v}" for k, v in plan["capabilities"].items()), f"  parameters: {json.dumps(plan['parameters'])}",
                 "  operations:"]
        lines += [f"    - {op['executable']}: {op['purpose']}" for op in plan["operations"]]
        lines.append("  executable: " + ("yes" if plan["executable"] else "NO (missing: " + ", ".join(plan["missing_capabilities"]) + ")"))
        return "\n".join(lines)
    o = res["observation"]
    lines = [f"[{o['kind']}] asset={o['asset_id']} source={o['source']} cache={res['cache']['status']} id={o['id']}"]
    lines += ["  " + ln for ln in json.dumps(o["data"], indent=2, ensure_ascii=False).splitlines()]
    return "\n".join(lines)


# ---- doctor
def doctor_report(workspace: Optional[str] = None, cache_dir: Optional[str] = None, allowed_input: Optional[List[str]] = None) -> Dict[str, Any]:
    """Diagnose this environment against the contract. Reports only what was actually checked; a check that could
    not run says so. `status` is ok when every declared tool is usable here, degraded otherwise, fail when the
    package itself is inconsistent (contract / registry / path policy)."""
    checks: Dict[str, Any] = {}
    problems: List[str] = []
    caps = detect()
    checks["python"] = {"status": "ok", "version": platform.python_version(), "implementation": platform.python_implementation(), "platform": platform.system()}
    for name in ("ffmpeg", "ffprobe"):
        c = caps.get(name)
        checks[name] = {"status": "ok" if c.status == "AVAILABLE" else "missing", "version": c.version, "path": c.path, "detail": c.detail}
    for name in sorted(caps.items):
        if name.startswith("filter:"):
            c = caps.get(name)
            checks[name] = {"status": "ok" if c.status == "AVAILABLE" else "missing", "detail": c.detail}
    try:
        registry = default_registry()
        rows = registry.availability(caps)
        checks["analyzer_registry"] = {"status": "ok", "analyzers": rows}
    except Exception as e:  # registry/contract mismatch is a packaging defect, reported not raised
        rows = []
        checks["analyzer_registry"] = {"status": "fail", "detail": str(e)}
        problems.append("analyzer registry: " + str(e))
    try:
        contract = skill_contract()
        refs = contract_refs(contract["analysis_kinds"], {})
        issues = check_contract(json.loads(json.dumps(contract, allow_nan=False)))   # the printed document must round-trip and self-validate
        implemented = {r["tool_id"] for r in rows}
        if {t["tool_id"] for t in contract["tools"]} != implemented:
            issues.append(f"declared tools != registry availability rows {sorted(implemented)}")
        checks["contract"] = {"status": "ok" if not issues else "fail", "schema": contract["schema"], "tools": len(contract["tools"]),
                              "kinds": len(contract["analysis_kinds"]), "issues": issues, "schemas": sorted(contract["schemas"]), "refs": sorted(refs)}
        problems += ["contract: " + i for i in issues]
    except Exception as e:
        checks["contract"] = {"status": "fail", "detail": str(e)}
        problems.append("contract: " + str(e))
    try:
        policy = PathPolicy(workspace=workspace, allowed_input_roots=allowed_input)
        checks["path_policy"] = {"status": "ok", "workspace": str(policy.workspace), "workspace_exists": policy.workspace.is_dir(),
                                 "allowed_input_roots": [str(r) for r in policy.allowed_input_roots] if policy.allowed_input_roots else None,
                                 "input_rule": "any readable regular file" if not policy.allowed_input_roots else "regular files under allowed roots (symlinks resolved)"}
        if not policy.workspace.is_dir():
            checks["path_policy"]["status"] = "fail"
            problems.append("path policy: workspace does not exist")
    except AnalysisError as e:
        policy = None
        checks["path_policy"] = {"status": "fail", "detail": e.message}
        problems.append("path policy: " + e.message)
    if policy is not None:
        target = cache_dir or ".media-analysis-cache"
        try:
            resolved = policy.resolve_write_dir(target)
            probe_dir = resolved if resolved.exists() else policy.workspace
            writable = os.access(str(probe_dir), os.W_OK)
            if writable:
                fd, tmp = tempfile.mkstemp(dir=str(probe_dir), prefix=".doctor-")
                os.close(fd)
                os.unlink(tmp)
            checks["cache"] = {"status": "ok" if writable else "fail", "directory": str(resolved), "exists": resolved.exists(), "writable": writable,
                               "checked": "requested directory" if cache_dir else "default directory name inside the workspace"}
            if not writable:
                problems.append("cache: directory is not writable")
        except (AnalysisError, OSError) as e:
            checks["cache"] = {"status": "fail", "directory": target, "detail": getattr(e, "message", str(e))}
            problems.append("cache: " + getattr(e, "message", str(e)))
    unavailable = [r["tool_id"] for r in rows if r["status"] != "available"]
    by_tool = {r["tool_id"]: r for r in rows}
    capabilities = []
    for entry in skill_contract()["provides"]:          # SKILL_SPEC.md #7: AVAILABLE / MISSING per declared Capability
        row = by_tool.get(entry["tool_id"])
        missing = list(row["missing_capabilities"]) if row else ["analyzer not registered"]
        capabilities.append({"id": entry["id"], "tool_id": entry["tool_id"], "kind": entry["kind"], "lifecycle": entry["lifecycle"],
                             "status": "AVAILABLE" if row and not missing else "MISSING", "missing": missing})
    status = "fail" if problems else ("degraded" if unavailable else "ok")
    return {"schema": "media-analysis/doctor@1", "skill": {"id": SKILL_ID, "version": VERSION}, "status": status, "checks": checks,
            "capabilities": capabilities, "unavailable_tools": unavailable, "problems": problems, "secrets_shown": False}


def _print_doctor(doc: Dict[str, Any]) -> None:
    ch = doc["checks"]
    print(f"media-analysis {doc['skill']['version']}: {doc['status']}")
    print(f"python: {ch['python']['version']} ({ch['python']['platform']})")
    for name in ("ffmpeg", "ffprobe"):
        c = ch[name]
        print(f"{name}: {'available' if c['status'] == 'ok' else 'missing'}" + (f"  version: {c['version']}  path: {c['path']}" if c["status"] == "ok" else f"  ({c['detail']})"))
    for name in sorted(k for k in ch if k.startswith("filter:")):
        print(f"{name}: {'available' if ch[name]['status'] == 'ok' else 'missing'}")
    print(f"contract: {ch['contract']['status']}" + (f"  tools={ch['contract']['tools']} kinds={ch['contract']['kinds']}" if ch["contract"]["status"] == "ok" else f"  {ch['contract'].get('issues') or ch['contract'].get('detail')}"))
    print(f"path policy: {ch['path_policy']['status']}  workspace={ch['path_policy'].get('workspace')}  inputs={ch['path_policy'].get('input_rule')}")
    if "cache" in ch:
        print(f"cache: {ch['cache']['status']}  {ch['cache'].get('directory')}" + ("" if ch["cache"]["status"] == "ok" else f"  ({ch['cache'].get('detail', 'not writable')})"))
    print("capabilities:")
    for c in doc["capabilities"]:
        print(f"  {c['id']}: {c['status']}  ({c['tool_id']}, {c['lifecycle']})" + (f"  missing: {', '.join(c['missing'])}" if c["missing"] else ""))
    print("analyzers:")
    for r in ch["analyzer_registry"].get("analyzers", []):
        extra = "" if r["status"] == "available" else "  missing: " + ", ".join(r["missing_capabilities"])
        print(f"  {r['tool_id']}@{r['version']}: {r['status']}  kinds: {', '.join(r['supported_kinds'])}{extra}")
    for p in doc["problems"]:
        print(f"problem: {p}")


def _conformance(as_json: bool) -> int:
    from .conformance import run_conformance
    doc = run_conformance()
    if as_json:
        print(json.dumps(doc, indent=2))
    else:
        print(f"conformance ({doc['spec']}): {doc['status']}")
        for c in doc["checks"]:
            print(f"  {c['check']}: {c['status']}  {c['detail']}")
    return 0 if doc["status"] == "ok" else 1


def _contract(as_json: bool, check: Optional[str] = None) -> int:
    if check is not None:
        try:
            text = sys.stdin.read() if check == "-" else open(check, "r", encoding="utf-8").read()
            saved = json.loads(text)
        except (OSError, ValueError) as e:
            problems = [f"contract: cannot read document: {e}"]
        else:
            problems = check_contract(saved)
        report = {"schema": "media-analysis/contract-check@1", "skill": {"id": SKILL_ID, "version": VERSION}, "status": "ok" if not problems else "drift",
                  "supported_schemas": ["media-analysis/contract@1"], "problems": problems}
        if as_json:
            print(json.dumps(report, indent=2))
        else:
            print(f"contract check: {report['status']}")
            for p in problems:
                print(f"  - {p}")
        return 0 if not problems else 1
    doc = skill_contract()
    if as_json:
        print(json.dumps(doc, indent=2))
    else:
        print(f"{doc['skill_id']} {doc['version']} ({doc['package']}) — {doc['description']}")
        for t in doc["tools"]:
            print(f"  {t['tool_id']}: kinds={', '.join(t['kinds'])}  capabilities={', '.join(t['required_capabilities'])}")
        print("canonical invocation: " + " ".join(doc["execution"]["canonical_invocation"]))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    if args.cmd == "doctor":
        doc = doctor_report(args.workspace, args.cache_dir, args.allowed_input)
        if as_json:
            print(json.dumps(doc, indent=2))
        else:
            _print_doctor(doc)
        return 0 if doc["status"] != "fail" else 1
    if args.cmd == "contract":
        return _contract(as_json, args.check)
    if args.cmd == "conformance":
        return _conformance(as_json)
    try:
        engine = _engine(args)
        document = _document(args)
    except AnalysisError as e:
        response = AnalysisEngine.error_response(e)
    else:
        response = engine.run(document, dry_run=args.dry_run)
    code = exit_code_for(response)
    if as_json:
        print(json.dumps(response, indent=2, ensure_ascii=False, allow_nan=False))
    else:
        if response.get("error"):
            sys.stderr.write(f"error [{response['error']['code']}]: {response['error']['message']}" + (f" {json.dumps(response['error']['details'])}" if response["error"]["details"] else "") + "\n")
        for res in response["results"]:
            (print if res["status"] == "ok" else lambda t: sys.stderr.write(t + "\n"))(_human_result(res))
    return code


if __name__ == "__main__":
    sys.exit(main())
