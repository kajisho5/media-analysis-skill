"""A Dependabot pull request must not be able to raise the product's release level (major / minor).

Covers .github/scripts/dependabot_release_labels.py and its wiring in .github/workflows/release.yml.
"""
import importlib.util
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve()
while not (ROOT / ".github" / "release-drafter.yml").is_file():
    if ROOT.parent == ROOT:
        raise RuntimeError("repository root (.github/release-drafter.yml) not found")
    ROOT = ROOT.parent
SCRIPT = ROOT / ".github" / "scripts" / "dependabot_release_labels.py"

_spec = importlib.util.spec_from_file_location("dependabot_release_labels", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _pull(login, *labels):
    return {"number": 1, "user": {"login": login}, "labels": [{"name": n} for n in labels], "pull_request": {}}


def _labeled(label, login, actor_type, at):
    return {"event": "labeled", "label": {"name": label}, "actor": {"login": login, "type": actor_type},
            "created_at": at}


DEPENDABOT = "dependabot[bot]"
BY_DEPENDABOT = [
    _labeled("dependencies", DEPENDABOT, "Bot", "2026-11-06T14:44:19Z"),
    _labeled("major", DEPENDABOT, "Bot", "2026-11-06T14:44:21Z"),
    _labeled("chore", "github-actions[bot]", "Bot", "2026-11-06T14:44:28Z"),
]


def _config_release_level_labels():
    """Labels release-drafter.yml maps to a major or minor bump (`version-resolver`, or categories
    carrying `semver-increment`), read without a YAML library."""
    found = {"major": set(), "minor": set()}
    level = None
    collecting = False
    for line in (ROOT / ".github" / "release-drafter.yml").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        section = re.match(r"^\s+(major|minor|patch):\s*$", line)
        increment = re.match(r"^\s+semver-increment:\s*['\"]?(major|minor|patch)['\"]?\s*$", line)
        if section or increment:
            level = (section or increment).group(1)
            collecting = False
            continue
        if re.match(r"^\S", line) or re.match(r"^\s+- title:", line) or re.match(r"^\s+default:", line):
            level = None
            collecting = False
            continue
        if level not in found:
            continue
        if re.match(r"^\s+labels:", line):
            collecting = True
            found[level].update(re.findall(r"['\"]([^'\"]+)['\"]", line.split("labels:", 1)[1]))
        elif collecting and re.match(r"^\s+- ", line):
            found[level].add(line.strip()[2:].strip().strip("'\""))
        elif not re.match(r"^\s+when:\s*$", line):
            collecting = False
    return found


class LabelsToRemove(unittest.TestCase):
    def test_label_put_on_a_dependabot_pull_request_by_dependabot_is_removed(self):
        pull = _pull(DEPENDABOT, "dependencies", "github_actions", "chore", "major")
        self.assertEqual(mod.labels_to_remove(pull, BY_DEPENDABOT), ["major"])

    def test_every_release_level_label_is_removed_and_no_other(self):
        pull = _pull(DEPENDABOT, "dependencies", "patch", "fix", *mod.RELEASE_LEVEL_LABELS)
        self.assertEqual(mod.labels_to_remove(pull, []), list(mod.RELEASE_LEVEL_LABELS))

    def test_label_a_person_applied_to_a_dependabot_pull_request_stays(self):
        events = BY_DEPENDABOT[:1] + [_labeled("major", "a-maintainer", "User", "2026-11-07T09:00:00Z")]
        pull = _pull(DEPENDABOT, "dependencies", "major")
        self.assertEqual(mod.labels_to_remove(pull, events), [])

    def test_newest_labeled_event_decides(self):
        pull = _pull(DEPENDABOT, "dependencies", "major")
        person_then_bot = [
            _labeled("major", "a-maintainer", "User", "2026-11-07T09:00:00Z"),
            _labeled("major", DEPENDABOT, "Bot", "2026-11-08T09:00:00Z"),
        ]
        bot_then_person = [
            _labeled("major", DEPENDABOT, "Bot", "2026-11-06T09:00:00Z"),
            {"event": "unlabeled", "label": {"name": "major"}, "actor": {"login": "a-maintainer", "type": "User"},
             "created_at": "2026-11-07T09:00:00Z"},
            _labeled("major", "a-maintainer", "User", "2026-11-08T09:00:00Z"),
        ]
        self.assertEqual(mod.labels_to_remove(pull, person_then_bot), ["major"])
        self.assertEqual(mod.labels_to_remove(pull, bot_then_person), [])

    def test_label_applied_by_another_bot_is_removed_too(self):
        events = [_labeled("feature", "github-actions[bot]", "Bot", "2026-11-06T14:44:28Z")]
        self.assertEqual(mod.labels_to_remove(_pull(DEPENDABOT, "feature"), events), ["feature"])

    def test_label_name_case_is_ignored(self):
        events = [_labeled("Major", DEPENDABOT, "Bot", "2026-11-06T14:44:21Z")]
        self.assertEqual(mod.labels_to_remove(_pull(DEPENDABOT, "Major"), events), ["Major"])

    def test_pull_request_by_anyone_else_is_never_touched(self):
        events = [_labeled("feature", "github-actions[bot]", "Bot", "2026-11-06T14:44:28Z")]
        self.assertEqual(mod.labels_to_remove(_pull("a-maintainer", "feature", "major"), events), [])
        self.assertEqual(mod.labels_to_remove(_pull("some-other-app[bot]", "major"), []), [])


class Wiring(unittest.TestCase):
    def test_script_covers_every_major_and_minor_label_of_the_release_config(self):
        found = _config_release_level_labels()
        self.assertTrue(found["major"], "no major-level label found in .github/release-drafter.yml")
        self.assertTrue(found["minor"], "no minor-level label found in .github/release-drafter.yml")
        missing = {n.lower() for n in found["major"] | found["minor"]} - set(mod.RELEASE_LEVEL_LABELS)
        self.assertEqual(missing, set())

    def test_release_workflow_runs_the_script_before_resolving_the_version(self):
        text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
        script = [i for i, line in enumerate(lines) if "dependabot_release_labels.py" in line]
        drafter = [i for i, line in enumerate(lines) if re.search(r"uses:\s*release-drafter/release-drafter@", line)]
        self.assertEqual(len(script), 1)
        self.assertTrue(drafter, "release.yml no longer resolves the version with release-drafter")
        self.assertLess(script[0], min(drafter))
        step = "\n".join(lines[max(0, script[0] - 6):script[0] + 1])
        self.assertNotIn("continue-on-error", step)
        self.assertNotRegex(step, r"\n\s+if:")
        # removing a label needs a write token
        self.assertRegex("\n".join(lines), r"(?m)^\s+pull-requests:\s*write\b")


if __name__ == "__main__":
    unittest.main()
