#!/usr/bin/env python3
"""Decide whether what was merged since the latest release tag is worth a release (Velisa ADR-023, D37 and its addendum).

Run by .github/workflows/release.yml before the version is resolved, in auto mode only (the version
in the version file equals the latest tag). A version bumped by hand in a pull request is always
released; this script is not asked about it.

Rule, per pull request merged since the tag (the same as imagemagick-skill's resolve_version.py):

    a release-level label                  -> releases (release-drafter picks the level)
    dependencies (Dependabot)              -> releases, as a patch (the ecosystem's rule since the D37 addendum)
    no label at all                        -> releases (a pull request nobody labelled still ships, as a patch)
    any other labels only (chore, ci, docs, documentation, test, maintenance, refactor, ...)
                                           -> does not release

The release-level labels are the ones .github/release-drafter.yml's `version-resolver` maps to major,
minor or patch, so the two can never disagree; that block maps `dependencies` to a patch and lists no
chore / ci / docs / test label.
Without a `version-resolver` block, they are the labels of the categories that carry a
`semver-increment`, less MAINTENANCE_LABELS; without either, RELEASE_LABELS.

A commit on main that came from no pull request releases unless its subject starts with
chore / ci / docs / build / test / refactor / style. Nothing releasable since the tag means
`releasable=false`, and the release job stops before it bumps anything.

release-drafter cannot say "no release" itself: its `version-resolver.default` only accepts
major, minor or patch. So this script decides whether to release, and release-drafter decides the
level of a release this script let through.

Pure logic lives in pull_releases() and decide(); main() wraps it with `git` and `gh api`. Only label
names and commit subjects are read, and nothing read is ever run or spliced into a shell command.

Usage:
    python3 .github/scripts/release_gate.py [--since v1.2.3] [--github-output "$GITHUB_OUTPUT"]

Without --since the newest `v*` tag is used (by version order); with no tag at all this is the first release,
which is always made (`releasable=true`).
"""
import argparse
import json
import os
import re
import subprocess
import sys

DRAFTER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "release-drafter.yml")
RELEASE_LABELS = frozenset({
    "major", "breaking",
    "minor", "feature", "enhancement",
    "patch", "fix", "bug", "dependencies",
})
MAINTENANCE_LABELS = frozenset({
    "chore", "ci", "docs", "documentation", "test", "tests", "maintenance", "refactor", "style", "build",
    "skip-changelog",
})
NON_RELEASING_SUBJECT = re.compile(r"^(chore|ci|docs|build|test|tests|refactor|style)(\(.+\))?!?:", re.I)


def drafter_release_labels(text):
    """The label names `version-resolver` maps to a bump, from release-drafter.yml's text (inline or block lists).

    Without a `version-resolver` block: the labels of the categories with a `semver-increment`, less
    MAINTENANCE_LABELS. None when the file says neither.
    """
    m = re.search(r"(?ms)^version-resolver:\n(.*?)(?=^\S)", text + "\nend:")
    if m:
        block = re.sub(r"(?m)^\s*default:.*$", "", m.group(1))
        names = re.findall(r"""['"]([^'"]+)['"]""", block)
        return frozenset(name.lower() for name in names) or None
    m = re.search(r"(?ms)^categories:\n(.*?)(?=^\S)", text + "\nend:")
    if not m:
        return None
    names = set()
    for entry in re.split(r"(?m)^  - ", m.group(1)):
        if "semver-increment:" not in entry:
            continue
        for line in re.findall(r"(?m)^\s*labels:\s*\[(.*)\]\s*$", entry):
            names.update(n.lower() for n in re.findall(r"""['"]([^'"]+)['"]""", line))
    return frozenset(names - MAINTENANCE_LABELS) or None


def release_labels(path=DRAFTER):
    try:
        with open(path, encoding="utf-8") as fh:
            return drafter_release_labels(fh.read()) or RELEASE_LABELS
    except FileNotFoundError:
        return RELEASE_LABELS


def pull_releases(labels, release=None):
    """True when one merged pull request with these label names is worth a release."""
    names = {name.lower() for name in labels}
    if not names:
        return True
    return bool(names & (release_labels() if release is None else release))


def decide(commits, release=None):
    """commits: [(subject, [label lists of the merged pull requests that carry it])] -> (releasable, reasons)."""
    release = release_labels() if release is None else release
    reasons = []
    releasable = False
    for subject, pulls in commits:
        if pulls:
            ok = any(pull_releases(labels, release) for labels in pulls)
            labels = sorted({name for pull in pulls for name in pull})
            reasons.append(f"{'release' if ok else '-':8} {','.join(labels) or '(unlabelled)':32} {subject}")
        else:
            ok = not NON_RELEASING_SUBJECT.match(subject)
            reasons.append(f"{'release' if ok else '-':8} {'(no pull request)':32} {subject}")
        releasable = releasable or ok
    return releasable, reasons


def _git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def _merged_pull_labels(repo, sha):
    out = subprocess.run(
        ["gh", "api", f"repos/{repo}/commits/{sha}/pulls", "--jq", "[.[] | select(.merged_at != null) | [.labels[].name]]"],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out or "[]")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", help="the latest release tag (default: the newest v* tag)")
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    args = parser.parse_args(argv)
    since = args.since
    if since is None:
        tags = _git("tag", "--list", "v*", "--sort=-v:refname").split()
        since = tags[0] if tags else ""
    if not since:
        print("no release tag yet: the first release is always made")
        _write(args.github_output, True)
        return 0
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        print("release_gate.py: GITHUB_REPOSITORY is required to read pull request labels", file=sys.stderr)
        return 2
    lines = _git("log", "--no-merges", "--format=%H %s", f"{since}..HEAD").splitlines()
    commits = []
    for line in lines:
        sha, _, subject = line.partition(" ")
        commits.append((subject, _merged_pull_labels(repo, sha)))
    releasable, reasons = decide(commits)
    for reason in reasons:
        print(f"  {reason}")
    print(f"since {since}: {'release' if releasable else 'nothing to release (only chore/ci/docs/test changes)'}")
    _write(args.github_output, releasable)
    return 0


def _write(github_output, releasable):
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"releasable={'true' if releasable else 'false'}\n")


if __name__ == "__main__":
    sys.exit(main())
