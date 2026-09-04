"""media-analysis-skill: deterministic media observation.

This package measures facts about media files (container, streams, video / audio format, silence, loudness,
integrity, scene cuts, timing) and returns them as structured Observations. It is not an AI agent: it never
interprets, decides, plans or edits. See SKILL.md and docs/architecture.md.
"""
SKILL_ID = "media-analysis"
PACKAGE_NAME = "media-analysis-skill"
VERSION = "0.1.0"

__all__ = ["SKILL_ID", "PACKAGE_NAME", "VERSION"]
