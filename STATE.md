# Repository state

Last updated: 2026-10-30, checked against `main` at `45d07ad` (Release v0.1.3). Every statement below was checked
against the code, a command run or the GitHub API on that date; anything that could not be checked is marked
**unverified**.

## Version
- Single source: `pyproject.toml` `version`. `media_analysis.VERSION` (`src/media_analysis/__init__.py`, PR #17)
  reads this checkout's `pyproject.toml` when run from source, otherwise the installed `media_analysis_skill-*.dist-info`
  METADATA; there is no hard-coded constant. `media-analysis --version`, `contract --json` → `version`, every tool /
  analyzer `version`, `doctor` / `conformance` → `skill.version` and Observation `source` / `analysis.analyzer_version`
  all report it.
- The package version is provenance only. The analysis identity, observation id and cache key use each analyzer's
  own `algorithm_version` (declared in its class, `"1"` for all ten; ADR-028), so a release that does not change an
  analyzer keeps its cached Observations. Bump an analyzer's `algorithm_version` in the PR that changes its output.
- On `2d304bf` (Release v0.1.4) it is `0.1.4`, equal to the newest git tag `v0.1.4` (`contract --json` → `version`).
- The release workflow (`.github/workflows/release.yml` + `.github/scripts/release.py`) bumps `pyproject.toml`, writes
  `CHANGELOG.md`, tags and creates a GitHub release on **every push to `main`** (patch by default; minor / major from PR
  labels). So the number above goes stale with the next merge; read the live value with `media-analysis --version`.
- Tags / GitHub releases: `v0.1.0` … `v0.1.4`. Not on PyPI (`pypi.org/pypi/media-analysis-skill`
  returns 404; the publish step runs only when a `PYPI_API_TOKEN` secret exists). Install is `pip install -e .` from a clone.

## CURRENT (implemented, tested, on main)
- CLI `media-analysis` (`media_analysis.cli:main`), subcommands: `probe`, `analyze`, `run` (canonical machine
  interface: `run <request.json | -> --json`, one response document on stdout), `doctor`, `contract`, `conformance`.
- Contract `media-analysis/contract@1` (`schema_versions`: contract / request / response / observation all `1`).
  9 tools, 10 analysis kinds, derived from the analyzer registry:

  | Tool | Kinds | Capability id (`provides`) |
  |---|---|---|
  | `media-analysis/probe` | `media_probe` | `measure.media.probe` |
  | `media-analysis/streams` | `stream_layout` | `measure.media.stream_layout` |
  | `media-analysis/video` | `video_format` | `measure.video.probe` |
  | `media-analysis/audio` | `audio_format` | `measure.audio.probe` |
  | `media-analysis/timing` | `duration`, `timing` | `measure.media.duration`, `measure.video.timing` |
  | `media-analysis/silence` | `silence` | `measure.audio.silence` |
  | `media-analysis/loudness` | `loudness` | `measure.audio.loudness` |
  | `media-analysis/integrity` | `integrity` | `measure.audio.integrity` |
  | `media-analysis/scenes` | `scene_detection` | `measure.video.scene_detection` |

  All 10 `provides` entries are lifecycle `EXPERIMENTAL`. Required environment capabilities (`capability_names`):
  `ffmpeg`, `ffprobe`, `filter:ebur128`, `filter:scdet`, `filter:silencedetect`.
- Errors: 12 codes with exit codes 2–13 (`INVALID_INPUT` … `CACHE_MISS`), each with a class `FATAL` / `RETRYABLE` /
  `BLOCKED` (ADR-023). Execution is `local_subprocess`, ffmpeg / ffprobe found by PATH lookup only, no shell, no
  network, no AI. Request denylist (recursive, case-insensitive): `command argv args shell cmd exec filter_complex
  filter api_key token env` (ADR-024).
- Deterministic analysis identity, cache (use / bypass / only), budget, process-group timeout (POSIX `setsid` +
  `killpg`; Windows `CREATE_NEW_PROCESS_GROUP` + `taskkill /T /F`), verification, path policy.
- `contract --check <file>` drift detection (22 mutation fixtures in `tests/contract/cases`, ADR-020).
- `doctor --json` reports per Capability AVAILABLE / MISSING (ADR-025).
- `conformance --json` runs the 8 AI-video-production-OS `SKILL_SPEC.md` section 8 checks (ADR-027): 6 PASS,
  2 NOT_IMPLEMENTED as not applicable (`no_clobber_input`: no tool writes output; `dependency_version_ranges`: no
  `dependencies` field is published).
- Cross-check with the OS's own registry (read-only, AI-video-production-OS `main` `3e5bae1`, 2026-10-30):
  `registry.CapabilityRegistry.register_contract(<live contract>)` registers 10 providers,
  `providers_of("measure.audio.loudness")` resolves `media-analysis/loudness`, and `registry.conformance`
  `check_publishes_contract` / `check_lifecycle_declared` both PASS. This is a manual check, not a committed test.

## Who calls it
- video-production-agent (`kajisho5/video-production-agent`, read-only check of `main` `33d739f`, 2026-10-30):
  - Adapter `src/video_agent/tools/media_analysis/` (`adapter.py` 297 lines, `locate.py`, pinned
    `contract_0.1.3.json`). Version gate `SUPPORTED_SKILL_VERSIONS = ("0.1.",)`, `SUPPORTED_CONTRACT_VERSIONS = ("1",)`.
    Its `check_contract` accepts this repository's live 0.1.3 contract with no errors, and the pinned snapshot's
    `tools` and `provides` equal the live ones.
  - Located from `VIDEO_AGENT_MEDIA_ANALYSIS_DIR` / an explicit dir, then `~/.claude/skills/media-analysis-skill`,
    `./vendor/media-analysis-skill`, `../media-analysis-skill`, then `media-analysis` on PATH. Run as a subprocess;
    the agent never imports the package.
  - Registered at startup (`src/video_agent/service.py`: `registry.register_package(MEDIA_ANALYSIS_PACKAGE)`), and all
    9 tools appear in `src/video_agent/skills/registry.py` (probe / silence / loudness alongside the ffmpeg-skill
    equivalents; streams / video / audio / timing / integrity / scenes as `*_analysis` skills).
- No other caller found in the sibling checkouts; whether anything outside them calls it is **unverified**.

## Tests and CI
- `python -m pytest -q`: **111 passed** locally on 2026-10-30 (Python 3.11.17, FFmpeg 6.1.1, Linux) from 80 test
  functions — collected per file: `test_contract.py` 43, `test_integration.py` 33, `test_unit.py` 33, `test_evals.py` 2.
- `python evals/run.py` 9/9 measurement evals; `python evals/contract_evals.py` 17/17 contract evals;
  `python -m pyflakes src tests evals` clean.
- `.github/workflows/tests.yml` (push to main, every PR), real FFmpeg on every job:
  Ubuntu Python 3.9 (pinned to `ubuntu-24.04`, which still has 3.9), Ubuntu 3.11, Windows 3.11, macOS 3.11. Each job
  runs pytest, both evals, `doctor --json`, `contract --json` + `contract --check`, and `conformance --json`.
- Other workflows: `codeql.yml` (push / PR to main, weekly), `pr-labeler.yml` (release-drafter autolabeler),
  `release.yml` (see Version). On `main` at `8a9964f` (the last code commit) `tests`, `codeql` and `release` all succeeded.

## KNOWN LIMITATIONS / GAPS
- Reading is unrestricted unless `--allowed-input` is given (CLI default: "any readable regular file"); embedders must
  set roots.
- `timing` (packet timestamps) parses one dict per packet into memory; no streaming for long files. The earlier
  "~100 MB for an hour of A/V" figure was not re-measured and is **unverified**.
- Every Capability is `EXPERIMENTAL`; no STABLE promise yet.
- No MCP transport (ADR-010: not before a consumer needs one).

## NOT DONE / PLANNED
See `ECOSYSTEM_ROADMAP.md`. Step 1 (single-source the version) was implemented by PR #17; step 3 (noise floor / SNR,
A/V offset measurements, only if not qc-skill's) is open.

## NOT IN SCOPE (by design)
AI / inference / decisions, media editing, subtitles, transcription, semantic scene understanding, plugin loading.

## History (merged PRs)
#1 Skill 0.1.0 + agent-ready contract · #2 sponsor link · #3 README landing page · #4 `provides` · #5 OS conformance
hardening · #6 UTF-8 JSON protocol (ADR-026) · #8 STATE.md adapter note · #9 release / labeler / Dependabot / CodeQL ·
#10 README header · #11 Python 3.9 on ubuntu-24.04 · #15 Actions bump + release-drafter v7 · #16 ECOSYSTEM_ROADMAP.md ·
#17 version single-sourced from pyproject.toml · #18 STATE.md refresh. (#7 is an issue, not a PR; Dependabot PRs #12–#14 were closed
unmerged, superseded by #15.)
