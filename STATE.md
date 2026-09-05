# Repository state

Last updated: 2026-09-05 (autonomous maintenance session). Version 0.1.0, contract `media-analysis/contract@1`.

## CURRENT (implemented, tested, on main)
- 10 analysis kinds / 9 tools, deterministic identity, cache (use / bypass / only), budget, process-group timeout,
  verification, path policy, response envelope, exit codes, error classes (FATAL / RETRYABLE / BLOCKED).
- Contract: `contract --json` (tools / kinds / provides / schemas / errors / security), `contract --check` drift
  detection with 21 mutation fixtures, `doctor --json` with per-Capability AVAILABLE / MISSING.
- OS integration (additive): `provides` publishes 10 Capability ids matching AI-video-production-OS
  `CAPABILITY_MATRIX.md`; denylist is a superset of `SKILL_SPEC.md` 3.1 and recursive; OS registry rules recorded
  in `tests/contract/os_registry_contract.json`; agent rules in `tests/contract/agent_skill_package_contract.json`.
- Tests: 104 (pytest), measurement evals 9/9, contract evals 17/17; CI on Linux 3.9 / 3.11, Windows, macOS with real FFmpeg.
- Distribution: `pip install -e .` from a clone. **Not on PyPI, no git tags / GitHub releases yet.**

## KNOWN LIMITATIONS
- Reading is unrestricted unless `--allowed-input` is given (documented; embedders must set roots).
- Packet-timestamp kinds hold one dict per packet in memory (~100 MB for an hour of A/V); no large-file optimisation.
- Windows timeout kills the process tree with `taskkill`; POSIX uses process groups. Verified in CI.
- Capability lifecycle is EXPERIMENTAL for all ids (no STABLE promise yet).

## NOT DONE / PLANNED (in value order)
1. First release: git tag `v0.1.0` + GitHub release notes (needs human go-ahead for the public release step).
2. OS conformance harness hooks: expose a machine-readable self-test (`doctor` already covers #1, #6, #7 of
   SKILL_SPEC 8; #2-#5 are covered by our own tests but not by an OS-runnable probe).
3. video-production-agent adapter (lives in that repository; recipe in docs/architecture.md). Blocked on the agent's
   PR stack (#4-#10) settling.
4. Optional MCP transport over the same request / response schemas (ADR-010: not before a consumer exists).

## NOT IN SCOPE (by design)
AI / inference / decisions, media editing, subtitles, transcription, semantic scene understanding, plugin loading.

## Recent history
- PR #1 Skill 0.1.0 + agent-ready contract (merged), PR #2 sponsor funnel, PR #3 README landing page,
  PR #4 `provides` (from another session; verified here with real FFmpeg and merged), PR #5 OS conformance hardening,
  PR #6 UTF-8 JSON protocol for non-ASCII paths (ADR-026, stacked on #5).
