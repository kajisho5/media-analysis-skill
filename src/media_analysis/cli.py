"""media-analysis CLI.

stdout contract: with --json, stdout carries exactly one response document (schemas.RESPONSE_SCHEMA) and nothing
else, on success and on failure alike, always encoded as UTF-8 regardless of the console / pipe encoding (a Japanese
file name on a cp932 or cp1252 pipe must not break the protocol); without --json, stdout is human-readable text.
stderr is diagnostics only. Exit code: 0 when every result is ok, otherwise the exit code of the first error
(errors.EXIT_CODES)."""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
from typing import Any, Dict, List, Optional

from . import PACKAGE_NAME, SKILL_ID, VERSION
from .analyzers.events import events_status
from .analyzers.vad import vad_status
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


def emit_json(doc: Any) -> None:
    """Write one JSON document to stdout as UTF-8 bytes. Bypasses the text layer so the platform / pipe encoding
    (cp1252 on Windows pipes, cp932 on Japanese consoles) can never raise or mangle non-ASCII paths."""
    data = (json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    out = getattr(sys.stdout, "buffer", None)
    if out is not None:
        sys.stdout.flush()
        out.write(data)
        out.flush()
    else:                                   # a replaced text stream (tests, embedding): best effort, never crash
        sys.stdout.write(data.decode("utf-8"))
        sys.stdout.flush()


def _read_stdin_utf8() -> str:
    """Request documents are UTF-8 by contract, whatever the console encoding says."""
    buf = getattr(sys.stdin, "buffer", None)
    return buf.read().decode("utf-8") if buf is not None else sys.stdin.read()


def _tolerant_text_streams() -> None:
    """Human-readable output must never crash on a character the console cannot encode."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (ValueError, TypeError):
                pass


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

    st = sub.add_parser("style", help="editing-style profile of an edit (edit_style), saved as JSON, and a metric-by-metric diff of two profiles")
    stsub = st.add_subparsers(dest="style_cmd", required=True)
    sp = stsub.add_parser("profile", help="measure edit_style of one file; --out saves the Observation (profile + provenance)")
    sp.add_argument("input")
    sp.add_argument("--out", help="file to save the profile Observation to (inside the workspace)")
    sp.add_argument("--param", action="append", metavar="KEY=VALUE", help="edit_style parameter (JSON value), repeatable")
    sp.add_argument("--asset-id", default="asset-1")
    _add_engine_opts(sp)
    _add_common(sp)
    sd = stsub.add_parser("diff", help="compare two saved profiles: candidate minus reference, per metric")
    sd.add_argument("reference", help="profile of the reference edit (a saved profile, Observation or response document)")
    sd.add_argument("candidate", help="profile of the edit to compare")
    _add_common(sd)

    lb = sub.add_parser("library", help="index a folder of clips with measured metadata and tags, and search the index (ADR-031)")
    lbsub = lb.add_subparsers(dest="library_cmd", required=True)
    li = lbsub.add_parser("index", help="scan a folder; only new or changed files are analysed")
    li.add_argument("folder", help="library folder (read only; every clip under it is an allowed input)")
    li.add_argument("--index", required=True, help="index file to create or update (inside the workspace)")
    li.add_argument("--tags", help="external tags for clips: {\"<relative path>\": [tags]} or [{path, tags, source}]")
    li.add_argument("--timeout", type=float, help="seconds per analyzer execution (default 600)")
    li.add_argument("--cache-dir", help="observation cache directory (inside the workspace); omit to disable caching")
    li.add_argument("--workspace", help="directory writes are confined to (default: current directory)")
    _add_common(li)
    ls_ = lbsub.add_parser("search", help="search an index by keyword, tag and condition")
    ls_.add_argument("--index", required=True, help="index file written by `library index`")
    ls_.add_argument("--query", action="append", help="keyword(s); each must match a tag or a word of the clip's path (repeatable)")
    ls_.add_argument("--tag", action="append", help="tag the clip must carry (repeatable)")
    ls_.add_argument("--min-duration", type=float)
    ls_.add_argument("--max-duration", type=float)
    ls_.add_argument("--orientation", choices=["landscape", "portrait", "square"])
    ls_.add_argument("--min-height", type=int, help="minimum video height in pixels")
    ls_.add_argument("--has-audio", dest="has_audio", action="store_const", const=True, default=None)
    ls_.add_argument("--no-audio", dest="has_audio", action="store_const", const=False)
    ls_.add_argument("--scene-type", choices=["single_shot", "multi_shot", "audio_only"])
    ls_.add_argument("--limit", type=int)
    _add_common(ls_)

    hl = sub.add_parser("highlights", help="highlight candidates of a long recording on sentence boundaries, with reasons (ADR-032)")
    hl.add_argument("input")
    hl.add_argument("--transcript", required=True, help="transcript JSON: {segments: [{start, end, text, words?, speaker_id?}]}")
    hl.add_argument("--signals", help="applause / laughter spans: {applause: [{start, end}], laughter: [...]}, or an audio_events result")
    hl.add_argument("--keyword", action="append", help="keyword or phrase counted in each span (repeatable)")
    hl.add_argument("--stream", type=int, default=0, help="audio stream ordinal (default 0)")
    hl.add_argument("--window", type=float, default=0.5, help="energy window in seconds (default 0.5)")
    hl.add_argument("--min-length", type=float, default=15.0, help="shortest candidate in seconds (default 15)")
    hl.add_argument("--max-length", type=float, default=60.0, help="longest candidate in seconds (default 60)")
    hl.add_argument("--reaction-window", type=float, default=3.0, help="seconds after a span in which applause / laughter still counts (default 3)")
    hl.add_argument("--top", type=int, default=10, help="number of candidates (default 10)")
    hl.add_argument("--timeout", type=float, help="seconds for the measurement (default 600)")
    hl.add_argument("--allowed-input", action="append", help="restrict inputs to this root (repeatable)")
    _add_common(hl)

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
    if args.cmd == "style":
        return {"asset_id": args.asset_id, "input": args.input, "kind": "edit_style", "parameters": _params(args.param), **common}
    if args.cmd == "run":
        try:
            text = _read_stdin_utf8() if args.request == "-" else open(args.request, "r", encoding="utf-8").read()
        except OSError as e:
            raise AnalysisError("FILE_NOT_FOUND", f"cannot read request document: {e}")
        except UnicodeDecodeError as e:
            raise AnalysisError("INVALID_INPUT", f"request document is not UTF-8: {e}")
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
    vad = vad_status()      # optional extra: missing it leaves speech_regions UNKNOWN, it does not degrade the doctor status
    checks["extra:vad"] = {"status": "ok" if vad["available"] else "missing", "optional": True, "kinds": ["speech_regions"],
                           "runtime": vad["runtime"], "model": vad["model"], "detail": vad["detail"]}
    ev = events_status()    # optional extra: missing it leaves audio_events UNKNOWN (ADR-036)
    checks["extra:events"] = {"status": "ok" if ev["available"] else "missing", "optional": True, "kinds": ["audio_events"],
                              "runtime": ev["runtime"], "model": ev["model"], "detail": ev["detail"]}
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
        emit_json(doc)
    else:
        print(f"conformance ({doc['spec']}): {doc['status']}")
        for c in doc["checks"]:
            print(f"  {c['check']}: {c['status']}  {c['detail']}")
    return 0 if doc["status"] == "ok" else 1


def _contract(as_json: bool, check: Optional[str] = None) -> int:
    if check is not None:
        try:
            text = _read_stdin_utf8() if check == "-" else open(check, "r", encoding="utf-8").read()
            saved = json.loads(text)
        except (OSError, ValueError) as e:
            problems = [f"contract: cannot read document: {e}"]
        else:
            problems = check_contract(saved)
        report = {"schema": "media-analysis/contract-check@1", "skill": {"id": SKILL_ID, "version": VERSION}, "status": "ok" if not problems else "drift",
                  "supported_schemas": ["media-analysis/contract@1"], "problems": problems}
        if as_json:
            emit_json(report)
        else:
            print(f"contract check: {report['status']}")
            for p in problems:
                print(f"  - {p}")
        return 0 if not problems else 1
    doc = skill_contract()
    if as_json:
        emit_json(doc)
    else:
        print(f"{doc['skill_id']} {doc['version']} ({doc['package']}) — {doc['description']}")
        for t in doc["tools"]:
            print(f"  {t['tool_id']}: kinds={', '.join(t['kinds'])}  capabilities={', '.join(t['required_capabilities'])}")
        print("canonical invocation: " + " ".join(doc["execution"]["canonical_invocation"]))
    return 0


def _load_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as e:
        raise AnalysisError("FILE_NOT_FOUND", f"cannot read {path}: {e}")
    except ValueError as e:
        raise AnalysisError("INVALID_INPUT", f"{path} is not valid UTF-8 JSON: {e}")


def _save_json(policy: PathPolicy, path: str, doc: Any) -> str:
    """Write one JSON document to a file inside the workspace (atomic replace)."""
    target = os.path.abspath(path if os.path.isabs(path) else os.path.join(str(policy.workspace), path))
    folder = policy.resolve_write_dir(os.path.dirname(target))
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / os.path.basename(target)
    fd, tmp = tempfile.mkstemp(dir=str(folder), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False, allow_nan=False)
            fh.write("\n")
        os.replace(tmp, str(final))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return str(final)


def _style_diff(args: argparse.Namespace, as_json: bool) -> int:
    from .analyzers.style import diff_profiles
    try:
        doc = diff_profiles(_load_json(args.reference), _load_json(args.candidate))
    except AnalysisError as e:
        response = AnalysisEngine.error_response(e)
        if as_json:
            emit_json(response)
        else:
            sys.stderr.write(f"error [{e.code}]: {e.message}\n")
        return exit_code_for(response)
    if as_json:
        emit_json(doc)
    else:
        print(f"edit_style diff ({doc['direction']}): {doc['compared']} metrics compared, {len(doc['different'])} differ")
        for it in doc["items"]:
            if it["status"] != "compared":
                print(f"  {it['metric']}: unavailable in {', '.join(it['missing_in'])}")
            elif "difference" in it:
                print(f"  {it['metric']}: {it['reference']} -> {it['candidate']} ({it['difference']:+g})")
            else:
                print(f"  {it['metric']}: {it['reference']} -> {it['candidate']}" + ("" if it["equal"] else "  (differs)"))
    return 0


def _fail(e: AnalysisError, as_json: bool) -> int:
    response = AnalysisEngine.error_response(e)
    if as_json:
        emit_json(response)
    else:
        sys.stderr.write(f"error [{e.code}]: {e.message}" + (f" {json.dumps(e.details)}" if e.details else "") + "\n")
    return exit_code_for(response)


def _library(args: argparse.Namespace, as_json: bool) -> int:
    from pathlib import Path
    from . import library
    try:
        if args.library_cmd == "index":
            policy = PathPolicy(workspace=args.workspace, allowed_input_roots=[args.folder])
            cache = ObservationCache(args.cache_dir, policy) if args.cache_dir else None
            engine = AnalysisEngine(policy=policy, cache=cache, budget=Budget(timeout=args.timeout or 600.0))
            target = Path(args.index) if os.path.isabs(args.index) else policy.workspace / args.index
            previous = library.load_index(target)
            external = library.parse_external_tags(_load_json(args.tags)) if args.tags else None
            out = library.build_index(engine, args.folder, previous, external)
            written = library.write_index(policy, args.index, out["index"])
            doc = {"schema": "media-analysis/library-scan@1", "index": str(written), "root": out["index"]["root"],
                   "clip_count": out["index"]["clip_count"], "report": out["report"], "analyzer_executions": len(engine.executions)}
            if as_json:
                emit_json(doc)
            else:
                r = doc["report"]
                print(f"library {doc['root']}: {doc['clip_count']} clips -> {doc['index']}")
                print(f"  analysed {len(r['analysed'])}, reused {len(r['reused'])}, removed {len(r['removed'])}, failed {len(r['failed'])}")
                for f in r["failed"]:
                    print(f"  failed: {f['path']} [{f['code']}] {f['message']}")
            return 0
        index = library.load_index(Path(args.index))
        if index is None:
            raise AnalysisError("FILE_NOT_FOUND", f"library index not found: {args.index}")
        doc = library.search(index, args.query, args.tag, min_duration=args.min_duration, max_duration=args.max_duration,
                             orientation=args.orientation, min_height=args.min_height, has_audio=args.has_audio,
                             scene_type=args.scene_type, limit=args.limit)
    except AnalysisError as e:
        return _fail(e, as_json)
    if as_json:
        emit_json(doc)
    else:
        print(f"{doc['result_count']} clips")
        for h in doc["results"]:
            print(f"  {h['path']}  {h['duration']}s  @{h['representative_time']}s  [{', '.join(h['tags'] or [])}]")
    return 0


def _highlights(args: argparse.Namespace, as_json: bool) -> int:
    from .highlights import highlight_candidates
    try:
        path = PathPolicy(allowed_input_roots=args.allowed_input).resolve_input(args.input)
        doc = highlight_candidates(str(path), detect(), _load_json(args.transcript),
                                   signals=_load_json(args.signals) if args.signals else None, keywords=args.keyword or [],
                                   stream=args.stream, window=args.window, min_length=args.min_length, max_length=args.max_length,
                                   reaction_window=args.reaction_window, top=args.top, timeout=args.timeout or 600.0)
    except AnalysisError as e:
        return _fail(e, as_json)
    if as_json:
        emit_json(doc)
    else:
        print(f"{doc['candidate_count']} highlight candidates from {doc['sentence_count']} sentences ({doc['input']})")
        for c in doc["candidates"]:
            print(f"  #{c['rank']} {c['start']:.2f}-{c['end']:.2f} s ({c['duration']:.1f} s) score {c['score']:.2f}: " + "; ".join(c["reasons"]))
        for n in doc["not_measured"]:
            print(f"  not used: {n['item']} ({n['reason']})")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    _tolerant_text_streams()
    if args.cmd == "doctor":
        doc = doctor_report(args.workspace, args.cache_dir, args.allowed_input)
        if as_json:
            emit_json(doc)
        else:
            _print_doctor(doc)
        return 0 if doc["status"] != "fail" else 1
    if args.cmd == "contract":
        return _contract(as_json, args.check)
    if args.cmd == "conformance":
        return _conformance(as_json)
    if args.cmd == "style" and args.style_cmd == "diff":
        return _style_diff(args, as_json)
    if args.cmd == "library":
        return _library(args, as_json)
    if args.cmd == "highlights":
        return _highlights(args, as_json)
    try:
        engine = _engine(args)
        document = _document(args)
    except AnalysisError as e:
        response = AnalysisEngine.error_response(e)
    else:
        response = engine.run(document, dry_run=args.dry_run)
        if args.cmd == "style" and args.out and response["status"] == "ok" and not args.dry_run:
            try:
                _save_json(engine.policy, args.out, response["observations"][0])
            except AnalysisError as e:
                response = AnalysisEngine.error_response(e)
            except OSError as e:      # an existing directory, a file in the way, no write permission: still one JSON document
                response = AnalysisEngine.error_response(AnalysisError("PATH_NOT_ALLOWED", f"cannot write {args.out}: {e}", {"out": args.out}))
    code = exit_code_for(response)
    if as_json:
        emit_json(response)
    else:
        if response.get("error"):
            sys.stderr.write(f"error [{response['error']['code']}]: {response['error']['message']}" + (f" {json.dumps(response['error']['details'])}" if response["error"]["details"] else "") + "\n")
        for res in response["results"]:
            (print if res["status"] == "ok" else lambda t: sys.stderr.write(t + "\n"))(_human_result(res))
    return code


if __name__ == "__main__":
    sys.exit(main())
