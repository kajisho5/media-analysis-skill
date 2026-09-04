# Observation contract

```json
{
  "observation": {
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
  },
  "cache": "hit | miss | disabled",
  "cache_key": "<identity>",
  "budget": { "calls": 1, "seconds": 0.3, "budget": { "max_analysis_calls": null, "timeout": 600.0, "max_total_seconds": null } }
}
```

## Field rules

| field | rule |
|---|---|
| `id` | derived from the analysis identity; identical for identical analyses |
| `asset_id` | echoed from the request (`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`); a label, not part of the identity |
| `kind` | one of the declared `ANALYSIS_KINDS`; verified against the request |
| `data` | only measured values; numbers are finite; times are within `[0, duration + 1 s]`; no `command` / `argv` / `cmd` / `shell` keys; no secret-looking keys |
| `source` | `media-analysis/<tool>@<semver>`; never an AI source; matches video-production-agent `Observation.source = "<tool>@<version>"` (PR #7 boundary) and its validator (`"@" in source and not source.startswith("ai")`) |
| `analysis_id` | request value or derived |
| `observed_at` | UTC, second precision; on a cache hit the original measurement time is kept |
| `analysis.parameters` | the effective parameters (defaults applied); same input + tool + version + parameters → same `identity` |
| `asset.fingerprint` | sha256 of the file content; the path is informational |

`output_policy.round` (default 3) rounds floats in the returned document; it does not affect the identity or the
cached Observation.

## Observation vs Inference

An Observation states what a tool measured. It never says "silence is unwanted", "loudness is off target", "this is
a slide camera". Those are Inferences with evidence and belong to the agent; this package has no type for them.
Values that look like judgements are still measurements against explicit parameters: `type: "leading"` is defined
by `edge_tolerance`, `status: "FAIL"` by "the decoder printed error lines", `frame_rate_mode: "variable"` by the
measured intervals. The parameter that defines each of them is recorded next to the value.

## Errors

An error is never an Observation:

```json
{"error": {"code": "BUDGET_EXCEEDED", "message": "max_analysis_calls=1 reached", "details": {"calls": 1, "seconds": 0.05, "budget": {"...": "..."}}}}
```

| code | when |
|---|---|
| `INVALID_INPUT` | request / kind / parameter / stream ordinal invalid, command-style fields present |
| `FILE_NOT_FOUND` | input missing or not a regular file |
| `PATH_NOT_ALLOWED` | input outside `--allowed-input` roots, or a write outside the workspace |
| `UNSUPPORTED_FORMAT` | ffprobe cannot open the input, or the needed stream type is absent |
| `ANALYZER_UNAVAILABLE` | ffmpeg / ffprobe / filter missing |
| `ANALYZER_TIMEOUT` | analyzer exceeded the effective timeout (process group killed) |
| `ANALYSIS_FAILED` | ffmpeg / ffprobe ran but failed or produced unparsable output |
| `INVALID_RESULT` | analyzer returned something that is not an object |
| `BUDGET_EXCEEDED` | call or total-seconds budget exhausted before running |
| `CACHE_INVALID` | malformed cache key |
| `VERIFICATION_FAILED` | Observation failed the checks above |
