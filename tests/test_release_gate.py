"""The release job's gate and order (Velisa ADR-023): chore / ci / docs / test pull requests alone release nothing,
and the release commit is tested before it is pushed."""
import importlib.util
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, ".github", "scripts", "release_gate.py")
# Where release.yml makes the release commit (the test step must come after it and before every push).
COMMIT_MARKER = "run: python .github/scripts/release.py"  # it commits and tags
MAINTENANCE = {"chore", "ci", "docs", "documentation", "test", "tests", "maintenance", "refactor", "style", "build"}


def _load_gate():
    spec = importlib.util.spec_from_file_location("release_gate", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = _load_gate()

    def test_release_drafter_maps_no_maintenance_label_to_a_bump(self):
        labels = self.gate.drafter_release_labels(_read(".github", "release-drafter.yml"))
        self.assertTrue(labels and {"fix", "feature"} <= labels, labels)
        self.assertFalse(labels & MAINTENANCE, sorted(labels & MAINTENANCE))
        self.assertEqual(self.gate.release_labels(), labels)

    def test_release_level_labels_and_unlabelled_pull_requests_release(self):
        for labels in (["feature"], ["fix"], ["minor"], ["major"], ["ci", "fix"], []):
            self.assertTrue(self.gate.pull_releases(labels), labels)

    def test_maintenance_labels_alone_release_nothing(self):
        for labels in (["ci"], ["docs"], ["documentation"], ["chore"], ["test"], ["ci", "docs", "contract:none"]):
            self.assertFalse(self.gate.pull_releases(labels), labels)

    def test_dependencies_pull_requests_release_as_a_patch(self):
        # Velisa D37 addendum: a pull request labelled `dependencies` (Dependabot) is released, as a patch.
        self.assertIn("dependencies", self.gate.release_labels())
        for labels in (["dependencies"], ["dependencies", "github_actions"], ["dependencies", "chore"]):
            self.assertTrue(self.gate.pull_releases(labels), labels)
        self.assertTrue(self.gate.decide([("chore(deps): bump x (#3)", [["dependencies"]])])[0])

    def test_decide_over_the_commits_since_the_tag(self):
        self.assertFalse(self.gate.decide([("ci: x (#1)", [["ci"]]), ("docs: y (#2)", [["documentation"]])])[0])
        self.assertTrue(self.gate.decide([("ci: x (#1)", [["ci"]]), ("fix: y (#2)", [["fix"]])])[0])
        self.assertFalse(self.gate.decide([])[0])
        self.assertFalse(self.gate.decide([("chore: tidy", []), ("docs(readme): typo", []), ("test: more", [])])[0])
        self.assertTrue(self.gate.decide([("Fix the thing", [])])[0])


class ReleaseWorkflowOrderTests(unittest.TestCase):
    def test_gate_runs_and_the_release_commit_is_tested_before_any_push(self):
        text = _read(".github", "workflows", "release.yml")
        self.assertIn("python3 .github/scripts/release_gate.py", text)
        test_step = text.index("- name: Test the release commit before pushing it")
        self.assertLess(text.index(COMMIT_MARKER), test_step)
        pushes = [m.start() for m in re.finditer(r"git push", text)]
        self.assertTrue(pushes)
        for push in pushes:
            self.assertLess(test_step, push)

    def test_the_release_commit_is_tested_with_the_extras_ci_installs(self):
        # ADR-039: the release test step installs what tests.yml's test job installs, so a test that CI runs with an
        # optional extra (numpy, onnxruntime) is not run without it on the release commit.
        def installed_extras(text):
            m = re.search(r"pip install (?:-q )?-e \"?\.(?:\[([^\]]*)\])?\"?", text)
            self.assertIsNotNone(m, "no editable install of the package")
            return sorted(x.strip() for x in (m.group(1) or "").split(",") if x.strip())

        release = _read(".github", "workflows", "release.yml")
        step = release[release.index("- name: Test the release commit before pushing it"):]
        step = step[:step.index("- name:", 1)]
        ci = installed_extras(_read(".github", "workflows", "tests.yml"))
        self.assertTrue(ci)
        self.assertEqual(installed_extras(step), ci)


if __name__ == "__main__":
    unittest.main()
