---
name: media-analysis
description: Measure deterministic facts about a media file (container, streams, video/audio format, silence, loudness, integrity, scene cuts, timing) with local ffprobe/ffmpeg and return a structured Observation. Use when a task needs to *know* something about a video or audio file before deciding anything: duration, resolution, fps, CFR/VFR, codecs, channel layout, where the silence is, how loud it is (LUFS / true peak), whether it decodes cleanly, where hard cuts are. Do not use it to edit, convert, normalise or export media (that is ffmpeg-skill) or to interpret the numbers (that is the agent). Python 3.9 standard library, no cloud, no API keys, no AI.
---

# media-analysis (Skill)

`media-analysis-skill` is an observation Skill, not an agent. It measures; it never interprets, decides, plans or edits.

## Commands

```text
media-analysis probe <input> [--json]
media-analysis analyze <input> --kind <kind> [--kind <kind> ...] [--param key=value ...] [--json] [--dry-run]
                       [--asset-id ID] [--analysis-id ID] [--timeout S] [--max-analysis-calls N] [--max-total-seconds S]
                       [--cache-dir DIR] [--workspace DIR] [--allowed-input ROOT ...] [--round N]
media-analysis run <request.json | -> [--json] [--dry-run] [engine options]      # canonical machine interface
media-analysis doctor [--json] [--cache-dir DIR] [--workspace DIR]
media-analysis contract [--json] [--check FILE|-]
```

Engine options: `--timeout S`, `--max-analysis-calls N`, `--max-total-seconds S`, `--cache-dir DIR`,
`--cache-policy use|bypass|only`, `--workspace DIR`, `--allowed-input ROOT`.

- With `--json`, stdout is exactly one response document (`media-analysis/response@1`) on success and on failure;
  stderr is diagnostics only. Without `--json`, stdout is human-readable text and errors go to stderr.
- Exit code 0 = every result ok; otherwise the first error's exit code (2..13, table in `contract --json` → `errors`).
- `--dry-run` validates the request and input path and prints the analyzer, required capabilities and the
  ffprobe / ffmpeg operations it would run; nothing is executed.

## Analysis kinds and parameters

| kind | parameters (defaults) |
|---|---|
| `media_probe` | – |
| `stream_layout` | – |
| `video_format` | `stream` (0) video stream ordinal |
| `audio_format` | `stream` (0) audio stream ordinal |
| `duration` | – |
| `silence` | `stream` (0), `threshold_db` (-40), `min_duration` (0.5 s), `edge_tolerance` (0.1 s) |
| `loudness` | `stream` (0) |
| `integrity` | `max_error_lines` (200) |
| `scene_detection` | `stream` (0), `threshold` (10, scdet score 0-100), `min_scene_duration` (0.5 s) |
| `timing` | `gap_factor` (2.5 × median interval), `av_mismatch_tolerance` (0.1 s) |

Unknown kinds, unknown parameters, out-of-range values and any `command` / `argv` field are rejected with `INVALID_INPUT`.

## AnalysisRequest (structured input)

```json
{"analysis_id": "analysis-001", "asset_id": "asset-001", "input": "sample.mp4", "kind": "silence",
 "parameters": {"threshold_db": -45, "min_duration": 1.0}, "timeout": 120, "output_policy": {"round": 3}}
```

`media-analysis run` accepts one request, a list of requests, or a batch `{"requests": [...], "budget": {"max_analysis_calls": 4}}`
(budget names other than `max_analysis_calls`, `timeout`, `max_total_seconds` are rejected). `analysis_id` is
optional: without it the id is derived from the analysis identity (`analysis-<16 hex>`), so the same file + kind +
parameters always yields the same id. `cache_policy`: `use` (read + write, default), `bypass` (never read or write),
`only` (read; `CACHE_MISS` when absent, no analyzer runs).

## Response (structured output)

```json
{"schema": "media-analysis/response@1", "skill": {"id": "media-analysis", "version": "0.1.0"}, "status": "ok", "dry_run": false,
 "results": [{"analysis_id": "analysis-001", "asset_id": "asset-001", "kind": "silence", "status": "ok",
              "observation": {"id": "obs_…", "asset_id": "asset-001", "kind": "silence",
                              "data": {"segments": [{"start": 0.0, "end": 2.0, "duration": 2.0, "type": "leading", "runs_to_end": false}], "...": "..."},
                              "source": "media-analysis/silence@0.1.0", "analysis_id": "analysis-001", "observed_at": "2026-09-04T10:00:00Z",
                              "analysis": {"identity": "…", "analyzer": "media-analysis/silence", "analyzer_version": "0.1.0",
                                           "parameters": {"stream": 0, "threshold_db": -45.0, "min_duration": 1.0, "edge_tolerance": 0.1}, "seconds": 0.3},
                              "asset": {"path": "/abs/sample.mp4", "fingerprint": "<sha256>", "size": 208685}},
              "cache": {"status": "miss", "policy": "use", "key": "…"},
              "usage": {"analyzer_calls": 1, "seconds": 0.3, "operations": [{"executable": "ffprobe", "purpose": "…"}, {"executable": "ffmpeg", "purpose": "…"}]}}],
 "observations": ["…the ok observations in request order…"],
 "usage": {"analyzer_calls": 1, "cache_hits": 0, "seconds": 0.3},
 "budget": {"calls": 1, "seconds": 0.3, "budget": {"max_analysis_calls": null, "timeout": 600.0, "max_total_seconds": null}},
 "warnings": []}
```

A failed result has `"status": "error"`, `"error": {"code", "message", "details"}` and `"error_kind"`; the response
`status` is `ok` / `partial` / `error`. Rules an agent can rely on: `source` is always `media-analysis/<tool>@<version>`
(provenance OBSERVED, never AI); `data` contains only measured values; anything not measurable is `null` or
`"not_performed"`; the parameters that produced the measurement are recorded; changing them changes the identity.

## Workflow hints for an agent

1. `doctor --json` once per environment; only use kinds whose analyzer is `available`.
2. `media_probe` first (cheap, ffprobe only), then targeted kinds. `integrity`, `loudness`, `silence` and
   `scene_detection` decode the whole file.
3. Pass `--cache-dir` (inside your workspace) so repeated questions about the same file are free.
4. Set `--max-analysis-calls` / `--max-total-seconds` (or a batch `budget`) when you have a budget; `BUDGET_EXCEEDED`
   means that analyzer did not run and no observation exists for it.
5. Treat `status: WARN` / `FAIL` (integrity), `frame_rate_mode: "variable"`, `entirely_silent: true`,
   `integrated_below_absolute_gate: true` as facts to reason about, not as decisions made for you. The Skill never
   emits a recommendation, a confidence-of-action or an inference; "silence 0–3 s exists" is its whole statement,
   "trim it" is yours.
6. Budgets are analyzer wall-clock limits (`max_total_seconds` is seconds of analyzer execution, not media duration).
7. Save `contract --json` once and re-validate it with `contract --check` after upgrades; `status: drift` means
   your cached tool table is stale.

## Boundaries

Not here: cutting, normalising, exporting, captions, transcription, thumbnails, speaker detection, semantic scene or
slide understanding, any AI provider. Not here either: job / resume state, approval, policy, production plans,
Project IR, tool selection. Never send this Skill a command, argv, shell string, executable path or credential:
the request schema rejects them and nothing in the package can execute them.
