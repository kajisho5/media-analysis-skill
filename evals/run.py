#!/usr/bin/env python3
"""Evals: run every case in evals/cases against freshly generated fixtures with the real ffmpeg / ffprobe and compare
the Observation with expected values and tolerances. Expected values are derived from the fixture construction
(tests/fixtures/generate.py), each case says how.

    python3 evals/run.py            # table on stdout, exit 1 on any failure
    python3 evals/run.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE.parent / "tests"))

from fixtures.generate import available, build_all  # noqa: E402
from media_analysis.engine import AnalysisEngine  # noqa: E402
from media_analysis.errors import AnalysisError  # noqa: E402
from media_analysis.security import PathPolicy  # noqa: E402


def lookup(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise KeyError(path)
    return cur


def check(actual: Any, exp: Dict[str, Any]) -> bool:
    op = exp.get("op", "eq")
    v = exp.get("value")
    if op == "eq":
        return actual == v
    if op == "approx":
        return isinstance(actual, (int, float)) and actual is not None and abs(actual - v) <= exp["tolerance"]
    if op == "gte":
        return isinstance(actual, (int, float)) and actual >= v
    if op == "lte":
        return isinstance(actual, (int, float)) and actual <= v
    if op == "startswith":
        return isinstance(actual, str) and actual.startswith(v)
    raise ValueError(f"unknown op {op}")


def run_case(engine: AnalysisEngine, fixtures: Dict[str, Path], case: Dict[str, Any]) -> Dict[str, Any]:
    req = {"asset_id": case["id"], "input": str(fixtures[case["fixture"]]), "kind": case["kind"], "parameters": case.get("parameters", {})}
    try:
        obs = engine.analyze(req)["observation"]
    except AnalysisError as e:
        return {"id": case["id"], "ok": False, "error": e.to_dict(), "checks": []}
    checks = []
    for exp in case["expect"]:
        try:
            actual = lookup(obs, exp["path"])
            ok = check(actual, exp)
        except (KeyError, IndexError, TypeError):
            actual, ok = "<missing>", False
        checks.append({"path": exp["path"], "expected": exp, "actual": actual, "ok": ok})
    return {"id": case["id"], "ok": all(c["ok"] for c in checks), "checks": checks, "source": obs["source"]}


def run_all(cases_dir: Path = HERE / "cases") -> List[Dict[str, Any]]:
    if not available():
        raise SystemExit("ffmpeg / ffprobe are required to run the evals")
    with tempfile.TemporaryDirectory(prefix="media-analysis-evals-") as tmp:
        fixtures = build_all(Path(tmp) / "fixtures")
        engine = AnalysisEngine(policy=PathPolicy(workspace=tmp))
        cases = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(cases_dir.glob("*.json"))]
        return [run_case(engine, fixtures, c) for c in cases]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    results = run_all()
    if args.json:
        print(json.dumps({"passed": sum(r["ok"] for r in results), "total": len(results), "results": results}, indent=2))
    else:
        for r in results:
            print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['id']}")
            for c in r["checks"]:
                if not c["ok"]:
                    print(f"      {c['path']}: expected {c['expected']} got {c['actual']!r}")
            if r.get("error"):
                print(f"      error: {r['error']}")
        print(f"{sum(r['ok'] for r in results)}/{len(results)} passed")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
