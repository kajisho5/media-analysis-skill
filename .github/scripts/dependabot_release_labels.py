#!/usr/bin/env python3
"""Take release-level labels that a bot put on Dependabot pull requests off again.

Run by .github/workflows/release.yml right before release-drafter resolves the next version.

Why: Dependabot labels a pull request `major` (or `minor`) when the *dependency* it bumps moved a
major (or minor) version, as soon as a label of that name exists in the repository. That says nothing
about this product's own version, but release-drafter's version resolution reads the same label
names, so merging such a pull request cut a major product release.

Rule kept: a major (or minor) release happens only when a person applies the label. On a pull
request authored by Dependabot, a release-level label is therefore removed unless its newest
`labeled` event was made by a person. Pull requests by anyone else are never touched.

Pure logic lives in applied_by_person() and labels_to_remove(); main() wraps it with `gh api`.
Only label names, logins and event types are read from the API, never pull request text.
A failed API call fails the step, and with it the release: no version is resolved from labels
this script could not check.
"""
import json
import os
import subprocess
import sys
from urllib.parse import quote

# Every label that release-drafter.yml maps to a major or minor bump (the test suite checks that
# this covers the config).
RELEASE_LEVEL_LABELS = ("major", "breaking", "minor", "feature", "enhancement")
DEPENDABOT_LOGIN = "dependabot[bot]"


def applied_by_person(events, label):
    """True when the newest `labeled` event for `label` in an issue's events was made by a person."""
    newest = None
    for event in events:
        if event.get("event") != "labeled":
            continue
        if ((event.get("label") or {}).get("name") or "").lower() != label.lower():
            continue
        if newest is None or (event.get("created_at") or "") >= (newest.get("created_at") or ""):
            newest = event
    actor = (newest or {}).get("actor") or {}
    return actor.get("type") == "User"


def labels_to_remove(pull, events):
    """Label names to take off one pull request (an item of the issues API) given its events."""
    if ((pull.get("user") or {}).get("login") or "") != DEPENDABOT_LOGIN:
        return []
    names = [label.get("name") or "" for label in pull.get("labels") or []]
    return [
        name for name in names
        if name.lower() in RELEASE_LEVEL_LABELS and not applied_by_person(events, name)
    ]


def _gh_items(path):
    out = subprocess.run(
        ["gh", "api", "--paginate", path, "--jq", ".[] | @json"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    pulls = {}
    for label in RELEASE_LEVEL_LABELS:
        path = f"repos/{repo}/issues?state=all&creator=app%2Fdependabot&labels={quote(label)}&per_page=100"
        for item in _gh_items(path):
            if item.get("pull_request"):
                pulls[item["number"]] = item
    removed = 0
    for number in sorted(pulls):
        events = _gh_items(f"repos/{repo}/issues/{number}/events?per_page=100")
        for name in labels_to_remove(pulls[number], events):
            subprocess.run(
                ["gh", "api", "-X", "DELETE", f"repos/{repo}/issues/{number}/labels/{quote(name)}", "--silent"],
                check=True,
            )
            print(f"#{number}: removed `{name}` (put there by a bot on a Dependabot pull request)")
            removed += 1
    print(f"{len(pulls)} Dependabot pull request(s) carried a release-level label; {removed} label(s) removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
