"""Contract tests: the published contract is derived from the implementation, validates against its own schemas,
and satisfies the consumer's (video-production-agent) SkillPackage / ToolSpec / Observation rules."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from media_analysis import SKILL_ID, VERSION
from media_analysis.budget import Budget
from media_analysis.contract import ANALYSIS_KINDS, KIND_TO_TOOL, PARAMETER_SCHEMAS, AnalysisRequest, skill_contract, tool_spec
from media_analysis.engine import AnalysisEngine, exit_code_for
from media_analysis.errors import ERROR_CODES, EXIT_CODES, AnalysisError
from media_analysis.registry import default_registry
from media_analysis.schemas import (BATCH_SCHEMA, OBSERVATION_SCHEMA, RESPONSE_SCHEMA, RESULT_SCHEMA, contract_refs, request_schema, validate)

FIXTURE = json.loads((Path(__file__).parent / "contract" / "agent_skill_package_contract.json").read_text(encoding="utf-8"))
REFS = contract_refs(list(ANALYSIS_KINDS), PARAMETER_SCHEMAS)


# ---- the validator itself
def test_schema_validator():
    sch = {"type": "object", "required": ["a"], "additionalProperties": False,
           "properties": {"a": {"type": "integer", "minimum": 1}, "b": {"type": ["string", "null"], "pattern": "^x"}, "c": {"type": "array", "items": {"enum": [1, 2]}}}}
    assert validate({"a": 1, "b": None, "c": [1, 2]}, sch) == []
    errs = validate({"a": 0, "b": "y", "c": [3], "d": 1}, sch)
    assert len(errs) == 4 and any("unexpected property 'd'" in e for e in errs)
    assert validate(True, {"type": "integer"}) and validate(1, {"type": "boolean"})
    assert validate({"r": {"a": 1}}, {"type": "object", "properties": {"r": {"$ref": "#/x"}}}, {"#/x": sch}) == []


# ---- contract JSON
def test_contract_is_json_and_self_consistent():
    c = skill_contract()
    text = json.dumps(c, allow_nan=False)
    assert json.loads(text) == c
    assert c["schema"] == "media-analysis/contract@1" and c["skill_id"] == SKILL_ID and c["version"] == VERSION and c["package"] == "media-analysis-skill"
    assert c["execution"]["canonical_invocation"][:2] == ["media-analysis", "run"] and c["execution"]["ai"] is False and c["execution"]["media_processing"] is False
    assert c["provenance"] == "OBSERVED" and c["observation_source_format"] == "media-analysis/<tool>@<version>"
    assert set(c["schemas"]) == {"request", "batch", "result", "response", "observation"}
    assert c["schema_versions"] == {"contract": "1", "request": "1", "response": "1", "observation": "1"}
    assert c["errors"]["codes"] == list(ERROR_CODES) and c["errors"]["exit_codes"] == EXIT_CODES and c["errors"]["success_exit_code"] == 0
    assert c["budget"]["supported"] == list(Budget.SUPPORTED)
    assert c["cache"]["statuses"] == ["hit", "miss", "invalid", "bypass", "disabled"] and c["cache"]["policies"] == ["use", "bypass", "only"]


def test_tools_and_kinds_derive_from_implementation():
    c = skill_contract()
    reg = default_registry()
    assert [t["tool_id"] for t in c["tools"]] == [f"{SKILL_ID}/{a.id}" for a in reg.all()]
    for t, a in zip(c["tools"], reg.all()):
        assert t == tool_spec(a)
        assert t["kinds"] == list(a.supported_kinds) == t["output_observation_kinds"]
        assert t["required_capabilities"] == list(a.required_capabilities) and t["version"] == a.version
        assert t["produces_output"] is False and t["writes_media"] is False and t["deterministic"] is True and t["provenance"] == "OBSERVED"
        assert t["supports"] == {"timeout": True, "cache": True, "dry_run": True}
        assert set(t["parameters"]) == set(t["kinds"]) and all(t["parameters"][k] == PARAMETER_SCHEMAS[k] for k in t["kinds"])
    assert c["analysis_kinds"] == list(ANALYSIS_KINDS) and len(c["analysis_kinds"]) == 10
    assert c["kind_to_tool"] == {k: f"{SKILL_ID}/{v}" for k, v in KIND_TO_TOOL.items()}
    covered = {k for t in c["tools"] for k in t["kinds"]}
    assert covered == set(ANALYSIS_KINDS)
    assert c["capability_names"] == sorted({cap for t in c["tools"] for cap in t["required_capabilities"]})
    assert c["capabilities"] == ["ffprobe"] and all("ffprobe" in t["required_capabilities"] for t in c["tools"])


# ---- consumer (video-production-agent) rules, from the fixture
def test_agent_skill_package_rules():
    c = skill_contract()
    for f in FIXTURE["skill_package"]["required"]:
        assert f in c, f
    assert c["skill_id"] and "/" not in c["skill_id"]
    assert c["tools"]
    seen = set()
    for t in c["tools"]:
        for f in FIXTURE["tool_spec"]["required"]:
            assert f in t, f
        assert t["skill_id"] == c["skill_id"]
        assert t["tool_id"].startswith(c["skill_id"] + "/") and t["tool_id"].count("/") == 1
        assert t["tool_id"] not in seen
        seen.add(t["tool_id"])
        assert t["produces_output"] is False                     # kind == "measure" on the agent side
    names = FIXTURE["capability_names"]
    for cap in c["capability_names"]:
        assert cap in names["known"] or cap.startswith(names["filter_prefix"]), cap


def test_agent_observation_rules(tmp_path):
    from test_unit import CountingAnalyzer, FakeRegistry, fake_caps
    f = tmp_path / "a.bin"
    f.write_bytes(b"media")
    eng = AnalysisEngine(caps=fake_caps("ffprobe"), registry=FakeRegistry(CountingAnalyzer()), clock=lambda: "2026-09-04T00:00:00Z")
    o = eng.analyze({"asset_id": "asset-1", "input": str(f), "kind": "media_probe"})["observation"]
    for field in FIXTURE["observation"]["fields"]:
        assert field in o
    assert "@" in o["source"] and not o["source"].startswith("ai")
    tool, ver = o["source"].split("@")
    assert tool == f"{SKILL_ID}/probe" and ver == VERSION and tool.split("/", 1)[0] == SKILL_ID
    assert "AI_GENERATED" not in json.dumps(o) and "provenance" not in o["data"]
    assert validate(o, OBSERVATION_SCHEMA, REFS) == []


# ---- request / response / observation schemas
def test_request_schema_matches_validation():
    sch = request_schema(list(ANALYSIS_KINDS), PARAMETER_SCHEMAS)
    good = {"analysis_id": "analysis-1", "asset_id": "asset-1", "input": "x.mp4", "kind": "silence", "parameters": {"threshold_db": -45}, "timeout": 10,
            "cache_policy": "only", "output_policy": {"round": 2}}
    assert validate(good, sch) == []
    r = AnalysisRequest.from_dict(good)
    assert r.cache_policy == "only" and r.parameters["threshold_db"] == -45.0
    for bad in [{"asset_id": "a", "input": "x", "kind": "media_probe", "command": "rm"}, {"asset_id": "a", "input": "x", "kind": "nope"},
                {"asset_id": "a", "input": "x", "kind": "media_probe", "cache_policy": "always"}, {"asset_id": "a", "input": "x", "kind": "media_probe", "strategy": "FULL"},
                {"asset_id": "a", "input": "x", "kind": "media_probe", "budget": {}}, {"asset_id": "a", "input": "", "kind": "media_probe"}]:
        assert validate(bad, sch) != []
        with pytest.raises(AnalysisError) as e:
            AnalysisRequest.from_dict(bad)
        assert e.value.code == "INVALID_INPUT"
    for fp in sch["x-forbidden-properties"]:
        assert fp not in sch["properties"]
    assert set(sch["x-parameters-by-kind"]) == set(ANALYSIS_KINDS)
    assert validate({"requests": [good], "budget": {"max_analysis_calls": 1}}, BATCH_SCHEMA, REFS) == []
    assert validate({"requests": [good], "budget": {"gpu": 1}}, BATCH_SCHEMA, REFS) != []
    with pytest.raises(AnalysisError) as e:
        Budget.from_dict({"max_bytes_scanned": 1})
    assert e.value.code == "INVALID_INPUT" and e.value.details["fields"] == ["max_bytes_scanned"]


def test_response_schema_roundtrip(tmp_path):
    from test_unit import CountingAnalyzer, FakeRegistry, fake_caps
    f = tmp_path / "a.bin"
    f.write_bytes(b"media")
    an = CountingAnalyzer()
    eng = AnalysisEngine(caps=fake_caps("ffprobe"), registry=FakeRegistry(an), clock=lambda: "2026-09-04T00:00:00Z")
    good = {"asset_id": "asset-1", "input": str(f), "kind": "media_probe"}
    doc = eng.run({"requests": [good, {"asset_id": "asset-1", "input": str(tmp_path / "nope"), "kind": "media_probe"}, {"asset_id": "bad id", "input": "x", "kind": "media_probe"},
                                 {"asset_id": "a", "input": str(f), "kind": "media_probe", "argv": ["x"]}], "budget": {"max_analysis_calls": 5}})
    assert validate(doc, RESPONSE_SCHEMA, REFS) == [], validate(doc, RESPONSE_SCHEMA, REFS)
    assert doc["status"] == "partial" and [r["status"] for r in doc["results"]] == ["ok", "error", "error", "error"]
    assert [r.get("error_kind") for r in doc["results"]] == [None, "FILE_NOT_FOUND", "INVALID_INPUT", "INVALID_INPUT"]
    assert doc["observations"] == [doc["results"][0]["observation"]] and doc["usage"] == {"analyzer_calls": 1, "cache_hits": 0, "seconds": doc["usage"]["seconds"]}
    assert exit_code_for(doc) == EXIT_CODES["FILE_NOT_FOUND"]
    assert json.loads(json.dumps(doc, allow_nan=False)) == doc
    ok = eng.run(good)
    assert ok["status"] == "ok" and exit_code_for(ok) == 0 and validate(ok, RESPONSE_SCHEMA, REFS) == []
    plan = eng.run([good], dry_run=True)
    assert plan["dry_run"] is True and "plan" in plan["results"][0] and plan["observations"] == [] and validate(plan, RESPONSE_SCHEMA, REFS) == []
    err = eng.run("not a document")
    assert err["status"] == "error" and err["error_kind"] == "INVALID_INPUT" and err["results"] == [] and validate(err, RESPONSE_SCHEMA, REFS) == []
    assert exit_code_for(err) == 2
    err2 = AnalysisEngine.error_response(AnalysisError("BUDGET_EXCEEDED", "x"))
    assert validate(err2, RESPONSE_SCHEMA, REFS) == [] and exit_code_for(err2) == 10
    for r in doc["results"]:
        assert validate(r, RESULT_SCHEMA, REFS) == []
    assert an.calls == 2      # batch + single run; the dry run and the rejected documents ran nothing


def test_exit_code_contract():
    assert EXIT_CODES == {code: i + 2 for i, code in enumerate(ERROR_CODES)}
    assert list(ERROR_CODES) == ["INVALID_INPUT", "FILE_NOT_FOUND", "PATH_NOT_ALLOWED", "UNSUPPORTED_FORMAT", "ANALYZER_UNAVAILABLE", "ANALYZER_TIMEOUT",
                                 "ANALYSIS_FAILED", "INVALID_RESULT", "BUDGET_EXCEEDED", "CACHE_INVALID", "VERIFICATION_FAILED", "CACHE_MISS"]


# ---- process group kill (grandchild must die), platform-independent check via pid file
def test_timeout_kills_grandchildren(tmp_path):
    from media_analysis.runner import run_argv
    pidfile = tmp_path / "child.pid"
    script = ("import subprocess, sys, time; p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
              f"open({str(pidfile)!r}, 'w').write(str(p.pid)); time.sleep(30)")
    with pytest.raises(AnalysisError) as e:
        run_argv([sys.executable, "-c", script], timeout=1.5)
    assert e.value.code == "ANALYZER_TIMEOUT"
    pid = int(pidfile.read_text())
    deadline = time.time() + 5
    alive = True
    while time.time() < deadline and alive:
        alive = _alive(pid)
        if alive:
            time.sleep(0.1)
    assert not alive, "grandchild survived the timeout"


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], stdout=subprocess.PIPE, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # zombie check: a reaped child is gone; an unreaped zombie is not running
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
        return state != "Z"
    except OSError:
        return True


# ---- executable path is not configurable
def test_executable_path_not_configurable():
    from media_analysis.cli import build_parser
    help_text = build_parser().format_help()
    forbidden = ("--ffmpeg", "--ffprobe", "--executable", "--exe")
    src = Path(__file__).resolve().parent.parent / "src" / "media_analysis"
    files = {p.name: p.read_text(encoding="utf-8") for p in src.rglob("*.py")}
    text = "\n".join(files.values())
    for flag in forbidden:
        assert flag not in text and flag not in help_text
    # the process environment is read in exactly two places: the child-environment filter and the secret-leak check
    users = sorted(name for name, body in files.items() if "os.environ" in body)
    assert users == ["runner.py", "verify.py"], users
    assert "shutil.which" in files["runner.py"] and "which(" in files["capabilities.py"]   # executables come from PATH only
    sch = request_schema(list(ANALYSIS_KINDS), PARAMETER_SCHEMAS)
    assert not any(k in sch["properties"] for k in ("ffmpeg", "ffprobe", "executable", "command", "argv"))


# ---- contract self-validation and drift fixtures (tests/contract/cases)
CASES_DIR = Path(__file__).parent / "contract" / "cases"


def _cases():
    return sorted(CASES_DIR.glob("*.json"))


def test_contract_check_accepts_live_contract():
    from media_analysis.contract_check import check_contract
    live = json.loads(json.dumps(skill_contract(), allow_nan=False))
    assert check_contract(live) == []
    assert check_contract("nope") == ["contract: document is not an object"]


@pytest.mark.parametrize("case_path", _cases(), ids=lambda p: p.stem)
def test_contract_drift_fixture(case_path):
    from media_analysis.contract_check import check_contract
    from contract.mutate import apply
    case = json.loads(case_path.read_text(encoding="utf-8"))
    doc = apply(json.loads(json.dumps(skill_contract())), case["mutations"])
    problems = check_contract(doc)
    if not case["expect_problems"]:
        assert problems == []
    else:
        for needle in case["expect_problems"]:
            assert any(needle in p for p in problems), (needle, problems)
        assert len(problems) >= len(case["expect_problems"])


def test_contract_check_cli(tmp_path):
    saved = tmp_path / "contract.json"
    r = subprocess.run([sys.executable, "-m", "media_analysis.cli", "contract", "--json"], cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    saved.write_text(r.stdout)
    r = subprocess.run([sys.executable, "-m", "media_analysis.cli", "contract", "--check", str(saved), "--json"], cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    doc = json.loads(r.stdout)
    assert r.returncode == 0 and doc["status"] == "ok" and doc["problems"] == [] and doc["supported_schemas"] == ["media-analysis/contract@1"]
    drifted = json.loads(saved.read_text())
    drifted["version"] = "9.9.9"
    r = subprocess.run([sys.executable, "-m", "media_analysis.cli", "contract", "--check", "-", "--json"], cwd=str(tmp_path), input=json.dumps(drifted),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    doc = json.loads(r.stdout)
    assert r.returncode == 1 and doc["status"] == "drift" and any("version" in p for p in doc["problems"])
    r = subprocess.run([sys.executable, "-m", "media_analysis.cli", "contract", "--check", "-"], cwd=str(tmp_path), input="{broken", stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert r.returncode == 1 and "cannot read" in r.stdout


def test_observation_has_no_confidence_or_judgement_fields():
    """Observations carry measurements only: no confidence, no recommendation, no decision vocabulary."""
    src = Path(__file__).resolve().parent.parent / "src" / "media_analysis" / "analyzers"
    text = "\n".join(p.read_text(encoding="utf-8") for p in src.rglob("*.py")).lower()
    for word in ('"confidence"', '"recommend', '"should_', '"decision"', '"unwanted"', '"important"', '"keep"', '"remove"'):
        assert word not in text, word
    assert "confidence" not in json.dumps(OBSERVATION_SCHEMA)


def test_batch_identity_independent_of_order(tmp_path):
    from test_unit import CountingAnalyzer, FakeRegistry, fake_caps
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"aaaa")
    b.write_bytes(b"bbbb")
    eng = AnalysisEngine(caps=fake_caps("ffprobe"), registry=FakeRegistry(CountingAnalyzer()), clock=lambda: "2026-09-04T00:00:00Z")
    reqs = [{"analysis_id": "an-a", "asset_id": "asset-a", "input": str(a), "kind": "media_probe"},
            {"analysis_id": "an-b", "asset_id": "asset-b", "input": str(b), "kind": "media_probe"},
            {"asset_id": "asset-a2", "input": str(a), "kind": "media_probe"}]
    fwd = eng.run({"requests": reqs})
    rev = eng.run({"requests": list(reversed(reqs))})
    by_id = lambda doc: {(r["asset_id"], r["analysis_id"]): r["observation"] for r in doc["results"]}  # noqa: E731
    f, r = by_id(fwd), by_id(rev)
    assert set(f) == set(r) and all(f[k]["data"] == r[k]["data"] and f[k]["id"] == r[k]["id"] for k in f)
    assert f[("asset-a", "an-a")]["asset"]["fingerprint"] != f[("asset-b", "an-b")]["asset"]["fingerprint"]
    assert f[("asset-a", "an-a")]["id"] == f[("asset-a2", "analysis-" + f[("asset-a", "an-a")]["analysis"]["identity"][:16])]["id"]   # same file, same identity
    assert [x["asset_id"] for x in fwd["observations"]] == ["asset-a", "asset-b", "asset-a2"]


# ---- AI-video-production-OS registry / conformance rules (tests/contract/os_registry_contract.json)
OS_RULES = json.loads((Path(__file__).parent / "contract" / "os_registry_contract.json").read_text(encoding="utf-8"))


def test_os_registry_provides_rules():
    c = skill_contract()
    assert c["skill_id"] and "skill_id" in OS_RULES["skill_identity"]["accepted_shapes"]
    provides = c["provides"]
    assert [e["kind"] for e in provides] == sorted(ANALYSIS_KINDS) and len(provides) == 10
    ids = [e["id"] for e in provides]
    assert len(set(ids)) == len(ids)
    tool_ids = {t["tool_id"] for t in c["tools"]}
    for e in provides:
        for f in OS_RULES["provides_entry"]["required"]:
            assert isinstance(e[f], str) and e[f]
        assert e["lifecycle"] in OS_RULES["provides_entry"]["lifecycles"]
        assert e["tool_id"] in tool_ids and c["kind_to_tool"][e["kind"]] == e["tool_id"]
        assert set(e) <= set(OS_RULES["provides_entry"]["required"]) | set(OS_RULES["provides_entry"]["extra_fields_permitted"])
    for cid in OS_RULES["known_collision"]["ids"]:
        assert cid in ids                                    # the documented collision with qc-skill is published, not hidden


def test_os_denylist_is_a_superset_and_recursive():
    from media_analysis.contract import AnalysisRequest, forbidden_keys
    assert set(OS_RULES["denylist"]["canonical"]) <= set(AnalysisRequest.FORBIDDEN_KEYS)
    assert skill_contract()["security"]["forbidden_keys"] == list(AnalysisRequest.FORBIDDEN_KEYS)
    doc = {"asset_id": "a", "input": "x", "kind": "silence", "parameters": {"threshold_db": -40, "Filter": "x", "deep": {"token": 1}}, "list": [{"ENV": {}}]}
    assert forbidden_keys(doc, AnalysisRequest.FORBIDDEN_KEYS) == ["parameters.Filter", "parameters.deep.token", "list[0].ENV"]
    with pytest.raises(AnalysisError) as e:
        AnalysisRequest.from_dict(doc)
    assert e.value.code == "INVALID_INPUT" and e.value.details["fields"] == ["parameters.Filter", "parameters.deep.token", "list[0].ENV"]


def test_error_class_in_results(tmp_path):
    from test_unit import CountingAnalyzer, FakeRegistry, fake_caps
    f = tmp_path / "a.bin"
    f.write_bytes(b"media")
    eng = AnalysisEngine(caps=fake_caps("ffprobe"), registry=FakeRegistry(CountingAnalyzer()), budget=Budget(max_analysis_calls=0))
    doc = eng.run({"requests": [{"asset_id": "a", "input": str(f), "kind": "media_probe"}, {"asset_id": "a", "input": str(tmp_path / "nope"), "kind": "media_probe"}]})
    assert [r["error_class"] for r in doc["results"]] == ["BLOCKED", "FATAL"]
    assert all(r["error"]["class"] == r["error_class"] for r in doc["results"])
    assert validate(doc, RESPONSE_SCHEMA, REFS) == []
    err = eng.run("nope")
    assert err["error_class"] == "FATAL" and err["error"]["class"] == "FATAL"
    c = skill_contract()
    assert c["errors"]["classes"] == {code: c["errors"]["classes"][code] for code in ERROR_CODES} and set(c["errors"]["class_semantics"]) == {"FATAL", "RETRYABLE", "BLOCKED"}


def test_doctor_reports_per_capability(tmp_path):
    from media_analysis.cli import doctor_report
    doc = doctor_report(workspace=str(tmp_path))
    caps = {c["id"]: c for c in doc["capabilities"]}
    assert set(caps) == {e["id"] for e in skill_contract()["provides"]}
    for c in caps.values():
        assert c["status"] in ("AVAILABLE", "MISSING") and c["lifecycle"] == "EXPERIMENTAL" and c["tool_id"].startswith("media-analysis/")
        assert (c["status"] == "AVAILABLE") == (not c["missing"])
    if doc["status"] == "ok":
        assert all(c["status"] == "AVAILABLE" for c in caps.values())
