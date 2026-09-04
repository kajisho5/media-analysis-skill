# media-analysis-skill

Deterministic media **observation / analysis** Skill: measure facts about media files (container, streams,
video / audio format, silence, loudness, integrity, scene cuts, timing) and return them as structured
**Observations**.

**media-analysis-skill is NOT an AI agent.** It contains no AI provider, no LLM, no prompt, no reasoning, no
inference, no decision, no policy, no approval and no production planning. It never edits, converts or renders
media. It measures, and it says what it could not measure.

```text
media-analysis probe input.mp4
media-analysis analyze input.mp4 --kind media_probe
media-analysis analyze input.mp4 --kind silence --param threshold_db=-45 --json
media-analysis analyze input.mp4 --kind loudness --kind integrity --json --cache-dir .media-analysis-cache
media-analysis analyze input.mp4 --kind integrity --dry-run
media-analysis run request.json --json          # structured AnalysisRequest in, Observation out
media-analysis doctor                           # ffmpeg / ffprobe / filters and analyzer availability
media-analysis contract --json                  # Skill / Tool contract
```

Requirements: Python 3.9+, standard library only; FFmpeg (`ffmpeg` + `ffprobe`) on PATH. Install: `pip install -e .`

## Ecosystem and responsibilities

| | [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) | **media-analysis-skill** | [video-production-agent](https://github.com/kajisho5/video-production-agent) |
|---|---|---|---|
| Role | Deterministic media **processing** (hands) | Deterministic media **observation / analysis** (eyes / meters) | AI **orchestration** (brain) |
| Does | cut, render, overlay, captions, audio processing, loudness normalisation, export, compliance check | probe, stream layout, video / audio format, silence, loudness measurement, scene cuts, integrity, timing | interprets Observations, derives Inferences and Decisions, chooses Skills / Tools, asks for approval, plans and executes production |
| Never | decides what to produce | interprets, decides, edits, writes media | runs ffmpeg directly |
| Output | media artifacts (+ `--json` result) | Observation JSON (`source: media-analysis/<tool>@<version>`) | Project IR, plans, provenance |

Overlap that is intentional: ffmpeg-skill has `probe`, `silence --list`, `loudness --measure-only` and `scenes` for its
own workflow. This package is the dedicated observation domain with a stable Observation contract, analysis identity,
cache, budget, verification and a security boundary; it does not depend on ffmpeg-skill and does not modify it. Which
of the two a production system uses for a measurement is the agent's (registry's) choice, not this package's.

## What it returns

```json
{
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
  "cache": "miss",
  "cache_key": "<sha256>",
  "budget": {"calls": 1, "seconds": 0.05, "budget": {"max_analysis_calls": null, "timeout": 600.0, "max_total_seconds": null}}
}
```

Full field reference: [docs/observation.md](docs/observation.md). Tools and analysis kinds: [docs/tools.md](docs/tools.md).

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

## Guarantees

- **Deterministic identity**: same file content + analyzer + analyzer version + kind + effective parameters → same
  `analysis.identity`, same cache key, same observation id. Parameter order never matters (canonical JSON).
- **Cache** (`--cache-dir`, inside the workspace): a hit returns the stored Observation without running any analyzer;
  changing the file, the analyzer version or the parameters is a miss; tampered or unreadable entries are discarded.
- **Budget**: `--max-analysis-calls`, `--max-total-seconds`, `--timeout` are enforced; exceeding them yields
  `BUDGET_EXCEEDED` / `ANALYZER_TIMEOUT` and no Observation.
- **Verification**: every Observation is checked (schema, asset / kind / analysis identity, source format, finite
  numbers, timestamp range, no command / argv keys, no secret-looking keys or secret env values) before it is returned.
- **Security**: no shell, no user-supplied commands or argv, ffprobe / ffmpeg argv built only from structured data,
  `-protocol_whitelist file`, inputs resolved and optionally confined to `--allowed-input` roots, writes confined
  to the workspace, child processes get a minimal environment. See [docs/security.md](docs/security.md).
- **Honesty**: `doctor` reports only what was detected; analyzers whose capability is missing are `unavailable`;
  measurements that could not be made are `null` / `not_performed`, never PASS.

## Structured errors

`INVALID_INPUT`, `FILE_NOT_FOUND`, `PATH_NOT_ALLOWED`, `UNSUPPORTED_FORMAT`, `ANALYZER_UNAVAILABLE`, `ANALYZER_TIMEOUT`,
`ANALYSIS_FAILED`, `INVALID_RESULT`, `BUDGET_EXCEEDED`, `CACHE_INVALID`, `VERIFICATION_FAILED`. With `--json` an error
is `{"error": {"code", "message", "details"}}` on stdout; without it, one line on stderr. Exit code = 2 + index in
that list.

## Versioning

- Package / Skill version: `0.1.0` (`media_analysis.VERSION`). Every tool and every analyzer carries the package
  version; `source` is `media-analysis/<tool>@<version>`.
- Tool schema (request fields, parameter names, `data` keys of a kind) is stable within a minor version. Adding a key
  is a patch; renaming / removing a key, changing a default or changing how a value is measured bumps the minor
  version and therefore the analyzer version, which invalidates cached Observations by design.
- Skill contract (`media-analysis contract`): `schema_version` is bumped only when the envelope
  (`observation` / `analysis` / `asset` fields) changes. Stable identifiers for a future video-production-agent
  adapter: `skill_id`, `version`, `tools[].tool_id`, `tools[].version`, `tools[].required_capabilities`,
  `tools[].kinds`.

## Development

```text
pip install -e . pytest
python -m pytest -q        # unit + integration (real ffmpeg) + evals; nothing is skipped
python evals/run.py        # expected values with tolerances, derived from the fixture construction
```

Docs: [architecture](docs/architecture.md) · [tools](docs/tools.md) · [observation](docs/observation.md) ·
[security](docs/security.md) · [testing](docs/testing.md) · [decisions](docs/decisions.md) · [SKILL.md](SKILL.md)

License: MIT
