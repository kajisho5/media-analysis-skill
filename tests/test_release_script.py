""".github/scripts/release.py on a throwaway repository: the contract generator is found from a relative
RELEASE_SCRIPT_ROOT, and a committed contract without its generator stops the release before anything is written."""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve()
while not (ROOT / ".github" / "release-drafter.yml").is_file():
    if ROOT.parent == ROOT:
        raise RuntimeError("repository root (.github/release-drafter.yml) not found")
    ROOT = ROOT.parent
SCRIPT = ROOT / ".github" / "scripts" / "release.py"

GENERATOR = """import pathlib
out = pathlib.Path(__file__).resolve().parent / "contract.json"
version = pathlib.Path(__file__).resolve().parent.parent.joinpath("pyproject.toml").read_text().split('"')[1]
out.write_text('{"version": "%s"}\\n' % version)
"""


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class ReleaseScriptTests(unittest.TestCase):
    def _repo(self, with_generator):
        tmp = tempfile.TemporaryDirectory(prefix="media-analysis-release-")
        self.addCleanup(tmp.cleanup)
        parent = pathlib.Path(tmp.name)
        repo = parent / "repo"
        (repo / "contract").mkdir(parents=True)
        (repo / "pyproject.toml").write_text('[project]\nversion = "0.2.0"\n', encoding="utf-8")
        (repo / "contract" / "contract.json").write_text('{"version": "0.1.0"}\n', encoding="utf-8")
        if with_generator:
            (repo / "contract" / "generate.py").write_text(GENERATOR, encoding="utf-8")
        _git(repo, "init", "-q")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "add", "-A")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "init")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "tag", "-a", "v0.1.0", "-m", "v0.1.0")
        return parent, repo

    def _release(self, parent):
        env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_OUTPUT", "RESOLVED_VERSION", "HAS_PYPI_TOKEN")}
        env["RELEASE_SCRIPT_ROOT"] = "repo"   # relative to the working directory below
        return subprocess.run([sys.executable, str(SCRIPT)], cwd=parent, env=env, capture_output=True, text=True)

    def test_relative_root_finds_the_contract_generator(self):
        parent, repo = self._repo(with_generator=True)
        r = self._release(parent)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((repo / "contract" / "contract.json").read_text(encoding="utf-8"), '{"version": "0.2.0"}\n')

    def test_committed_contract_without_generator_stops_before_the_bump(self):
        parent, repo = self._repo(with_generator=False)
        r = self._release(parent)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("contract/generate.py is missing", r.stderr)
        self.assertFalse((repo / "CHANGELOG.md").exists())
        tags = subprocess.run(["git", "tag", "--list"], cwd=repo, capture_output=True, text=True).stdout.split()
        self.assertEqual(tags, ["v0.1.0"])


if __name__ == "__main__":
    unittest.main()
