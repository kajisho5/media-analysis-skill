# Architecture Decision Records

## ADR-001 Observation `source` is `media-analysis/<tool>@<version>`
- Context: the task brief suggested `media-analysis@0.1.0`; video-production-agent PR #7 defines `Observation.source`
  as `<tool>@<version>` (e.g. `ffmpeg-skill/probe@0.8.4`), its validator requires `@` and rejects `ai*`, and its
  provenance code derives the package from the prefix before `/`.
- Decision: use the tool id + version. It is more precise (which analyzer produced the value) and compatible with the
  agent as is. `AI_GENERATED` never appears; the verifier rejects any observation claiming an AI source.

## ADR-002 One package version for all tools and analyzers
- Every analyzer's `version` equals the package version. A change in how any value is measured bumps the package
  minor version, which changes every analysis identity and invalidates the cache. Simpler than per-tool versions and
  matches how the agent tracks `tool_versions` per package.

## ADR-003 Static registry, no plugins, no stubs
- Analyzers are a literal list. The registry refuses to start if it disagrees with the contract (`KIND_TO_TOOL`,
  `TOOL_CAPABILITIES`), so a kind cannot be declared without an analyzer, and an analyzer cannot exist undeclared.
  No dynamic import, no entry points, no future-tool placeholders.

## ADR-004 `asset_id` is a label; identity comes from file content
- The cache key and analysis identity use the sha256 of the file content, the analyzer, its version, the kind and the
  effective parameters. `asset_id` (and a caller-supplied `analysis_id`) are echoed but do not enter the identity, so
  two callers labelling the same file differently share one measurement. Hashing the content costs one read of the
  file, which every decode-based analysis does anyway; size + mtime fingerprints were rejected as not deterministic
  across copies.

## ADR-005 Measurement filters, not processing filters
- Loudness uses `ebur128` (a meter) rather than `loudnorm` (a normaliser with a measurement mode), silence uses
  `silencedetect`, scene detection uses `scdet`, integrity decodes to `-f null`. The package never writes media and
  never re-encodes, keeping the boundary with ffmpeg-skill mechanical rather than conventional.

## ADR-006 Certainty flags instead of guesses
- `frame_rate_mode` is decided from measured packet intervals (constant / variable / unknown with a basis string),
  not from `r_frame_rate != avg_frame_rate`. `channel_layout` is null when the container does not declare it.
  Loudness of silent input reports nulls in `unmeasurable` and `integrated_below_absolute_gate`. Integrity checks
  that could not run are `not_performed`; PASS requires the checks to have run clean.

## ADR-007 Budget covers only what is enforced
- `max_analysis_calls`, `timeout`, `max_total_seconds` exist because the engine enforces them (counter, process
  timeout with group kill, cumulative wall time). `max_storage`, `max_gpu_time`, `max_api_cost` from the agent's
  specification are not represented here because this package cannot measure or enforce them.

## ADR-008 Cache is Observation reuse only
- The cache stores verified Observations keyed by analysis identity, with metadata to re-validate an entry. It has no
  notion of jobs, plans, resume or dependencies (the agent's Job / IR resume, ADR-014 there, is not reimplemented).

## ADR-009 Dry-run shows operations, not argv
- `--dry-run` lists `{executable, purpose}` per operation. Printing argv would create a command-shaped artefact that
  invites command passthrough; the Observation verifier forbids `command` / `argv` keys for the same reason.
  ffmpeg-skill prints its commands in dry-run because its users copy them; this package's users are agents.

## ADR-010 No MCP server in 0.1.0
- The CLI (`run` with an AnalysisRequest on stdin, `--json`) is the contract and is enough for a process-boundary
  adapter (the agent already uses that pattern for ffmpeg-skill, its ADR-001). An MCP transport can be added as a
  thin wrapper over `run` later; adding it now would widen the surface without a consumer.

## ADR-011 Leading negative pts is a fact, not an anomaly
- AAC in MP4 carries a priming packet with a negative pts (edit list). `timing` reports `negative_pts` per stream but
  only lists non-monotonic DTS, gaps and duplicate pts as anomalies, so a normal file has `anomaly_count = 0`.

## ADR-012 Timestamp verification tolerance
- Values in `data` named like timestamps must lie in `[0, duration + 1 s]`. `silencedetect` can report a trailing
  silence end slightly beyond the declared duration (codec padding) and that value is kept as measured; the 1 s
  tolerance is for verification, not a correction.
