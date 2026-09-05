<p align="center">
  <img src="assets/hero.jpg" alt="Media Analysis Skill — Understand your media" width="100%">
</p>

# media-analysis-skill

**Measure your media before anyone decides anything about it.** Local `ffprobe` / `ffmpeg`, Python standard
library, structured Observations out. No AI inside.

<p>
  <a href="https://github.com/kajisho5/media-analysis-skill/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/kajisho5/media-analysis-skill/actions/workflows/tests.yml/badge.svg"></a>
  <img alt="python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-blue">
  <img alt="dependencies: none" src="https://img.shields.io/badge/dependencies-none-success">
  <img alt="platforms" src="https://img.shields.io/badge/platforms-linux%20%7C%20macos%20%7C%20windows-informational">
  <a href="LICENSE"><img alt="license MIT" src="https://img.shields.io/badge/license-MIT-lightgrey"></a>
  <a href="https://github.com/sponsors/kajisho5"><img alt="sponsor" src="https://img.shields.io/badge/sponsor-%E2%9D%A4-ff69b4"></a>
</p>

`media-analysis-skill` is the **eyes and meters** of a video-production toolchain. It measures facts about a media
file — container, streams, video / audio format, silence, loudness, integrity, scene cuts, timing — and returns
them as deterministic, verifiable **Observation JSON** that an agent (or a human) can reason about. Its sibling
[ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) is the hands that edit; the
[video-production-agent](https://github.com/kajisho5/video-production-agent) is the brain that decides.

**media-analysis-skill is NOT an AI agent.** It contains no AI provider, no LLM, no prompt, no reasoning, no
inference, no decision, no policy, no approval and no production planning. It never edits, converts or renders
media. It measures, and it says what it could not measure.

## Quick start

Requirements: Python 3.9+ (standard library only) and FFmpeg (`ffmpeg` + `ffprobe`) on `PATH`.

```bash
git clone https://github.com/kajisho5/media-analysis-skill
cd media-analysis-skill
pip install -e .
media-analysis doctor            # ffmpeg / ffprobe / filters, analyzer availability, contract, cache, path policy
media-analysis probe input.mp4   # container + first video / audio stream, human-readable
```

Then measure what you need:

```text
media-analysis analyze input.mp4 --kind media_probe
media-analysis analyze input.mp4 --kind silence --param threshold_db=-45 --json
media-analysis analyze input.mp4 --kind loudness --kind integrity --json --cache-dir .media-analysis-cache
media-analysis analyze input.mp4 --kind integrity --dry-run
media-analysis run request.json --json          # canonical machine interface: AnalysisRequest in, response document out
cat request.json | media-analysis run - --json  # same, over stdin
media-analysis doctor --json                    # environment vs. contract: python, ffmpeg, ffprobe, filters, registry, cache, path policy
media-analysis contract --json                  # machine-readable Skill / Tool contract with request / response / observation schemas
media-analysis contract --check saved.json      # does a saved contract still describe this installation? (drift detection, exit 1 on drift)
```

Everything runs locally; nothing is uploaded. Ten analysis kinds, one response schema, exit codes an adapter can
branch on. Details below and in [docs/](docs/).

## Why a measurement Skill

| | |
|---|---|
| **Deterministic** | same file content + analyzer + version + parameters → same identity, same Observation id, same data |
| **Observations, not opinions** | `silence 0.0–3.0 s exists` is the whole statement; `trim it` is the agent's job. No confidence-of-action, no recommendations |
| **Verifiable** | every Observation records the analyzer, its version, the effective parameters and the file's sha256; every response is checked against the published schemas before it leaves |
| **Honest** | what could not be measured is `null` / `not_performed`, never PASS; a failed analyzer run still reports its cost |
| **Safe to call from an agent** | structured JSON in and out, no shell, no command or argv fields, no executable override, workspace-confined writes, process-group timeouts |
| **Cheap to repeat** | content-addressed cache: a second question about the same file starts zero processes |

## Ecosystem and responsibilities

| | [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) | **media-analysis-skill** | [video-production-agent](https://github.com/kajisho5/video-production-agent) |
|---|---|---|---|
| Role | Deterministic media **processing** (hands) | Deterministic media **observation / analysis** (eyes / meters) | AI **orchestration** (brain) |
| Does | cut, render, overlay, captions, audio processing, loudness normalisation, export, compliance check | probe, stream layout, video / audio format, silence, loudness measurement, scene cuts, integrity, timing | interprets Observations, derives Inferences and Decisions, chooses Skills / Tools, asks for approval, plans and executes production |
| Never | decides what to produce | interprets, decides, edits, writes media | runs ffmpeg directly |
| Output | media artifacts (+ `--json` result) | Observation JSON (`source: media-analysis/<tool>@<version>`) | Project IR, plans, provenance |

```text
media file ──▶ media-analysis-skill ──▶ Observations (measured facts, provenance OBSERVED)
                                             │
                                             ▼
                                   video-production-agent ──▶ Inference → Decision → Plan
                                             │
                                             ▼
                                   production Skills (ffmpeg-skill …) ──▶ Execution → QA
```

Overlap that is intentional: ffmpeg-skill has `probe`, `silence --list`, `loudness --measure-only` and `scenes` for its
own workflow. This package is the dedicated observation domain with a stable Observation contract, analysis identity,
cache, budget, verification and a security boundary; it does not depend on ffmpeg-skill and does not modify it. Which
of the two a production system uses for a measurement is the agent's (registry's) choice, not this package's.

## What it returns

Every `--json` invocation prints exactly one response document (schema `media-analysis/response@1`), on success and
on failure alike; stderr carries diagnostics only.

```json
{
  "schema": "media-analysis/response@1",
  "skill": {"id": "media-analysis", "version": "0.1.0"},
  "status": "ok",
  "dry_run": false,
  "results": [{
    "analysis_id": "analysis-001", "asset_id": "asset-001", "kind": "media_probe", "status": "ok",
    "observation": {
      "id": "obs_5d07f00595dfc668",
      "asset_id": "asset-001",
      "kind": "media_probe",
      "data": { "container": {"format": "mov,mp4,m4a,3gp,3g2,mj2", "duration": 6.0, "size": 208685, "bitrate": 278246, "start_time": 0.0},
                "video": {"codec": "h264", "width": 320, "height": 180, "fps": 25.0, "pixel_format": "yuv420p"},
                "audio": {"codec": "aac", "sample_rate": 48000, "channels": 1, "channel_layout": "mono", "sample_format": "fltp"} },
      "source": "media-analysis/probe@0.1.0",
      "analysis_id": "analysis-001",
      "observed_at": "2026-09-04T10:00:00Z",
      "analysis": {"identity": "<sha256>", "analyzer": "media-analysis/probe", "analyzer_version": "0.1.0", "parameters": {}, "seconds": 0.05},
      "asset": {"path": "/abs/sample.mp4", "fingerprint": "<sha256 of file>", "size": 208685}
    },
    "cache": {"status": "miss", "policy": "use", "key": "<sha256>"},
    "usage": {"analyzer_calls": 1, "seconds": 0.05, "operations": [{"executable": "ffprobe", "purpose": "format and stream metadata"}]}
  }],
  "observations": [ "...the observations of the ok results, in request order..." ],
  "usage": {"analyzer_calls": 1, "cache_hits": 0, "seconds": 0.05},
  "budget": {"calls": 1, "seconds": 0.05, "budget": {"max_analysis_calls": null, "timeout": 600.0, "max_total_seconds": null}},
  "warnings": []
}
```

A failed result carries `"status": "error"`, `error: {code, message, details}` and `error_kind` instead of an
observation; `status` of the response is `ok`, `partial` or `error`. Full reference: [docs/observation.md](docs/observation.md).

## Analysis kinds

| kind | tool | needs | measures |
|---|---|---|---|
| `media_probe` | `media-analysis/probe` | ffprobe | container + first video / audio stream summary |
| `stream_layout` | `media-analysis/streams` | ffprobe | every stream: index, type, ordinal, codec, language, disposition, dimensions, rate, channels |
| `video_format` | `media-analysis/video` | ffprobe | resolution, fps, frame count, pixel format, colour, SAR / DAR, CFR / VFR (measured from packet timestamps) |
| `audio_format` | `media-analysis/audio` | ffprobe | sample rate, channels, layout, codec, sample format, bitrate, duration |
| `duration` | `media-analysis/timing` | ffprobe | container / stream durations and start times |
| `silence` | `media-analysis/silence` | ffmpeg `silencedetect` | segments with leading / internal / trailing classification, parameters recorded |
| `loudness` | `media-analysis/loudness` | ffmpeg `ebur128` | integrated LUFS, loudness range, true peak (EBU R128) |
| `integrity` | `media-analysis/integrity` | ffmpeg + ffprobe | full decode error count, decoded vs declared frames, timestamp monotonicity → PASS / WARN / FAIL |
| `scene_detection` | `media-analysis/scenes` | ffmpeg `scdet` | visual cuts with score; intervals between them. Not semantic scenes |
| `timing` | `media-analysis/timing` | ffprobe | per-stream packet timestamps, gaps, A/V duration mismatch |

## How video-production-agent consumes media-analysis-skill

```text
video-production-agent                       media-analysis-skill
  SkillRegistry / ToolRouter                    contract --json  (SkillPackage / ToolSpec fields, schemas)
  adapter builds AnalysisRequest JSON  ──────▶  media-analysis run - --json      (stdin: request, batch or list)
  adapter parses the response document ◀──────  stdout: one response document; stderr: diagnostics
  Observation layer takes `observations[]`      source = media-analysis/<tool>@<version>, provenance OBSERVED
  Inference / Decision / Plan / IR / Execution  (agent only; nothing of that exists here)
```

- The adapter never parses human-readable text and never builds ffmpeg commands: it sends a request document and
  reads the response document. Exit code 0 means every result is ok; any other code is the first error's code.
- What the agent must not send: arbitrary commands, argv, shell strings, executable paths, credentials, AI reasoning,
  production plans, Project IR. The request schema rejects unknown fields; the executables come from `PATH` only.
- What the agent gets back: measurements with the parameters that produced them. Deciding what they mean
  (unwanted silence, off-target loudness, which cut to make) is the agent's Inference / Decision layer.

Step-by-step adapter recipe: [docs/architecture.md](docs/architecture.md#connecting-to-video-production-agent).

## Guarantees

- **Deterministic identity**: same file content + analyzer + analyzer version + kind + effective parameters → same
  `analysis.identity`, same cache key, same observation id. Parameter order never matters (canonical JSON).
- **Cache** (`--cache-dir`, inside the workspace; `cache_policy` `use` / `bypass` / `only`): a hit returns the
  stored Observation without running any analyzer and counts no analyzer call; changing the file, the analyzer
  version or the parameters is a miss; tampered or unreadable entries are reported as `invalid` and re-measured.
- **Budget**: `max_analysis_calls`, `max_total_seconds`, `timeout` are enforced (CLI flags or a batch `budget`);
  unknown budget names are rejected; exceeding a limit yields `BUDGET_EXCEEDED` / `ANALYZER_TIMEOUT` and no Observation.
- **Timeout**: the whole ffmpeg / ffprobe process group is killed; no partial observation and no cache entry is left.
- **Verification**: every Observation is checked (schema, asset / kind / analysis identity, source format, finite
  numbers, timestamp range, no command / argv keys, no secret-looking keys or secret env values) before it is returned.
- **Security**: no shell, no user-supplied commands, argv or executable paths; ffprobe / ffmpeg argv built only from
  structured data; `-protocol_whitelist file`; inputs resolved and optionally confined to `--allowed-input` roots;
  writes confined to the workspace; child processes get a minimal environment. See [docs/security.md](docs/security.md).
- **Honesty**: `doctor` reports only what was detected; analyzers whose capability is missing are `unavailable`;
  measurements that could not be made are `null` / `not_performed`, never PASS. A failed analyzer run still reports
  its cost (`usage.analyzer_calls = 1`) in the error result; errors are never cached.
- **No inference**: an Observation says what exists (`silence 0.0–3.0 s`, `integrity FAIL: 19 decoder error lines`,
  `cut at 2.0 s, score 29.4`). It never says what to do about it. There is no `confidence` field anywhere: values
  such as `cut_score` are measurements against the stated parameters, not a probability that the agent should act.

## Structured errors

| exit | code | exit | code |
|---|---|---|---|
| 0 | success (every result ok) | 8 | `ANALYSIS_FAILED` |
| 2 | `INVALID_INPUT` | 9 | `INVALID_RESULT` |
| 3 | `FILE_NOT_FOUND` | 10 | `BUDGET_EXCEEDED` |
| 4 | `PATH_NOT_ALLOWED` | 11 | `CACHE_INVALID` |
| 5 | `UNSUPPORTED_FORMAT` | 12 | `VERIFICATION_FAILED` |
| 6 | `ANALYZER_UNAVAILABLE` | 13 | `CACHE_MISS` (cache_policy `only`) |
| 7 | `ANALYZER_TIMEOUT` | | |

With `--json` every error is inside the response document (per result, or `error` / `error_kind` at the top when the
document itself was rejected); without `--json`, one line on stderr. The table is also in `contract --json` → `errors`.

## Versioning

- Package / Skill version: `0.1.0` (`media_analysis.VERSION`). Every tool and every analyzer carries the package
  version; `source` is `media-analysis/<tool>@<version>`.
- Tool schema (request fields, parameter names, `data` keys of a kind) is stable within a minor version. Adding a key
  is a patch; renaming / removing a key, changing a default or changing how a value is measured bumps the minor
  version and therefore the analyzer version, which invalidates cached Observations by design.
- Contract schemas (`contract --json` → `schema_versions`): `contract`, `request`, `response`, `observation` are
  versioned separately and bumped only when the corresponding document shape changes incompatibly. Within
  `media-analysis/contract@1` changes are additive only (new optional fields, new kinds); an adapter that saved the
  contract can verify it against the installed skill with `contract --check` (`status: ok | drift`). A future
  `contract@2` would be a different `schema` value and is refused by `--check` of a `@1` implementation. Stable
  identifiers for the video-production-agent adapter: `skill_id`, `version`, `tools[].tool_id`, `tools[].version`,
  `tools[].required_capabilities`, `tools[].kinds`, `kind_to_tool`, `errors.exit_codes`.
- `contract --json` → `provides`: all ten of this Skill's analysis kinds by their cross-repository Capability id
  (`silence` -> `measure.audio.silence`, `loudness` -> `measure.audio.loudness`, `integrity` -> `measure.audio.integrity`,
  `scene_detection` -> `measure.video.scene_detection`, `timing` -> `measure.video.timing`, `media_probe` ->
  `measure.media.probe`, `stream_layout` -> `measure.media.stream_layout`, `video_format` -> `measure.video.probe`,
  `audio_format` -> `measure.audio.probe`, `duration` -> `measure.media.duration`), matching the ids assigned to this
  Skill in [`kajisho5/AI-video-production-OS`](https://github.com/kajisho5/AI-video-production-OS)'s
  `docs/CAPABILITY_MATRIX.md` (see `docs/decisions.md` ADR-022 for why `video_format` gets its own
  `measure.video.probe` id rather than `qc-skill`'s `measure.video.format`: a raw probe and a threshold judgment are
  different capabilities, confirmed by reading both implementations). `silence`/`loudness`/`integrity` are also
  independently implemented by `qc-skill`, which publishes the identical ids: this is that project's one documented
  Capability collision, and the two Skills agreeing on the id with no shared code is what lets a future registry see
  it as one Capability with two Providers instead of two unrelated things that happen to share a name.

## Development

```text
pip install -e . pytest
python -m pytest -q             # unit + contract + integration (real ffmpeg) + evals; nothing is skipped
python evals/run.py             # measurement evals: expected values with tolerances, derived from the fixture construction
python evals/contract_evals.py  # contract evals: contract vs. implementation, schemas, rejection of unsafe input, cache identity
```

CI runs the same on Linux (Python 3.9 and 3.11), Windows and macOS with a real FFmpeg.

Docs: [architecture](docs/architecture.md) · [tools](docs/tools.md) · [observation](docs/observation.md) ·
[security](docs/security.md) · [testing](docs/testing.md) · [decisions](docs/decisions.md) · [SKILL.md](SKILL.md)

## Support

If this skill saves you time, you can help keep it maintained through [GitHub Sponsors](https://github.com/sponsors/kajisho5).
Issues and pull requests are just as welcome.

License: MIT
