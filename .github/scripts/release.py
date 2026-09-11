#!/usr/bin/env python3
"""Single-job release automation, run on every push to main (.github/workflows/release.yml).

Version resolution:
  - No git tag exists yet          -> first release: tag whatever pyproject.toml already says.
  - pyproject.toml version != tag  -> someone already bumped it by hand: respect that value, tag it as-is.
  - pyproject.toml version == tag  -> auto-bump: use RESOLVED_VERSION (release-drafter dry-run output,
                                       computed from merged-PR labels; unlabeled PRs default to patch).

Nothing here interpolates untrusted text (PR titles, commit subjects, ...) into a shell command string: this
script itself is the only thing exec'd by the workflow's `run:` step, and every external string it touches
(RESOLVED_VERSION, commit subjects for the changelog) is read as *data* -- via os.environ or subprocess stdout --
never re-assembled into another shell command line.

Writes GITHUB_OUTPUT keys: released (true/false), new_version, new_tag, notes_file, should_publish (true/false).
"""
from __future__ import annotations

import datetime
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

ROOT = Path(os.environ["RELEASE_SCRIPT_ROOT"]) if os.environ.get("RELEASE_SCRIPT_ROOT") else Path(__file__).resolve().parent.parent.parent
PYPROJECT = ROOT / "pyproject.toml"
CHANGELOG = ROOT / "CHANGELOG.md"
VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
TAG_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")


def run(*args: str, check: bool = True) -> str:
    proc = subprocess.run(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(args)}\n{proc.stderr}")
    return proc.stdout.strip()


def read_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    m = VERSION_RE.search(text)
    if not m:
        raise RuntimeError("pyproject.toml has no [project] version field")
    return m.group(1)


def write_version(new_version: str) -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    new_text = VERSION_RE.sub(f'version = "{new_version}"', text, count=1)
    if new_text == text:
        raise RuntimeError("failed to rewrite pyproject.toml version")
    PYPROJECT.write_text(new_text, encoding="utf-8")


def latest_tag() -> Optional[str]:
    out = run("git", "describe", "--tags", "--abbrev=0", check=False)
    return out or None


def normalize(v: str) -> str:
    m = TAG_RE.match(v.strip())
    if not m:
        raise RuntimeError(f"not a semver-looking version: {v!r}")
    return m.group(1)


def changelog_entries(since_tag: Optional[str]) -> List[str]:
    """One line per commit since `since_tag` (or full history if there is none), each read from `git log`
    output -- never assembled from a `${{ }}`-interpolated string."""
    range_arg = f"{since_tag}..HEAD" if since_tag else "HEAD"
    log = run("git", "log", range_arg, "--no-merges", "--pretty=format:%s (%h)")
    return [line for line in log.splitlines() if line.strip()]


def write_changelog(version: str, entries: List[str]) -> str:
    """Prepend a dated section to CHANGELOG.md and return the section's body (without the header), for use as
    the GitHub Release notes -- written to a file rather than passed through `${{ }}` (see NOTES_FILE below)."""
    date = datetime.date.today().isoformat()
    header = f"## v{version} - {date}\n\n"
    body = "\n".join(f"- {e}" for e in entries) if entries else "- (no changes recorded)"
    section = header + body + "\n\n"
    existing = CHANGELOG.read_text(encoding="utf-8") if CHANGELOG.exists() else "# Changelog\n\n"
    if not existing.startswith("# Changelog"):
        existing = "# Changelog\n\n" + existing
    marker = "# Changelog\n\n"
    idx = existing.index(marker) + len(marker)
    CHANGELOG.write_text(existing[:idx] + section + existing[idx:], encoding="utf-8")
    return body


def set_output(**kwargs: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    lines = "\n".join(f"{k}={v}" for k, v in kwargs.items())
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(lines + "\n")
    else:
        print(lines)


def main() -> int:
    current = read_version()
    tag = latest_tag()

    if tag is None:
        new_version = normalize(current)
        reason = "no tag exists yet: first release tags the current pyproject.toml version"
    elif normalize(current) != normalize(tag):
        new_version = normalize(current)
        reason = f"pyproject.toml ({current}) already differs from the latest tag ({tag}): respecting the manual bump"
    else:
        resolved = os.environ.get("RESOLVED_VERSION", "").strip()
        if not resolved:
            print("RESOLVED_VERSION is empty and pyproject.toml already matches the latest tag; nothing to release.")
            set_output(released="false", new_version=current, should_publish="false")
            return 0
        new_version = normalize(resolved)
        reason = f"pyproject.toml matches the latest tag ({tag}): auto-bumping via release-drafter's resolved version {resolved!r}"

    new_tag = f"v{new_version}"
    existing_tags = set(run("git", "tag", "--list").splitlines())
    if new_tag in existing_tags:
        print(f"{new_tag} already exists; nothing to release.")
        set_output(released="false", new_version=new_version, should_publish="false")
        return 0

    print(f"Releasing {new_tag} ({reason})")

    if normalize(current) != new_version:
        write_version(new_version)

    entries = changelog_entries(tag)
    notes = write_changelog(new_version, entries)
    notes_file = ROOT / ".github" / "scripts" / ".release-notes.md"
    notes_file.parent.mkdir(parents=True, exist_ok=True)
    notes_file.write_text(notes + "\n", encoding="utf-8")

    run("git", "config", "user.name", "github-actions[bot]")
    run("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    run("git", "add", "pyproject.toml", "CHANGELOG.md")
    # nothing to commit is possible only if write_version() was a no-op and the changelog section is identical
    # to one already present (re-run on the same commit); guard rather than let `git commit` fail the job.
    status = run("git", "status", "--porcelain", "--", "pyproject.toml", "CHANGELOG.md")
    if status:
        run("git", "commit", "-m", f"Release {new_tag}")
    run("git", "tag", "-a", new_tag, "-m", f"Release {new_tag}")

    should_publish = "true" if os.environ.get("HAS_PYPI_TOKEN") == "true" else "false"
    set_output(released="true", new_version=new_version, new_tag=new_tag, notes_file=str(notes_file), should_publish=should_publish)
    return 0


if __name__ == "__main__":
    sys.exit(main())
