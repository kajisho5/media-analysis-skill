# media-analysis-skill — instructions for Claude Code sessions

Read `STATE.md` first: it says what exists, what is incomplete, and the next highest-value task. Do not rely on
conversation history.

## What this repository is
A deterministic media **measurement** Skill (Python 3.9+, standard library, FFmpeg on PATH). It measures facts
about media files and returns Observations. It is one Provider in the `kajisho5/AI-video-production-OS` ecosystem
(sibling: `ffmpeg-skill` processes media; `video-production-agent` decides).

## Invariants (never break)
- No AI, no inference, no decision, no production planning, no media editing. Observations state what exists.
- Structured JSON in and out; `media-analysis run - --json` prints exactly one response document; stderr is diagnostics.
- No shell, no command / argv / executable override; the denylist in `AnalysisRequest.FORBIDDEN_KEYS` is never narrowed.
- `source = media-analysis/<tool>@<version>`, provenance OBSERVED (ADR-001).
- Contract `media-analysis/contract@1` changes are additive only; tools / kinds / provides are derived from the
  registry, never hand-written. `python -m pytest -q` must stay green on Linux / Windows / macOS; never skip or weaken tests.
- Standalone usability first; OS integration is additive.

## Where things are
`src/media_analysis/` (contract.py, engine.py, cli.py, analyzers/, cache / budget / security / verify /
contract_check), `tests/` (unit, contract, integration; fixtures generated with ffmpeg at test time), `evals/`
(measurement + contract evals), `docs/` (architecture, tools, observation, security, testing, decisions = ADRs).

## Working rules
- Run `python -m pyflakes src tests evals`, `python -m pytest -q`, `python evals/run.py`, `python evals/contract_evals.py` before pushing.
- Record architecture decisions in `docs/decisions.md`; update `STATE.md` and `docs/testing.md` counts when they change.
- Work on a branch, open a PR, let the 4-platform CI run. Merging is the human's call unless they delegated it.
