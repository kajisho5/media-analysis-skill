#!/usr/bin/env python3
"""Contract evals: does the published contract hold against the running implementation? No media needed except
for the last cases, which generate one fixture with ffmpeg.

    python3 evals/contract_evals.py [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE.parent / "tests"))

from fixtures.generate import available, build_all  # noqa: E402
from media_analysis import SKILL_ID  # noqa: E402
from media_analysis.cache import ObservationCache  # noqa: E402
from media_analysis.capabilities import CapabilitySet  # noqa: E402
from media_analysis.contract import ANALYSIS_KINDS, PARAMETER_SCHEMAS, AnalysisRequest, skill_contract  # noqa: E402
from media_analysis.engine import AnalysisEngine, exit_code_for  # noqa: E402
from media_analysis.errors import AnalysisError  # noqa: E402
from media_analysis.registry import default_registry  # noqa: E402
from media_analysis.schemas import OBSERVATION_SCHEMA, RESPONSE_SCHEMA, contract_refs, validate  # noqa: E402
from media_analysis.security import PathPolicy  # noqa: E402

REFS = contract_refs(list(ANALYSIS_KINDS), PARAMETER_SCHEMAS)
CASES: List[Dict[str, Any]] = []


def case(cid: str, description: str) -> Callable:
    def deco(fn):
        CASES.append({"id": cid, "description": description, "fn": fn})
        return fn
    return deco


def expect(cond: bool, detail: str) -> Dict[str, Any]:
    return {"ok": bool(cond), "detail": detail}


@case("C01_contract_json_valid", "contract --json is JSON-serialisable and carries every required section")
def c01():
    c = skill_contract()
    text = json.dumps(c, allow_nan=False)
    required = ["schema", "skill_id", "version", "package", "analysis_kinds", "tools", "capability_names", "schemas", "execution", "schema_versions", "errors", "cache", "budget"]
    return [expect(json.loads(text) == c, "roundtrip"), expect(all(k in c for k in required), f"sections {required}")]


@case("C02_declared_tools_exist", "every declared tool is an analyzer in the registry and vice versa")
def c02():
    declared = {t["tool_id"] for t in skill_contract()["tools"]}
    implemented = {f"{SKILL_ID}/{a.id}" for a in default_registry().all()}
    return [expect(declared == implemented, f"declared={sorted(declared)} implemented={sorted(implemented)}")]


@case("C03_declared_kinds_exist", "every declared kind is served by exactly one analyzer and can be requested")
def c03():
    reg = default_registry()
    c = skill_contract()
    out = [expect(set(c["analysis_kinds"]) == set(ANALYSIS_KINDS), "kinds == ANALYSIS_KINDS")]
    for k in c["analysis_kinds"]:
        a = reg.for_kind(k)
        out.append(expect(c["kind_to_tool"][k] == f"{SKILL_ID}/{a.id}", f"{k} -> {a.id}"))
        AnalysisRequest.from_dict({"asset_id": "a", "input": "x", "kind": k})
    return out


@case("C04_observation_schema_valid", "an observation from a fake analyzer validates against the observation schema")
def c04():
    from test_unit import CountingAnalyzer, FakeRegistry, fake_caps
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "a.bin"
        f.write_bytes(b"media")
        eng = AnalysisEngine(caps=fake_caps("ffprobe"), registry=FakeRegistry(CountingAnalyzer()))
        o = eng.analyze({"asset_id": "asset-1", "input": str(f), "kind": "media_probe"})["observation"]
        errs = validate(o, OBSERVATION_SCHEMA, REFS)
        return [expect(errs == [], f"schema errors: {errs}"), expect(o["source"] == f"{SKILL_ID}/probe@{o['analysis']['analyzer_version']}", "source format")]


@case("C05_request_response_roundtrip", "run() of a batch yields a response that validates and mirrors the request ids")
def c05():
    from test_unit import CountingAnalyzer, FakeRegistry, fake_caps
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "a.bin"
        f.write_bytes(b"media")
        eng = AnalysisEngine(caps=fake_caps("ffprobe"), registry=FakeRegistry(CountingAnalyzer()))
        doc = eng.run({"requests": [{"analysis_id": "an-1", "asset_id": "as-1", "input": str(f), "kind": "media_probe"}]})
        errs = validate(doc, RESPONSE_SCHEMA, REFS)
        r = doc["results"][0]
        return [expect(errs == [], f"schema errors: {errs}"), expect(r["analysis_id"] == "an-1" and r["asset_id"] == "as-1" and r["kind"] == "media_probe", "ids echoed"),
                expect(doc["status"] == "ok" and exit_code_for(doc) == 0, "ok status / exit 0")]


@case("C06_unsupported_field_rejected", "unknown request fields, unknown parameters, unknown budget fields are INVALID_INPUT")
def c06():
    out = []
    for doc in [{"asset_id": "a", "input": "x", "kind": "media_probe", "strategy": "FULL"}, {"asset_id": "a", "input": "x", "kind": "silence", "parameters": {"gain": 1}},
                {"asset_id": "a", "input": "x", "kind": "media_probe", "output_policy": {"format": "yaml"}}]:
        try:
            AnalysisRequest.from_dict(doc)
            out.append(expect(False, f"accepted {doc}"))
        except AnalysisError as e:
            out.append(expect(e.code == "INVALID_INPUT", f"{e.code} for {list(doc)[-1]}"))
    eng = AnalysisEngine(caps=CapabilitySet())
    resp = eng.run({"requests": [{"asset_id": "a", "input": "x", "kind": "media_probe"}], "budget": {"max_cost_usd": 1}})
    out.append(expect(resp["status"] == "error" and resp["error_kind"] == "INVALID_INPUT", "unknown budget rejected at batch level"))
    return out


@case("C07_malicious_command_rejected", "command / argv / shell style fields never reach an analyzer")
def c07():
    from test_unit import CountingAnalyzer, FakeRegistry, fake_caps
    an = CountingAnalyzer()
    eng = AnalysisEngine(caps=fake_caps("ffprobe"), registry=FakeRegistry(an))
    out = []
    for field in ("command", "argv", "args", "shell", "cmd", "exec"):
        resp = eng.run({"asset_id": "a", "input": "x", "kind": "media_probe", field: ["rm", "-rf", "/"]})
        out.append(expect(resp["results"][0]["error_kind"] == "INVALID_INPUT", field))
    out.append(expect(an.calls == 0, "no analyzer call"))
    return out


@case("C08_path_traversal_rejected", "inputs outside allowed roots and writes outside the workspace are PATH_NOT_ALLOWED")
def c08():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "root"
        root.mkdir()
        outside = Path(tmp) / "secret.bin"
        outside.write_bytes(b"x")
        pol = PathPolicy(workspace=str(root), allowed_input_roots=[str(root)])
        out = []
        for p in (str(outside), str(root / ".." / "secret.bin")):
            try:
                pol.resolve_input(p)
                out.append(expect(False, f"accepted {p}"))
            except AnalysisError as e:
                out.append(expect(e.code == "PATH_NOT_ALLOWED", f"{e.code} for traversal input"))
        for w in ("../escape-cache", str(Path(tmp) / "elsewhere")):
            try:
                ObservationCache(w, pol)
                out.append(expect(False, f"cache accepted {w}"))
            except AnalysisError as e:
                out.append(expect(e.code == "PATH_NOT_ALLOWED", f"{e.code} for cache dir {w}"))
        return out


@case("C09_credential_leakage_rejected", "an observation carrying a secret env value or a secret-looking key is refused")
def c09():
    import os
    from test_unit import CountingAnalyzer, FakeRegistry, fake_caps
    os.environ["MEDIA_EVAL_API_TOKEN"] = "eval-secret-0123456789"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "a.bin"
            f.write_bytes(b"media")
            out = []
            for data in ({"container": {"note": "eval-secret-0123456789"}}, {"container": {"password": "x"}}):
                an = CountingAnalyzer()
                an.data = data
                eng = AnalysisEngine(caps=fake_caps("ffprobe"), registry=FakeRegistry(an))
                resp = eng.run({"asset_id": "a", "input": str(f), "kind": "media_probe"})
                out.append(expect(resp["results"][0]["error_kind"] == "VERIFICATION_FAILED" and resp["observations"] == [], str(list(data["container"]))))
            return out
    finally:
        del os.environ["MEDIA_EVAL_API_TOKEN"]


@case("C10_cache_hit_identical_observation", "real ffmpeg: second run of the same request is a cache hit with an identical observation and zero analyzer calls")
def c10():
    if not available():
        return [expect(False, "ffmpeg / ffprobe required")]
    with tempfile.TemporaryDirectory() as tmp:
        fx = build_all(Path(tmp) / "fixtures")
        pol = PathPolicy(workspace=tmp)
        eng = AnalysisEngine(policy=pol, cache=ObservationCache(str(Path(tmp) / "cache"), pol))
        req = {"asset_id": "asset-1", "input": str(fx["av"]), "kind": "loudness", "parameters": {"stream": 0}}
        first = eng.run([req, dict(req, parameters={})])         # {} and {"stream": 0} are the same effective parameters
        second = eng.run(req)
        return [expect(first["usage"]["analyzer_calls"] == 1 and first["results"][1]["cache"]["status"] == "hit", "effective-parameter identity within one batch"),
                expect(second["usage"]["analyzer_calls"] == 0 and second["results"][0]["cache"]["status"] == "hit", "second run: no analyzer"),
                expect(second["observations"] == first["observations"][:1], "identical observation"),
                expect(len(eng.executions) == 1, "one execution overall")]


@case("C11_contract_registry_consistency", "check_contract() accepts the live contract and flags every drift fixture in tests/contract/cases")
def c11():
    from contract.mutate import apply
    from media_analysis.contract_check import check_contract
    live = json.loads(json.dumps(skill_contract()))
    out = [expect(check_contract(live) == [], "live contract self-validates")]
    for path in sorted((HERE.parent / "tests" / "contract" / "cases").glob("*.json")):
        case_doc = json.loads(path.read_text(encoding="utf-8"))
        problems = check_contract(apply(live, case_doc["mutations"]))
        ok = (problems == []) if not case_doc["expect_problems"] else all(any(n in p for p in problems) for n in case_doc["expect_problems"])
        out.append(expect(ok, f"{case_doc['id']}: {problems[:2]}"))
    return out


@case("C12_invalid_input_json", "CLI `run -`: malformed JSON, missing / unknown kind, unknown parameter, command field -> one parseable error response each")
def c12():
    import subprocess
    docs = ["{broken", json.dumps({"asset_id": "a", "input": "x.mp4"}), json.dumps({"asset_id": "a", "input": "x.mp4", "kind": "nope"}),
            json.dumps({"asset_id": "a", "input": "x.mp4", "kind": "silence", "parameters": {"gain": 1}}), json.dumps({"asset_id": "a", "input": "x.mp4", "kind": "media_probe", "argv": ["x"]})]
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        for d in docs:
            r = subprocess.run([sys.executable, "-m", "media_analysis.cli", "run", "-", "--json"], cwd=tmp, input=d, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                resp = json.loads(r.stdout)
                kind = resp.get("error_kind") or resp["results"][0]["error_kind"]
                out.append(expect(r.returncode == 2 and resp["status"] == "error" and kind == "INVALID_INPUT" and r.stderr == "", d[:40]))
            except (ValueError, KeyError, IndexError) as e:
                out.append(expect(False, f"unparseable response for {d[:40]}: {e}"))
    return out


@case("C13_timeout_process_tree", "real ffmpeg: a timed-out request yields ANALYZER_TIMEOUT, leaves no cache entry and no ffmpeg process")
def c13():
    if not available():
        return [expect(False, "ffmpeg / ffprobe required")]
    with tempfile.TemporaryDirectory() as tmp:
        fx = build_all(Path(tmp) / "fixtures")
        pol = PathPolicy(workspace=tmp)
        eng = AnalysisEngine(policy=pol, cache=ObservationCache(str(Path(tmp) / "cache"), pol))
        doc = eng.run({"asset_id": "a", "input": str(fx["av"]), "kind": "integrity", "timeout": 0.001})
        alive = []
        if Path("/proc").is_dir():
            for pid in Path("/proc").iterdir():
                if pid.name.isdigit():
                    try:
                        cmd = (pid / "cmdline").read_bytes()
                    except OSError:
                        continue
                    if b"ffmpeg" in cmd and str(fx["av"]).encode() in cmd:
                        alive.append(pid.name)
        return [expect(doc["status"] == "error" and doc["results"][0]["error_kind"] == "ANALYZER_TIMEOUT", "error kind"),
                expect(not list((Path(tmp) / "cache").rglob("*.json")), "no cache entry"), expect(alive == [], f"ffmpeg processes: {alive}"),
                expect(doc["observations"] == [], "no partial observation")]


@case("C14_batch_identity", "results keep analysis_id / asset_id / kind per request regardless of order; same content -> same identity")
def c14():
    from test_unit import CountingAnalyzer, FakeRegistry, fake_caps
    with tempfile.TemporaryDirectory() as tmp:
        a, b = Path(tmp) / "a.bin", Path(tmp) / "b.bin"
        a.write_bytes(b"aaaa")
        b.write_bytes(b"bbbb")
        eng = AnalysisEngine(caps=fake_caps("ffprobe"), registry=FakeRegistry(CountingAnalyzer()))
        reqs = [{"analysis_id": "an-a", "asset_id": "asset-a", "input": str(a), "kind": "media_probe"},
                {"analysis_id": "an-b", "asset_id": "asset-b", "input": str(b), "kind": "media_probe"}]
        fwd, rev = eng.run({"requests": reqs}), eng.run({"requests": list(reversed(reqs))})
        key = lambda r: (r["asset_id"], r["analysis_id"], r["observation"]["id"])  # noqa: E731
        return [expect(sorted(map(key, fwd["results"])) == sorted(map(key, rev["results"])), "same identities in both orders"),
                expect(fwd["results"][0]["observation"]["asset"]["fingerprint"] != fwd["results"][1]["observation"]["asset"]["fingerprint"], "different content, different fingerprint"),
                expect([r["asset_id"] for r in rev["results"]] == ["asset-b", "asset-a"], "request order preserved in results")]


@case("C15_no_audio", "real ffmpeg: video-only media -> audio kinds are UNSUPPORTED_FORMAT results, video kinds ok, batch partial")
def c15():
    if not available():
        return [expect(False, "ffmpeg / ffprobe required")]
    with tempfile.TemporaryDirectory() as tmp:
        fx = build_all(Path(tmp) / "fixtures")
        eng = AnalysisEngine(policy=PathPolicy(workspace=tmp))
        doc = eng.run({"requests": [{"asset_id": "v", "input": str(fx["video_only"]), "kind": k} for k in ("media_probe", "audio_format", "silence", "loudness", "video_format", "timing")]})
        by = {r["kind"]: r for r in doc["results"]}
        return [expect(doc["status"] == "partial", "partial"),
                expect(all(by[k]["status"] == "error" and by[k]["error_kind"] == "UNSUPPORTED_FORMAT" for k in ("audio_format", "silence", "loudness")), "audio kinds unsupported"),
                expect(all(by[k]["status"] == "ok" for k in ("media_probe", "video_format", "timing")), "video kinds ok"),
                expect(by["media_probe"]["observation"]["data"]["audio"] is None and by["media_probe"]["observation"]["data"]["audio_stream_count"] == 0, "probe reports no audio as a fact")]


@case("C16_deterministic_result", "real ffmpeg: two engines, no cache -> identical observation id, identity, data, fingerprint")
def c16():
    if not available():
        return [expect(False, "ffmpeg / ffprobe required")]
    with tempfile.TemporaryDirectory() as tmp:
        fx = build_all(Path(tmp) / "fixtures")
        reqs = [{"asset_id": "a", "input": str(fx["av"]), "kind": k} for k in ("silence", "loudness", "scene_detection", "timing")]
        d1 = AnalysisEngine(policy=PathPolicy(workspace=tmp)).run({"requests": reqs})
        d2 = AnalysisEngine(policy=PathPolicy(workspace=tmp)).run({"requests": reqs})
        same = all(o1["id"] == o2["id"] and o1["data"] == o2["data"] and o1["asset"] == o2["asset"] for o1, o2 in zip(d1["observations"], d2["observations"]))
        return [expect(d1["status"] == d2["status"] == "ok", "both ok"), expect(same, "identical observations")]


@case("C17_cache_invalidation", "content change, analyzer version change and parameter change each invalidate; corruption is 'invalid'")
def c17():
    from test_unit import CountingAnalyzer, FakeRegistry, fake_caps
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "a.bin"
        f.write_bytes(b"v1")
        an = CountingAnalyzer()
        pol = PathPolicy(workspace=tmp)
        eng = AnalysisEngine(caps=fake_caps("ffprobe"), registry=FakeRegistry(an), policy=pol, cache=ObservationCache(str(Path(tmp) / "cache"), pol))
        req = {"asset_id": "a", "input": str(f), "kind": "media_probe"}
        s1 = eng.analyze(req)["cache"]["status"]
        s2 = eng.analyze(req)["cache"]["status"]
        f.write_bytes(b"v2")
        s3 = eng.analyze(req)["cache"]["status"]
        an.version = "0.1.1"
        s4 = eng.analyze(req)["cache"]["status"]
        key = eng.analyze(req)["cache"]["key"]
        entry = Path(tmp) / "cache" / key[:2] / f"{key}.json"
        entry.write_text("{corrupt")
        s5 = eng.analyze(req)["cache"]["status"]
        return [expect([s1, s2, s3, s4, s5] == ["miss", "hit", "miss", "miss", "invalid"], f"statuses {[s1, s2, s3, s4, s5]}"), expect(an.calls == 4, f"analyzer calls {an.calls}")]


def run_all() -> List[Dict[str, Any]]:
    results = []
    for c in CASES:
        try:
            checks = c["fn"]()
            results.append({"id": c["id"], "description": c["description"], "ok": all(x["ok"] for x in checks), "checks": checks})
        except Exception as e:  # a crash is a failure, reported not raised
            results.append({"id": c["id"], "description": c["description"], "ok": False, "checks": [], "error": f"{type(e).__name__}: {e}"})
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    results = run_all()
    if args.json:
        print(json.dumps({"passed": sum(r["ok"] for r in results), "total": len(results), "results": results}, indent=2))
    else:
        for r in results:
            print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['id']}  {r['description']}")
            for ch in r["checks"]:
                if not ch["ok"]:
                    print(f"      {ch['detail']}")
            if r.get("error"):
                print(f"      error: {r['error']}")
        print(f"{sum(r['ok'] for r in results)}/{len(results)} passed")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
