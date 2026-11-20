#!/usr/bin/env python3
"""Decide whether what was merged since the latest release tag is worth a release (Velisa ADR-023, D37 and its addendum).

Run by .github/workflows/release.yml before the version is resolved, in auto mode only (the version
in the version file equals the latest tag). A version bumped by hand in a pull request is always
released; this script is not asked about it.

Rule, per pull request merged since the tag (the same as imagemagick-skill's resolve_version.py):

    a release-level label                  -> releases (release-drafter picks the level)
    dependencies (Dependabot)              -> releases, as a patch (the ecosystem's rule since the D37 addendum)
    no label at all                        -> releases (a pull request nobody labelled still ships, as a patch)
    contract:* only (contract-diff bot)    -> counts as no label at all, so it releases
    any other labels only (chore, ci, docs, documentation, test, maintenance, refactor, ...)
                                           -> does not release

The release-level labels are the ones .github/release-drafter.yml's `version-resolver` maps to major,
minor or patch, so the two can never disagree; that block maps `dependencies` to a patch and lists no
chore / ci / docs / test label.
Without a `version-resolver` block, they are the labels of the categories that carry a
`semver-increment`, less MAINTENANCE_LABELS; without either, RELEASE_LABELS.

The contract:none / contract:additive / contract:breaking labels are put on every pull request by Velisa's
contract-diff workflow (ADR-020). They describe the contract change, not the release level, so they are left
out before the rule above is applied (CONTRACT_LABEL_PREFIX); otherwise a fix pull request carrying only
`contract:none` would count as "other labels only" and release nothing. A contract:* label that
`version-resolver` itself maps to a bump is still counted.

From a Skill's 1.0.0, a breaking contract change ships only in a major release (Velisa ADR-031 section 5,
decision D46). So when the latest tag is 1.0.0 or later, a merged pull request labelled `contract:breaking` that
carries none of the labels `version-resolver` maps to major (MAJOR_LABELS when it maps none) stops the release:
the reason is printed, `releasable=false` is written and the script exits 1. Each such pull request needs its own
major label, as on the pull request itself. When the version was bumped by hand (or cannot be read) the labels do
not pick the version, so only a version file whose major is above the tag's lets the release go. On 0.x nothing changes: si contract-diff's rule there is that a breaking
change needs only a minor bump (ADR-020), and the labels already give at least a patch.
The same rule runs on every pull request, from .github/workflows/major-label-check.yml (`--check-pr`): from 1.0.0 it
waits for that commit's contract-diff run, reads the pull request's labels as they are then, and fails the check. It
also fails when that run did not succeed or never came, or left no contract:* label (a fork's token cannot write
one), as the classification is then not known.

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
    python3 .github/scripts/release_gate.py --check-pr 12 [--head-sha <sha>]

Without --since the newest `v*` tag is used (by version order); with no tag at all this is the first release,
which is always made (`releasable=true`).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

DRAFTER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "release-drafter.yml")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VERSION_FILE = os.path.join(ROOT, "pyproject.toml")
CONTRACT_DIFF_WORKFLOW = "contract-diff.yml"
RELEASE_LABELS = frozenset({
    "major", "breaking",
    "minor", "feature", "enhancement",
    "patch", "fix", "bug", "dependencies",
})
MAINTENANCE_LABELS = frozenset({
    "chore", "ci", "docs", "documentation", "test", "tests", "maintenance", "refactor", "style", "build",
    "skip-changelog",
})
# Labels the contract-diff bot puts on every pull request (Velisa ADR-020); not a release level.
CONTRACT_LABEL_PREFIX = "contract:"
# Velisa ADR-031 section 5: from 1.0.0, a pull request with this label needs a major label (D46).
BREAKING_LABEL = "contract:breaking"
# The major labels when release-drafter.yml names none.
MAJOR_LABELS = frozenset({"major", "breaking"})
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


def drafter_major_labels(text):
    """The label names release-drafter.yml maps to a major bump: `version-resolver.major.labels`, else the labels of
    the categories whose `semver-increment` is major. None when the file names neither."""
    m = re.search(r"(?ms)^version-resolver:\n(.*?)(?=^\S)", text + "\nend:")
    if m:
        major = re.search(r"(?ms)^  major:\n(.*?)(?=^  \S|\Z)", m.group(1))
        block = re.sub(r"(?m)^\s*#.*$", "", major.group(1)) if major else ""
        names = re.findall(r"""['"]([^'"]+)['"]""", block)
        return frozenset(name.lower() for name in names) or None
    m = re.search(r"(?ms)^categories:\n(.*?)(?=^\S)", text + "\nend:")
    if not m:
        return None
    names = set()
    for entry in re.split(r"(?m)^  - ", m.group(1)):
        if not re.search(r"""(?m)^\s*semver-increment:\s*['"]?major['"]?\s*$""", entry):
            continue
        for line in re.findall(r"(?m)^\s*labels:\s*\[(.*)\]\s*$", entry):
            names.update(n.lower() for n in re.findall(r"""['"]([^'"]+)['"]""", line))
    return frozenset(names) or None


