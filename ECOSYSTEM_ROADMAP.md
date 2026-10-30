# Roadmap — media-analysis-skill

Part of the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS/blob/main/docs/ecosystem/ROADMAP.md) ecosystem: department **検証 (Verification)**, status `active`.
Written from a read-only audit of this repository on 2026-10-30. Each step below is meant to be one pull request; tick it off by linking the PR.

## Where it stands

Version single-sourced from `pyproject.toml` (#17): `contract --json` → `version` equals the newest tag (0.1.4 at `v0.1.4`, checked 2026-10-30). Cache keyed by per-analyzer algorithm version, so releases no longer empty it (ADR-028). 10 measurement kinds. 80 test functions (111 collected), CI on 3 OSes. STATE.md current (#18).

## What this repository owns

`ecosystem.json` gives each job to exactly one repository. Work outside this list belongs to its owner and is handed over, not re-implemented.

| Owned job | Status | Note |
|---|---|---|
| メディア計測 | done |  |

## Wiring

All 9 tools wired in the agent. Calls ffmpeg/ffprobe directly (overlap with ffmpeg-skill probe/silence/loudness/scenes is intentional).

## Steps

- [x] **1. Single-source the version** — coordinated with the agent's version gate — [#17](https://github.com/kajisho5/media-analysis-skill/pull/17)
  - Done when: contract version == tag
- [x] **2. Refresh STATE.md** — [#18](https://github.com/kajisho5/media-analysis-skill/pull/18)
- [ ] **3. Measurements new skills need for acceptance** — noise floor / SNR (voice-enhance), A/V offset -- only if not qc-skill's
  - Done when: evals

---
Engine for every media operation: [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) (42 tools, treated as complete). Orchestrator: [video-production-agent](https://github.com/kajisho5/video-production-agent).
