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

## ADR-013 One response document for every `--json` invocation
- Context: PR #1 printed a bare result (or `{"results": [...]}`) and a bare `{"error": ...}`; an adapter had to
  branch on shape. Phase 2 PR 2 fixes the machine interface: `run`, `analyze` and `probe` all print exactly one
  `media-analysis/response@1` document with `status` ok / partial / error, `results[]`, `observations[]`, `usage`,
  `budget`; per-request failures are error results and do not abort a batch. Human output is unchanged.
- `run` is the canonical invocation (`media-analysis run - --json`, request on stdin); `analyze` / `probe` are
  conveniences that build the same request document.

## ADR-014 Cache policy is a request field with five result statuses
- `cache_policy` `use` / `bypass` / `only`; result `cache.status` `hit` / `miss` / `invalid` / `bypass` / `disabled`.
  `only` never runs an analyzer (`CACHE_MISS`, exit 13) so an adapter can ask "what do we already know" at zero
  cost. `invalid` is reported (not folded into `miss`) so a tampered or stale entry is visible in provenance.

## ADR-015 Batch budget replaces the CLI budget; unknown budget names are errors
- A batch document may carry `budget`; only `max_analysis_calls`, `timeout`, `max_total_seconds` exist. Any other
  name (`max_bytes_scanned`, GPU, cost, storage) is `INVALID_INPUT` for the whole document rather than silently
  ignored, so an agent cannot believe a budget is enforced when it is not.

## ADR-016 Schemas are published, minimal validator is in-tree
- The request / batch / result / response / observation JSON Schemas live in `schemas.py` and are printed by
  `contract --json`. A ~60-line validator covering the subset used is included instead of a `jsonschema`
  dependency (the package stays standard-library only); tests and contract evals validate real responses with it.

## ADR-017 Consumer rules are a fixture, the contract is generated
- `tests/contract/agent_skill_package_contract.json` records the video-production-agent rules (SkillPackage /
  ToolSpec / Observation / capability names) copied once from its `skills/contract.py`. The media-analysis contract
  under test is always generated from the registry (`tool_spec(analyzer)`), never hand-written, so the two cannot
  drift apart unnoticed; the agent repository is not installed or imported.

## ADR-018 CI covers Linux 3.9 / 3.11, Windows and macOS with a real FFmpeg
- Each platform installs FFmpeg its own way (apt / choco / brew). The first Windows run found two real defects
  (`proc.kill()` leaves grandchildren alive; a 1 ns time budget is unmeasurable on a 16 ms clock): the runner now
  uses `CREATE_NEW_PROCESS_GROUP` + `taskkill /T /F` on Windows, and the budget test uses a measurable duration. Nothing is skipped on any platform; the one
  platform-conditional piece of test code tolerates a refused symlink creation on Windows (privilege), while every
  other path-policy case still runs there.

## ADR-019 A failed analyzer run is reported as usage
- Context: an error result used to show `analyzer_calls = 0` even when ffprobe / ffmpeg had run before the
  failure (e.g. a stream ordinal out of range, a decode failure). The budget was charged, the report was not.
- Decision: `run()` fills the error result's `usage` from the execution log when the analyzer ran, so what the
  agent sees equals what the budget counted. Errors are never cached, so repeating an unsupported request costs a
  call each time and says so.

## ADR-020 Contract drift is detected by mutation fixtures, not by a saved copy
- `contract --check` and `contract_check.check_contract()` compare a document with the live implementation.
  Fixtures in `tests/contract/cases` describe *mutations* of the live contract (delete a tool, change a capability,
  bump a version, add a kind) and the problems each must raise; a full hand-written "valid contract" fixture was
  rejected because it would be a second source of truth that drifts silently. `contract@1` is the only supported
  schema; a `@2` document is refused as a whole rather than half-validated.

## ADR-021 No confidence field
- Observations carry measurements against recorded parameters. A `confidence` value would read as "probability the
  agent should act", which is inference. Scores that are measurements (`cut_score`, `probe_score`) keep their
  measurement names. A static test forbids confidence / recommendation vocabulary in the analyzers.

## ADR-022 `provides` publishes five analysis kinds as cross-repository Capability ids, deliberately not all nine
- `contract.py` adds a top-level `provides` list for `kajisho5/AI-video-production-OS`'s `CapabilityContract.provides`
  (`docs/SPEC.md` there), so a registry can resolve "who provides `measure.audio.loudness`" without hardcoding this
  repository. Five kinds get an id, matching that project's own `docs/CAPABILITY_MATRIX.md` section 8 exactly:
  `silence` -> `measure.audio.silence`, `loudness` -> `measure.audio.loudness`, `integrity` -> `measure.audio.integrity`,
  `scene_detection` -> `measure.video.scene_detection`, `timing` -> `measure.video.timing`.
- Three of those five (`silence`, `loudness`, `integrity`) are the ecosystem's one documented Capability collision:
  `qc-skill` independently implements the same three measurements with no shared code and publishes the identical id
  for each in its own `contract.py` (companion PR), so a registry sees one Capability with two Providers, not two
  unrelated things that happen to share a name.
- `media_probe`, `stream_layout`, `video_format`, `audio_format` and `duration` are deliberately not in `provides`.
  `CAPABILITY_MATRIX.md`'s own section 8c leaves their id unsettled - its note bundles them as "measure.video.probe /
  measure.\*.format / measure.\*.duration" without pinning one id per kind - and that document is explicit that this
  Skill's `video_format` is *not* the same capability as `qc-skill`'s `measure.video.format` ("a related-but-distinct
  capability, not the same id, pending further audit"). Guessing an id here would risk publishing a false collision
  that document has already ruled out, so these five kinds stay unassigned until that matrix decision is made rather
  than forcing five new ids into existence in this change.
- Additive: a new top-level `provides` key, verified against `contract_check.check_contract()` (reports zero
  problems with `provides` present - `check_contract` does not assert on this field, so nothing needed to change
  there) and against `evals/contract_evals.py`'s `C11_contract_registry_consistency`.
