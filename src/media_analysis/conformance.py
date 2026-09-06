"""Self-conformance checks against AI-video-production-OS `docs/SKILL_SPEC.md` section 8 ("what a third-party or
non-Python Skill must pass"). `contract_check.py` answers "does a saved contract document still describe this
installation"; this module answers the separate question "does this installation actually behave the way its
contract claims" — it submits real requests to the engine and inspects real source, rather than trusting the
contract's own words about itself.

Every check reports PASS, FAIL, or NOT_IMPLEMENTED (that vocabulary, not a bespoke one, so results are drop-in
comparable with the OS's own `registry/conformance.py` `CheckResult`). NOT_IMPLEMENTED is used only when a check
structurally cannot apply to this Skill (documented why on that check), never as a stand-in for "didn't check": every
check this module claims to run either actually submitted a request / inspected real source and got PASS or FAIL.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List

from . import VERSION
from .contract import AnalysisRequest, skill_contract
from .errors import AnalysisError
from .security import PathPolicy

_SHELL_PATTERNS = ("shell=True", "os.system(", "os.popen(", "commands.getoutput(", "subprocess.call(cmd, shell", "eval(", "exec(")


@dataclass
class CheckResult:
    check: str
    status: str  # "PASS" | "FAIL" | "NOT_IMPLEMENTED"
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {"check": self.check, "status": self.status, "detail": self.detail}


def check_publishes_contract() -> CheckResult:
    """SKILL_SPEC.md #1: contract entrypoint produces valid JSON with a skill identity and well-formed `provides`."""
    c = skill_contract()
    if not isinstance(c.get("skill_id"), str) or not c["skill_id"]:
        return CheckResult("publishes_contract", "FAIL", "no skill_id")
    bad = [e for e in c.get("provides", []) if not isinstance(e.get("id"), str) or not isinstance(e.get("tool_id"), str) or not e.get("lifecycle")]
    if bad:
        return CheckResult("publishes_contract", "FAIL", f"malformed provides entries: {bad}")
    return CheckResult("publishes_contract", "PASS", f"skill_id={c['skill_id']!r}, {len(c['provides'])} provides entries well-formed")


def check_forbidden_keys_rejected() -> CheckResult:
    """SKILL_SPEC.md #2: a denylisted key, at top level and nested inside `parameters`, is rejected structurally."""
    for doc in ({"asset_id": "a", "input": "x.mp4", "kind": "media_probe", "command": "id"},
                {"asset_id": "a", "input": "x.mp4", "kind": "silence", "parameters": {"api_key": "x"}}):
        try:
            AnalysisRequest.from_dict(doc)
            return CheckResult("forbidden_keys_rejected", "FAIL", f"accepted a request carrying a forbidden key: {doc}")
        except AnalysisError as e:
            if e.code != "INVALID_INPUT":
                return CheckResult("forbidden_keys_rejected", "FAIL", f"wrong error code {e.code} for {doc}")
    return CheckResult("forbidden_keys_rejected", "PASS", "top-level and nested forbidden keys both rejected with INVALID_INPUT")


def check_no_unsafe_shell_out() -> CheckResult:
    """SKILL_SPEC.md #3: source is available, so this is the AST/lint-shaped check rather than an injection probe."""
    src = Path(__file__).resolve().parent
    files = [p for p in src.rglob("*.py") if p.name != "conformance.py"]   # this file's own docstrings/denylist name the patterns; it never runs a subprocess itself
    hits = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for pattern in _SHELL_PATTERNS:
            if pattern in text:
                hits.append(f"{path.name}: {pattern}")
    if hits:
        return CheckResult("no_unsafe_shell_out", "FAIL", f"found: {hits}")
    return CheckResult("no_unsafe_shell_out", "PASS", f"no shell=True / os.system / eval / exec across {len(files)} source files")


