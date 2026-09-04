# Observation, result and response contract

Machine-readable versions of everything below: `media-analysis contract --json` → `schemas.request`, `schemas.batch`,
`schemas.result`, `schemas.response`, `schemas.observation` (JSON Schema documents; `schema_versions` lists their
versions). The same documents are what a future MCP transport would carry (transport-independent).

## Response (stdout of `run` / `analyze` / `probe` with `--json`)

```json
{
  "schema": "media-analysis/response@1",
  "skill": {"id": "media-analysis", "version": "0.1.0"},
  "status": "ok | partial | error",
  "dry_run": false,
  "results": [ "...one result per request, in request order..." ],
  "observations": [ "...observations of the ok results, in request order..." ],
  "usage": {"analyzer_calls": 1, "cache_hits": 0, "seconds": 0.3},
  "budget": {"calls": 1, "seconds": 0.3, "budget": {"max_analysis_calls": null, "timeout": 600.0, "max_total_seconds": null}},
  "warnings": [],
  "error": {"code": "...", "message": "...", "details": {}},   "error_kind": "..."     // only when the document itself was rejected
}
```

`status` is `ok` when every result is ok, `partial` when some are, `error` when none are or the request document
was rejected before any request ran (invalid JSON, unknown batch / budget field). `dry_run: true` results carry a
`plan` instead of an `observation`.

## Result

```json
{
  "analysis_id": "analysis-001", "asset_id": "asset-001", "kind": "silence",
  "status": "ok",
  "observation": { "..." },
  "cache": {"status": "hit | miss | invalid | bypass | disabled", "policy": "use | bypass | only", "key": "<sha256 | null>"},
  "usage": {"analyzer_calls": 0 | 1, "seconds": 0.3, "operations": [{"executable": "ffprobe | ffmpeg", "purpose": "..."}]}
}
```

A failed result: `"status": "error"`, `"error": {"code", "message", "details"}`, `"error_kind": "<code>"`, no
`observation`; `analysis_id` / `asset_id` / `kind` are echoed when the request carried them as strings, else null.
`usage.operations` names what ran (executable + purpose), never argv.

## Observation

```json
{
    "id":          "obs_<identity[:16]>",
    "asset_id":    "<caller's asset label>",
    "kind":        "<AnalysisKind>",
    "data":        { "...measured values only..." },
    "source":      "media-analysis/<tool>@<version>",
    "analysis_id": "<caller's analysis id | analysis-<identity[:16]>>",
    "observed_at": "YYYY-MM-DDTHH:MM:SSZ",
    "analysis":    { "identity": "<sha256>", "analyzer": "media-analysis/<tool>", "analyzer_version": "0.1.0",
                     "parameters": { "...effective parameters..." }, "seconds": 0.3 },
    "asset":       { "path": "/abs/path", "fingerprint": "<sha256 of content>", "size": 12345 }
}
```

## Field rules