def major_labels(path=DRAFTER):
    try:
        with open(path, encoding="utf-8") as fh:
            return drafter_major_labels(fh.read()) or MAJOR_LABELS
    except FileNotFoundError:
        return MAJOR_LABELS


def parse_version(text):
    """'v1.2.3' or '1.2.3' (a pre-release or build suffix is ignored) -> (1, 2, 3); None when it is not one."""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", (text or "").strip())
    return tuple(int(part) for part in m.groups()) if m else None


def file_version(path=VERSION_FILE):
    """The version in pyproject.toml's [project] table (no tomllib: the tests also run on Python 3.9)."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return None
    m = re.search(r"(?ms)^[ \t]*\[[ \t]*project[ \t]*\][ \t]*(?:#[^\n]*)?$(.*?)(?=^[ \t]*\[|\Z)", text)
    if not m:
        return None  # never a version from another table
    found = re.search(r"""(?m)^[ \t]*version[ \t]*=[ \t]*['"]([^'"]+)['"]""", m.group(1))
    return found.group(1) if found else None


def needs_major(labels, version, major=None):
    """True when a pull request with these labels may not ship in a non-major release of a Skill at `version`.

    That is: `version` is 1.0.0 or later, the pull request is labelled contract:breaking, and it carries no major
    label. On 0.x (or with no version yet) it is always False.
    """
    major = major_labels() if major is None else major
    parsed = parse_version(version)
    if parsed is None or parsed[0] < 1:
        return False
    names = {name.lower() for name in labels}
    return BREAKING_LABEL in names and not (names & major)


def major_blockers(commits, version, major=None, new_version=None):
    """commits as for decide(), `version` the latest tag -> the reasons the release must stop (empty: it may go).

    `new_version` is the version file's when it was bumped by hand (manual mode; "" when it cannot be read), None in
    auto mode. In auto mode each contract:breaking pull request needs its own major label, as on the pull request
    itself. In manual mode the labels do not pick the version: only a version file whose major is above the tag's
    lets them go.
    """
    major = major_labels() if major is None else major
    current, bumped = parse_version(version), parse_version(new_version)
    if current is None or current[0] < 1:
        return []
    if new_version is not None and bumped is not None and bumped[0] > current[0]:
        return []
    reasons = []
    for subject, pulls in commits:
        for labels in pulls:
            if new_version is not None and BREAKING_LABEL in {name.lower() for name in labels}:
                # bumped by hand: the labels do not pick the version, so a major label does not help
                shipped = f"bumped by hand to {new_version}" if new_version else "whose version file cannot be read"
                reasons.append(f"{BREAKING_LABEL} in a release {shipped}, not a major: {subject}")
            elif new_version is None and needs_major(labels, version, major):
                reasons.append(f"{BREAKING_LABEL} without {'/'.join(sorted(major))}: {subject}")
    return reasons


def bumped_by_hand(version, tag):
    """True when the version file no longer equals the tag less its `v`: release.yml's manual mode, which ships the
    file's version as written. Whole strings are compared, as release.yml does (`1.2.3+build.1` is not `1.2.3`)."""
    tag = (tag or "").strip()
    if not tag:
        return False
    if version is None:
        return True  # unreadable here is not "unchanged": release.yml reads it its own way
    return version.strip() != (tag[1:] if tag.startswith("v") else tag)


