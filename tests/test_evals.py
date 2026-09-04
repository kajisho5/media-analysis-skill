"""The evals must pass in the test suite too (real ffmpeg, no tolerance widening at test time)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))

import run as evals  # noqa: E402


def test_evals_pass(media):
    results = evals.run_all()
    failed = [r for r in results if not r["ok"]]
    assert len(results) >= 6
    assert not failed, [(r["id"], [c for c in r["checks"] if not c["ok"]], r.get("error")) for r in failed]


import contract_evals  # noqa: E402


def test_contract_evals_pass(media):
    results = contract_evals.run_all()
    failed = [r for r in results if not r["ok"]]
    assert len(results) >= 6
    assert not failed, [(r["id"], [c for c in r["checks"] if not c["ok"]], r.get("error")) for r in failed]
