"""media-analysis-skill: deterministic media observation.

This package measures facts about media files (container, streams, video / audio format, silence, loudness,
integrity, scene cuts, timing) and returns them as structured Observations. It is not an AI agent: it never
interprets, decides, plans or edits. See SKILL.md and docs/architecture.md.
"""
SKILL_ID = "media-analysis"
PACKAGE_NAME = "media-analysis-skill"
def _version() -> str:
    """The package version, from one place: this checkout's pyproject.toml when running from source,
    otherwise the installed distribution's metadata. Releases bump only pyproject.toml."""
    import re as _re
    from pathlib import Path as _Path
    pyproject = _Path(__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8")
        if _re.search(r'^name\s*=\s*"media-analysis-skill"', text, _re.M):
            found = _re.search(r'^version\s*=\s*"([^"]+)"', text, _re.M)
            if found:
                return found.group(1)
    import sys as _sys
    for base in _sys.path:
        for meta in sorted(_Path(base or ".").glob("media_analysis_skill-*.dist-info/METADATA")):
            found = _re.search(r"^Version: *(\S+)", meta.read_text(encoding="utf-8", errors="replace"), _re.M)
            if found:
                return found.group(1)
    return "0.0.0+unknown"


VERSION = _version()

__all__ = ["SKILL_ID", "PACKAGE_NAME", "VERSION"]