def _major_name(major):
    return "major" if "major" in major else sorted(major)[0]


def pr_verdict(labels, tag_version, head_version, major=None, need_contract_label=False):
    """The pull request check: (ok, message) for a pull request with these labels.

    `tag_version` is the latest release tag, `head_version` the version file on the pull request. When the pull
    request bumps the version by hand, that version is what ships, so only a higher major passes; otherwise a
    major label does (release-drafter then resolves a major release). `need_contract_label`: contract-diff ran
    and succeeded, so a pull request with no contract:* label at all was not classified (a fork's token cannot
    label) and fails.
    """
    major = major_labels() if major is None else major
    names = sorted({name.lower() for name in labels})
    current = parse_version(tag_version)
    if current is None or current[0] < 1:
        return True, f"{tag_version or 'no release tag'} is before 1.0.0: a contract:breaking change needs no major label"
    if need_contract_label and not any(name.startswith(CONTRACT_LABEL_PREFIX) for name in names):
        return False, ("contract-diff put no contract:* label on this pull request (a fork's token cannot write one), "
                       "so whether it is breaking is not known. A maintainer reads contract-diff's summary and adds "
                       "contract:none, contract:additive or contract:breaking; the check then runs again")
    if BREAKING_LABEL not in names:
        return True, f"not labelled {BREAKING_LABEL}"
    head = parse_version(head_version)
    if bumped_by_hand(head_version, tag_version):
        if head_version is None:
            return False, (f"labelled {BREAKING_LABEL} on {tag_version}, and the version in pyproject.toml cannot be "
                           f"read, so whether this is a major release is not known")
        if head is not None and head[0] > current[0]:
            return True, f"the version is bumped by hand to {head_version}, a major release"
        return False, (f"labelled {BREAKING_LABEL}, and the version is bumped by hand to {head_version}, which is not a "
                       f"major release after {tag_version} (Velisa ADR-031: from 1.0.0 a breaking change ships only in "
                       f"a major release). Bump the major version, or leave the version alone and add the "
                       f"'{_major_name(major)}' label")
    if not needs_major(names, tag_version, major):
        return True, f"labelled {BREAKING_LABEL} and {'/'.join(sorted(set(names) & major))}: a major release"
    return False, (f"labelled {BREAKING_LABEL} on {tag_version} without a major label ({', '.join(sorted(major))}). "
                   f"Velisa ADR-031: from 1.0.0 a breaking change ships only in a major release. Add the "
                   f"'{_major_name(major)}' label if this is meant to be a major release, or remove the breaking change")


def release_labels(path=DRAFTER):
    try:
        with open(path, encoding="utf-8") as fh:
            return drafter_release_labels(fh.read()) or RELEASE_LABELS
    except FileNotFoundError:
        return RELEASE_LABELS


def pull_releases(labels, release=None):
    """True when one merged pull request with these label names is worth a release.

    contract:* labels from the contract-diff bot are not counted, unless `release` names them.
    """
    release = release_labels() if release is None else release
    names = {name.lower() for name in labels}
    names = {name for name in names if not name.startswith(CONTRACT_LABEL_PREFIX) or name in release}
    if not names:
        return True
    return bool(names & release)


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


def _gh_json(path):
    out = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=True).stdout
    return json.loads(out or "null")


