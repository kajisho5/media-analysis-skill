"""Velisa ADR-031 section 5 (decision D46): from 1.0.0, a contract:breaking pull request needs a major label.

The rule lives in .github/scripts/release_gate.py: the release job stops a non-major release that carries such a pull
request, and .github/workflows/major-label-check.yml fails the pull request itself. On 0.x nothing changes.
This file is identical in every repository that has release_gate.py.
"""
import importlib.util
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, ".github", "scripts", "release_gate.py")
CHECK = os.path.join(ROOT, ".github", "workflows", "major-label-check.yml")
MAJOR = frozenset({"major"})


def _load_gate():
    spec = importlib.util.spec_from_file_location("release_gate", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class NeedsMajorTests(unittest.TestCase):
    def setUp(self):
        self.gate = _load_gate()

    def test_breaking_without_major_on_1x_needs_major(self):
        for version in ("1.0.0", "v1.0.0", "1.4.5", "v2.3.1"):
            self.assertTrue(self.gate.needs_major(["contract:breaking"], version, MAJOR), version)
            self.assertTrue(self.gate.needs_major(["contract:breaking", "fix"], version, MAJOR), version)
            self.assertTrue(self.gate.needs_major(["Contract:Breaking", "feature"], version, MAJOR), version)

    def test_a_major_label_lets_it_through(self):
        self.assertFalse(self.gate.needs_major(["contract:breaking", "major"], "1.0.0", MAJOR))
        self.assertFalse(self.gate.needs_major(["contract:breaking", "breaking"], "1.0.0", frozenset({"major", "breaking"})))

    def test_other_contract_labels_need_nothing(self):
        for labels in (["contract:none"], ["contract:additive", "feature"], ["fix"], []):
            self.assertFalse(self.gate.needs_major(labels, "1.2.0", MAJOR), labels)

    def test_0x_is_unchanged(self):
        for version in ("0.1.0", "v0.10.3", "0.99.99", "", None, "not-a-version"):
            self.assertFalse(self.gate.needs_major(["contract:breaking"], version, MAJOR), version)

    def test_the_major_labels_are_this_repository_s_own(self):
        major = self.gate.major_labels()
        self.assertIn("major", major)
        self.assertNotIn("contract:breaking", major)
        self.assertNotIn("minor", major)
        self.assertNotIn("patch", major)
        self.assertLessEqual(major, self.gate.release_labels())

    def test_major_labels_from_either_form_of_release_drafter_yml(self):
        resolver = "version-resolver:\n  major:\n    labels:\n      - 'major'\n      # 'x'\n      - 'breaking'\n" \
                   "  minor:\n    labels: ['minor']\n  default: patch\n"
        self.assertEqual(self.gate.drafter_major_labels(resolver), {"major", "breaking"})
        categories = "categories:\n  - title: 'Breaking'\n    semver-increment: 'major'\n    when:\n      labels: ['major']\n" \
                     "  - title: 'Features'\n    semver-increment: 'minor'\n    when:\n      labels: ['feature']\n"
        self.assertEqual(self.gate.drafter_major_labels(categories), {"major"})
        self.assertIsNone(self.gate.drafter_major_labels("template: x\n"))


class ReleaseStopsTests(unittest.TestCase):
    """The release job: a non-major release with a contract:breaking pull request (and no major label) stops."""

    def setUp(self):
        self.gate = _load_gate()

    def test_a_patch_release_of_a_breaking_pull_request_on_1x_stops(self):
        commits = [("fix: drop the old field (#9)", [["contract:breaking"]]), ("ci: x (#8)", [["ci", "contract:none"]])]
        reasons = self.gate.major_blockers(commits, "v1.0.0", MAJOR)
        self.assertEqual(len(reasons), 1)
        self.assertIn("contract:breaking", reasons[0])
        self.assertIn("#9", reasons[0])
        # the ADR-023 verdict alone would have released it (contract:* only counts as unlabelled)
        self.assertTrue(self.gate.decide(commits, frozenset({"major", "minor", "fix"}))[0])

    def test_a_minor_release_stops_too(self):
        commits = [("feat: rename a parameter (#9)", [["contract:breaking", "feature"]])]
        self.assertTrue(self.gate.major_blockers(commits, "v1.3.2", MAJOR))

    def test_a_major_release_goes(self):
        labelled = [("feat!: x (#9)", [["contract:breaking", "major"]])]
        self.assertEqual(self.gate.major_blockers(labelled, "v1.0.0", MAJOR), [])
        # another merged pull request carries major: release-drafter resolves a major release
        other = [("fix: x (#9)", [["contract:breaking"]]), ("feat!: y (#10)", [["major"]])]
        self.assertEqual(self.gate.major_blockers(other, "v1.0.0", MAJOR), [])
        # version bumped by hand to the next major
        self.assertEqual(self.gate.major_blockers([("fix: x (#9)", [["contract:breaking"]])], "v1.4.0", MAJOR,
                                                  new_version="2.0.0"), [])

    def test_a_version_bumped_by_hand_within_the_major_stops(self):
        commits = [("fix: x (#9)", [["contract:breaking", "major"]])]
        # in manual mode the labels do not pick the version: 1.5.0 is what would ship
        self.assertTrue(self.gate.major_blockers(commits, "v1.4.0", MAJOR, new_version="1.5.0"))

    def test_0x_releases_as_before(self):
        commits = [("fix: x (#9)", [["contract:breaking"]])]
        self.assertEqual(self.gate.major_blockers(commits, "v0.4.4", MAJOR), [])
        self.assertEqual(self.gate.major_blockers(commits, "", MAJOR), [])

    def test_no_breaking_pull_request_goes(self):
        commits = [("fix: x (#9)", [["fix", "contract:none"]]), ("feat: y (#10)", [["contract:additive"]])]
        self.assertEqual(self.gate.major_blockers(commits, "v1.0.0", MAJOR), [])

    def test_main_stops_the_release_and_says_why(self):
        import contextlib
        import io
        import tempfile
        gate = self.gate
        saved = (gate._git, gate._merged_pull_labels, gate.file_version, os.environ.get("GITHUB_REPOSITORY"))
        gate._git = lambda *args: "abc fix: drop a field (#9)\n"
        gate._merged_pull_labels = lambda repo, sha: [["contract:breaking"]]
        gate.file_version = lambda path=None: "1.0.0"
        os.environ["GITHUB_REPOSITORY"] = "owner/repo"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = os.path.join(tmp, "out")
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = gate.main(["--since", "v1.0.0", "--github-output", out])
                self.assertEqual(code, 1)
                self.assertEqual(_read(out), "releasable=false\n")
                self.assertIn("::error::contract:breaking", buf.getvalue())
                self.assertIn("ADR-031", buf.getvalue())
                gate._merged_pull_labels = lambda repo, sha: [["contract:breaking", "major"]]
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(gate.main(["--since", "v1.0.0", "--github-output", out]), 0)
                self.assertTrue(_read(out).endswith("releasable=true\n"))
                gate._merged_pull_labels = lambda repo, sha: [["contract:breaking"]]
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(gate.main(["--since", "v0.9.0", "--github-output", out]), 0)
                self.assertTrue(_read(out).endswith("releasable=true\n"))
        finally:
            gate._git, gate._merged_pull_labels, gate.file_version = saved[:3]
            if saved[3] is None:
                os.environ.pop("GITHUB_REPOSITORY", None)
            else:
                os.environ["GITHUB_REPOSITORY"] = saved[3]


class PullRequestCheckTests(unittest.TestCase):
    """major-label-check.yml: the pull request itself fails."""

    def setUp(self):
        self.gate = _load_gate()

    def test_breaking_without_major_on_1x_fails(self):
        ok, message = self.gate.pr_verdict(["contract:breaking", "fix"], "v1.0.0", "1.0.0", MAJOR)
        self.assertFalse(ok)
        self.assertIn("'major'", message)

    def test_with_major_or_a_hand_bumped_major_passes(self):
        self.assertTrue(self.gate.pr_verdict(["contract:breaking", "major"], "v1.0.0", "1.0.0", MAJOR)[0])
        self.assertTrue(self.gate.pr_verdict(["contract:breaking"], "v1.2.0", "2.0.0", MAJOR)[0])

    def test_a_hand_bumped_minor_fails_even_with_major(self):
        self.assertFalse(self.gate.pr_verdict(["contract:breaking", "major"], "v1.2.0", "1.3.0", MAJOR)[0])

    def test_0x_and_non_breaking_pass(self):
        self.assertTrue(self.gate.pr_verdict(["contract:breaking"], "v0.2.7", "0.2.7", MAJOR)[0])
        self.assertTrue(self.gate.pr_verdict(["contract:breaking"], "", "0.1.0", MAJOR)[0])
        self.assertTrue(self.gate.pr_verdict(["contract:additive", "feature"], "v1.0.0", "1.0.0", MAJOR)[0])
        self.assertTrue(self.gate.pr_verdict([], "v1.0.0", "1.0.0", MAJOR)[0])

    def test_the_version_file_is_read(self):
        version = self.gate.file_version()
        self.assertIsNotNone(self.gate.parse_version(version), version)

    def test_the_check_reruns_when_labels_change(self):
        text = _read(CHECK)
        types = re.search(r"types:\s*\[([^\]]*)\]", text)
        self.assertIsNotNone(types)
        names = {name.strip() for name in types.group(1).split(",")}
        self.assertLessEqual({"opened", "synchronize", "reopened", "labeled", "unlabeled"}, names)
        self.assertIn("release_gate.py --check-pr", text)
        self.assertIn("--head-sha", text)
        self.assertIn("fetch-depth: 0", text)
        self.assertNotIn("pull_request_target", text)


if __name__ == "__main__":
    unittest.main()
