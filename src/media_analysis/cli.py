"""media-analysis CLI. stdout carries either machine-readable JSON (--json) or human-readable text, never both.
Errors: with --json a {"error": {...}} document on stdout; otherwise one line on stderr. Exit code = error code."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from . import PACKAGE_NAME, SKILL_ID, VERSION
from .budget import Budget
from .cache import ObservationCache
from .capabilities import detect
from .contract import ANALYSIS_KINDS, PARAMETER_SCHEMAS, skill_contract
from .engine import AnalysisEngine
from .errors import AnalysisError
from .registry import default_registry
from .security import PathPolicy


def _add_common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--json", action="store_true", help="machine-readable JSON on stdout")


def _add_engine_opts(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--dry-run", action="store_true", help="validate and show the planned analyzer / operations; run nothing")
    ap.add_argument("--timeout", type=float, help="seconds per analyzer execution (default 600)")
    ap.add_argument("--max-analysis-calls", type=int, help="budget: analyzer executions allowed in this invocation")
    ap.add_argument("--max-total-seconds", type=float, help="budget: cumulative analyzer seconds allowed in this invocation")
    ap.add_argument("--cache-dir", help="observation cache directory (inside the workspace); omit to disable caching")
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

    r = sub.add_parser("run", help="run a structured AnalysisRequest (JSON file, or - for stdin)")
    r.add_argument("request", help="path to an AnalysisRequest JSON document, or - for stdin")
    _add_engine_opts(r)
    _add_common(r)

    d = sub.add_parser("doctor", help="detect ffmpeg / ffprobe / filters and report analyzer availability")
    _add_common(d)

    c = sub.add_parser("contract", help="print the Skill / Tool contract")
    _add_common(c)
    return ap


def _engine(args: argparse.Namespace) -> AnalysisEngine:
    policy = PathPolicy(workspace=getattr(args, "workspace", None), allowed_input_roots=getattr(args, "allowed_input", None))
    budget = Budget(max_analysis_calls=getattr(args, "max_analysis_calls", None), timeout=getattr(args, "timeout", None) or 600.0,
                    max_total_seconds=getattr(args, "max_total_seconds", None))
    cache = ObservationCache(args.cache_dir, policy) if getattr(args, "cache_dir", None) else None
    return AnalysisEngine(policy=policy, cache=cache, budget=budget)


def _requests(args: argparse.Namespace) -> List[Dict[str, Any]]:
    if args.cmd == "probe":
        return [{"asset_id": args.asset_id, "input": args.input, "kind": "media_probe", "timeout": args.timeout}]
    if args.cmd == "analyze":
        if args.analysis_id and len(args.kind) > 1:
            raise AnalysisError("INVALID_INPUT", "--analysis-id can only be used with a single --kind")
        params = _params(args.param)
        unknown = [k for k in params if not any(k in PARAMETER_SCHEMAS[kind] for kind in args.kind)]
        if unknown:
            raise AnalysisError("INVALID_INPUT", "parameters not accepted by the requested kind(s)", {"fields": unknown})
        reqs = []
        for kind in args.kind:
            applicable = {k: v for k, v in params.items() if k in PARAMETER_SCHEMAS[kind]}
            reqs.append({"asset_id": args.asset_id, "input": args.input, "kind": kind, "parameters": applicable, "analysis_id": args.analysis_id,
                         "timeout": args.timeout, "output_policy": {"round": args.round}})
        return reqs
    if args.cmd == "run":
        text = sys.stdin.read() if args.request == "-" else open(args.request, "r", encoding="utf-8").read()
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise AnalysisError("INVALID_INPUT", f"request is not valid JSON: {e}")
        return doc if isinstance(doc, list) else [doc]
    raise AnalysisError("INVALID_INPUT", f"unknown command {args.cmd}")


# ---- human rendering
def _human_observation(res: Dict[str, Any]) -> str:
    o = res["observation"]
    lines = [f"[{o['kind']}] asset={o['asset_id']} source={o['source']} cache={res['cache']} id={o['id']}"]
    lines += ["  " + ln for ln in json.dumps(o["data"], indent=2, ensure_ascii=False).splitlines()]
    return "\n".join(lines)


def _human_plan(plan: Dict[str, Any]) -> str:
    lines = [f"[dry-run] {plan['request']['kind']} on {plan['input']}", f"  analyzer: {plan['analyzer']['tool_id']}@{plan['analyzer']['version']}",
             "  capabilities: " + ", ".join(f"{k}={v}" for k, v in plan["capabilities"].items()), f"  parameters: {json.dumps(plan['parameters'])}",
             "  operations:"]
    lines += [f"    - {op['executable']}: {op['purpose']}" for op in plan["operations"]]
    lines.append("  executable: " + ("yes" if plan["executable"] else "NO (missing: " + ", ".join(plan["missing_capabilities"]) + ")"))
    return "\n".join(lines)


def _doctor(as_json: bool) -> int:
    caps = detect()
    rows = default_registry().availability(caps)
    doc = {"skill": SKILL_ID, "version": VERSION, "capabilities": caps.to_dict(), "analyzers": rows}
    if as_json:
        print(json.dumps(doc, indent=2))
    else:
        for name in ("ffmpeg", "ffprobe"):
            c = caps.get(name)
            print(f"{name}: {c.status.lower()}" + (f"  version: {c.version}  path: {c.path}" if c.status == "AVAILABLE" else f"  ({c.detail})"))
        for name in sorted(caps.items):
            if name.startswith("filter:"):
                print(f"{name}: {caps.get(name).status.lower()}")
        print("analyzers:")
        for r in rows:
            extra = "" if r["status"] == "available" else "  missing: " + ", ".join(r["missing_capabilities"])
            print(f"  {r['tool_id']}@{r['version']}: {r['status']}  kinds: {', '.join(r['supported_kinds'])}{extra}")
    return 0


def _contract(as_json: bool) -> int:
    doc = skill_contract()
    if as_json:
        print(json.dumps(doc, indent=2))
    else:
        print(f"{doc['skill_id']} {doc['version']} ({doc['package']}) — {doc['description']}")
        for t in doc["tools"]:
            print(f"  {t['tool_id']}: kinds={', '.join(t['kinds'])}  capabilities={', '.join(t['required_capabilities'])}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    try:
        if args.cmd == "doctor":
            return _doctor(as_json)
        if args.cmd == "contract":
            return _contract(as_json)
        engine = _engine(args)
        reqs = _requests(args)
        results = [engine.plan(r) if args.dry_run else engine.analyze(r) for r in reqs]
    except AnalysisError as e:
        if as_json:
            print(json.dumps({"error": e.to_dict()}, indent=2))
        else:
            sys.stderr.write(f"error [{e.code}]: {e.message}" + (f" {json.dumps(e.details)}" if e.details else "") + "\n")
        return e.exit_code
    if as_json:
        print(json.dumps(results[0] if len(results) == 1 else {"results": results}, indent=2, ensure_ascii=False))
    else:
        for res in results:
            print(_human_plan(res) if args.dry_run else _human_observation(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