def _wait_for_contract_diff(repo, sha, appear=120, finish=1500, every=15):
    """Wait for contract-diff on `sha` (it labels with GITHUB_TOKEN, which starts no run) -> "ok", "absent" or a reason.

    "ok": the newest pull_request run on `sha` finished with success, so its label is on the pull request. "absent":
    this repository has no contract-diff.yml. Anything else says why the labels cannot be trusted yet (no run, a run
    that failed or was cancelled, or a time-out).
    """
    if not os.path.exists(os.path.join(ROOT, ".github", "workflows", CONTRACT_DIFF_WORKFLOW)):
        return "absent"
    start = time.monotonic()
    time.sleep(every)  # the run this same event starts may not be listed yet (reopened: an older run would be)
    while True:
        runs = _gh_json(f"repos/{repo}/actions/workflows/{CONTRACT_DIFF_WORKFLOW}/runs"
                        f"?head_sha={sha}&event=pull_request&per_page=50")
        runs = sorted((runs or {}).get("workflow_runs", []), key=lambda run: (run.get("created_at") or "", run.get("id") or 0))
        waited = time.monotonic() - start
        if runs and all(run.get("status") == "completed" for run in runs):
            conclusion = runs[-1].get("conclusion")
            if conclusion == "success":
                return "ok"
            return f"the newest contract-diff run on {sha[:12]} ended {conclusion}"
        if not runs and waited >= appear:
            return f"no contract-diff run on {sha[:12]} after {appear}s"
        if waited >= finish:
            return f"contract-diff on {sha[:12]} has not finished after {finish}s"
        time.sleep(every)


def _on_base(base, path):
    """True when `path` exists on the base branch (the checkout has every branch: fetch-depth 0)."""
    try:
        _git("cat-file", "-e", f"origin/{base}:{path}")
        return True
    except subprocess.CalledProcessError:
        return False


def check_pr(number, head_sha=None):
    """The pull request check (major-label-check.yml): 0 when the pull request may merge, 1 when it may not."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        print("release_gate.py: GITHUB_REPOSITORY is required to read pull request labels", file=sys.stderr)
        return 2
    tags = _git("tag", "--list", "v*", "--sort=-v:refname").split()
    tag = tags[0] if tags else ""
    current = parse_version(tag)
    if current is None or current[0] < 1:
        print(f"{tag or 'no release tag'} is before 1.0.0: nothing to check")
        return 0
    waited = None
    if head_sha:
        waited = _wait_for_contract_diff(repo, head_sha)
        base = os.environ.get("GITHUB_BASE_REF") or "main"
        if waited == "absent" and _on_base(base, f".github/workflows/{CONTRACT_DIFF_WORKFLOW}"):
            print(f"::error::this pull request removes {CONTRACT_DIFF_WORKFLOW}, which {base} has, so it is not classified")
            return 1
        if waited == "absent":
            print(f"no {CONTRACT_DIFF_WORKFLOW} here: the labels are read as they are")
        elif waited != "ok":
            print(f"::error::{waited}, so this pull request's contract:* label is not known. Re-run contract-diff, "
                  f"then this check.")
            return 1
    labels = [label["name"] for label in _gh_json(f"repos/{repo}/issues/{number}/labels?per_page=100") or []]
    version = file_version()
    ok, message = pr_verdict(labels, tag, version, need_contract_label=head_sha is not None and waited == "ok")
    print(f"labels: {', '.join(sorted(labels)) or '(none)'}; latest tag: {tag}; version file: {version}")
    print(message if ok else f"::error::{message}")
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", help="the latest release tag (default: the newest v* tag)")
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    parser.add_argument("--check-pr", type=int, metavar="NUMBER",
                        help="check one pull request instead: contract:breaking on 1.x needs a major label")
    parser.add_argument("--head-sha", help="with --check-pr: wait for contract-diff's run on this commit first")
    args = parser.parse_args(argv)
    if args.check_pr is not None:
        return check_pr(args.check_pr, args.head_sha)
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
    version = file_version()
    manual = bumped_by_hand(version, since)
    blockers = major_blockers(commits, since, new_version=(version or "") if manual else None)
    if blockers:
        for reason in blockers:
            print(f"::error::{reason}")
        print(f"since {since}: no release. Velisa ADR-031: from 1.0.0 a breaking change ships only in a major release. "
              f"Add the '{_major_name(major_labels())}' label to the pull request above and run the release again, or bump "
              f"the major version by hand; remove {BREAKING_LABEL} only if the breaking change was reverted.")
        _write(args.github_output, False)
        return 1
    print(f"since {since}: {'release' if releasable else 'nothing to release (only chore/ci/docs/test changes)'}")
    _write(args.github_output, releasable)
    return 0


def _write(github_output, releasable):
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"releasable={'true' if releasable else 'false'}\n")


if __name__ == "__main__":
    sys.exit(main())