def check_workspace_confinement() -> CheckResult:
    """SKILL_SPEC.md #4: a cache directory (this Skill's only write target) outside the declared workspace, and a
    symlink that resolves outside it, are both rejected."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "workspace"
        outside = Path(tmp) / "outside"
        workspace.mkdir()
        outside.mkdir()
        policy = PathPolicy(workspace=str(workspace))
        try:
            policy.resolve_write_dir(str(outside))
            return CheckResult("workspace_confinement", "FAIL", "a cache directory outside the workspace was accepted")
        except AnalysisError as e:
            if e.code != "PATH_NOT_ALLOWED":
                return CheckResult("workspace_confinement", "FAIL", f"wrong error code {e.code} for an outside path")
        link = workspace / "escape"
        try:
            link.symlink_to(outside)
        except OSError:
            return CheckResult("workspace_confinement", "NOT_IMPLEMENTED", "symlink creation not permitted in this environment (e.g. Windows without privilege); the plain outside-path case above still passed")
        try:
            policy.resolve_write_dir(str(link))
            return CheckResult("workspace_confinement", "FAIL", "a symlink resolving outside the workspace was accepted")
        except AnalysisError as e:
            if e.code != "PATH_NOT_ALLOWED":
                return CheckResult("workspace_confinement", "FAIL", f"wrong error code {e.code} for a symlink escape")
    return CheckResult("workspace_confinement", "PASS", "an outside path and a symlink resolving outside the workspace are both rejected")


def check_no_clobber_input() -> CheckResult:
    """SKILL_SPEC.md #5: not applicable — this Skill is measurement-only. It never writes to media at all (no output
    path parameter exists anywhere in the request schema: `contract --json` -> `tools[].produces_output` is false
    for every tool), so there is no call shape that could overwrite an input."""
    c = skill_contract()
    if any(t.get("produces_output") for t in c["tools"]):
        return CheckResult("no_clobber_input", "FAIL", "a tool claims produces_output=true; this check needs real wiring")
    return CheckResult("no_clobber_input", "NOT_IMPLEMENTED", "not applicable: every tool has produces_output=false (measurement only, no output-path parameter exists)")


def check_lifecycle_declared() -> CheckResult:
    """SKILL_SPEC.md #6: every declared Capability carries a valid lifecycle from the 5-state model."""
    from .contract_check import LIFECYCLES
    c = skill_contract()
    bad = [e["id"] for e in c["provides"] if e.get("lifecycle") not in LIFECYCLES]
    if bad:
        return CheckResult("lifecycle_declared", "FAIL", f"invalid lifecycle for: {bad}")
    return CheckResult("lifecycle_declared", "PASS", f"{len(c['provides'])} provides entries all carry a valid lifecycle")


def check_doctor_status() -> CheckResult:
    """SKILL_SPEC.md #7: doctor produces a machine-readable AVAILABLE/MISSING report per declared Capability."""
    from .cli import doctor_report
    doc = doctor_report()
    caps = doc.get("capabilities", [])
    if not caps or any(c.get("status") not in ("AVAILABLE", "MISSING") for c in caps):
        return CheckResult("doctor_status", "FAIL", f"doctor did not report a valid AVAILABLE/MISSING status per capability: {caps}")
    return CheckResult("doctor_status", "PASS", f"{len(caps)} capabilities reported, each AVAILABLE or MISSING")


def check_dependency_version_ranges() -> CheckResult:
    """SKILL_SPEC.md #8: not applicable — this Skill declares no `dependencies` field in its contract (its only
    runtime dependency, FFmpeg, is a detected capability, not a versioned Skill dependency)."""
    if "dependencies" in skill_contract():
        return CheckResult("dependency_version_ranges", "FAIL", "contract now declares dependencies; this check needs real wiring")
    return CheckResult("dependency_version_ranges", "NOT_IMPLEMENTED", "not applicable: no dependencies field is published")


CHECKS: Dict[str, Callable[[], CheckResult]] = {
    "publishes_contract": check_publishes_contract,
    "forbidden_keys_rejected": check_forbidden_keys_rejected,
    "no_unsafe_shell_out": check_no_unsafe_shell_out,
    "workspace_confinement": check_workspace_confinement,
    "no_clobber_input": check_no_clobber_input,
    "lifecycle_declared": check_lifecycle_declared,
    "doctor_status": check_doctor_status,
    "dependency_version_ranges": check_dependency_version_ranges,
}


def run_conformance() -> Dict[str, Any]:
    """All eight SKILL_SPEC.md section 8 checks, run against this installation. `status` is `ok` (no FAIL), `fail`
    otherwise. Never raises: a check that itself errors is reported FAIL with the exception, not propagated."""
    results: List[CheckResult] = []
    for name, fn in CHECKS.items():
        try:
            results.append(fn())
        except Exception as e:  # a crashing check is a failed check, not a crashed process
            results.append(CheckResult(name, "FAIL", f"{type(e).__name__}: {e}"))
    status = "fail" if any(r.status == "FAIL" for r in results) else "ok"
    return {"schema": "media-analysis/conformance@1", "skill": {"id": "media-analysis", "version": VERSION},
            "spec": "AI-video-production-OS docs/SKILL_SPEC.md section 8", "status": status,
            "checks": [r.to_dict() for r in results]}