| field | rule |
|---|---|
| `id` | derived from the analysis identity; identical for identical analyses |
| `asset_id` | echoed from the request (`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`); a label, not part of the identity |
| `kind` | one of the declared `ANALYSIS_KINDS`; verified against the request |
| `data` | only measured values; numbers are finite; times are within `[0, duration + 1 s]`; no `command` / `argv` / `cmd` / `shell` keys; no secret-looking keys |
| `source` | `media-analysis/<tool>@<semver>`; provenance is always OBSERVED, never INFERRED / AI_GENERATED / USER / DECISION; matches video-production-agent `Observation.source = "<package>/<tool>@<version>"` (PR #7 boundary, ADR-001) and its validator (`"@" in source and not source.startswith("ai")`) |
| `analysis_id` | request value or derived |
| `observed_at` | UTC, second precision; on a cache hit the original measurement time is kept |
| `analysis.parameters` | the effective parameters (defaults applied); same input + tool + version + parameters → same `identity` |
| `asset.fingerprint` | sha256 of the file content; the path is informational |

`output_policy.round` (default 3) rounds floats in the returned document; it does not affect the identity or the
cached Observation.

## Confidence and evidence

There is no `confidence` field in any Observation, by design. Every value is a measurement against explicit
parameters recorded next to it (`analysis.parameters`); the fields that look like scores are measurements too:
`cut_score` (scdet score of the frame), `probe_score` (ffprobe container detection score), `integrity.status`
(decoder printed error lines or not). None of them is a probability that the agent should act, and the Skill never
assigns one. Evidence an agent can verify later: `asset.fingerprint` (sha256 of the content), `asset.path`,
`analysis.analyzer` + `analysis.analyzer_version` (== `source`), `analysis.parameters`, `analysis.identity`,
`observed_at`, and in the result `cache.status` (a `hit` was measured at `observed_at`, not now) and
`usage.operations` (which executables ran, for what).

## Determinism

`id`, `analysis.identity`, `data` and `asset.fingerprint` depend only on the file content, the analyzer, its version,
the kind and the effective parameters. `observed_at`, `analysis.seconds`, `usage.seconds` and `asset.path` are
outside the identity (time, wall clock and location of the copy). Nothing derived from a process id, a temporary
path or the current time enters `data` or the identity (tested across processes).

## Observation vs Inference

An Observation states what a tool measured. It never says "silence is unwanted", "loudness is off target", "this is
a slide camera". Those are Inferences with evidence and belong to the agent; this package has no type for them.
Values that look like judgements are still measurements against explicit parameters: `type: "leading"` is defined
by `edge_tolerance`, `status: "FAIL"` by "the decoder printed error lines", `frame_rate_mode: "variable"` by the
measured intervals. The parameter that defines each of them is recorded next to the value.

## Errors and exit codes

An error is never an Observation. It is a result with `status: "error"` (or a top-level `error` when the document
was rejected):

```json
{"analysis_id": null, "asset_id": "asset-1", "kind": "loudness", "status": "error",
 "error": {"code": "BUDGET_EXCEEDED", "message": "max_analysis_calls=1 reached", "details": {"calls": 1, "seconds": 0.05, "budget": {"...": "..."}}},
 "error_kind": "BUDGET_EXCEEDED", "cache": {"status": "disabled", "policy": "use", "key": null}, "usage": {"analyzer_calls": 0, "seconds": 0.0, "operations": []}}
```

Process exit code: 0 when every result is ok; otherwise the code of the first error, `2 + index` in the table
(also published as `contract --json` → `errors.exit_codes`).

| exit | code | when |
|---|---|---|
| 2 | `INVALID_INPUT` | request / kind / parameter / stream ordinal / cache_policy / budget field invalid, command-style or unknown fields present, request document not JSON |
| 3 | `FILE_NOT_FOUND` | input missing or not a regular file |
| 4 | `PATH_NOT_ALLOWED` | input outside `--allowed-input` roots, or a write outside the workspace |
| 5 | `UNSUPPORTED_FORMAT` | ffprobe cannot open the input, or the needed stream type is absent |
| 6 | `ANALYZER_UNAVAILABLE` | ffmpeg / ffprobe / filter missing |
| 7 | `ANALYZER_TIMEOUT` | analyzer exceeded the effective timeout (process group killed) |
| 8 | `ANALYSIS_FAILED` | ffmpeg / ffprobe ran but failed or produced unparsable output |
| 9 | `INVALID_RESULT` | analyzer returned something that is not an object |
| 10 | `BUDGET_EXCEEDED` | call or total-seconds budget exhausted before running |
| 11 | `CACHE_INVALID` | malformed cache key |
| 12 | `VERIFICATION_FAILED` | Observation failed the checks above |
| 13 | `CACHE_MISS` | `cache_policy: only` and no valid cache entry (no analyzer was run) |
