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
- Amended by ADR-028: the tool `version` is still the package version, but it no longer enters the analysis identity
  or the cache key; a per-analyzer `algorithm_version` does.

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

## ADR-022 `provides` publishes all ten analysis kinds as cross-repository Capability ids
- `contract.py` adds a top-level `provides` list for `kajisho5/AI-video-production-OS`'s `CapabilityContract.provides`
  (`docs/SPEC.md` there), so a registry can resolve "who provides `measure.audio.loudness`" without hardcoding this
  repository. All ten kinds get an id, matching that project's own `docs/CAPABILITY_MATRIX.md` section 8 exactly:
  `silence` -> `measure.audio.silence`, `loudness` -> `measure.audio.loudness`, `integrity` -> `measure.audio.integrity`,
  `scene_detection` -> `measure.video.scene_detection`, `timing` -> `measure.video.timing`, `media_probe` ->
  `measure.media.probe`, `stream_layout` -> `measure.media.stream_layout`, `video_format` -> `measure.video.probe`,
  `audio_format` -> `measure.audio.probe`, `duration` -> `measure.media.duration`.
- Three of those (`silence`, `loudness`, `integrity`) are the ecosystem's one documented Capability collision:
  `qc-skill` independently implements the same three measurements with no shared code and publishes the identical id
  for each in its own `contract.py` (companion PR), so a registry sees one Capability with two Providers, not two
  unrelated things that happen to share a name.
- The last five ids (`media_probe`, `stream_layout`, `video_format`, `audio_format`, `duration`) were left unassigned
  in an earlier version of this contract, while `CAPABILITY_MATRIX.md`'s own section 8c still bundled them as one
  unpinned note, specifically to avoid two risks: colliding with `qc-skill`'s `measure.video.format`, and colliding
  with `ffmpeg-skill`'s base-layer `probe` tool. Both were resolved there by reading the actual implementations, not
  by assumption: `video_format` (this Skill's raw, threshold-free probe of one video stream) and `measure.video.format`
  (qc-skill's pass/fail judgment against caller-supplied thresholds) measure genuinely different things - confirmed
  by comparing `analyzers/probe.py`'s `VideoAnalyzer` against qc-skill's `rules.py` check, not merely by their
  descriptions - so `video_format` got its own id, `measure.video.probe`, naming the distinction (`probe`, not
  `format`) in the id itself; `ffmpeg-skill`'s `probe` tool overlap was already confirmed a base-layer *tool*
  overlap, never a Capability collision, since this Skill talks to `ffprobe` directly with no dependency on the
  `ffmpeg-skill` package - so the two ids never share a namespace (`measure.*` vs. `ffmpeg-skill.*`) regardless.
  `duration` gets its own id (`measure.media.duration`) too, consistent with every other analysis kind here, even
  though `TimingAnalyzer.analyze`'s `duration` branch reports only a strict subset of what `media_probe` already
  returns (`_durations(p)`, no packet-timestamp work) - kept separate rather than folded in, because every other
  kind in this contract is independently addressable regardless of overlap in what it measures.
- Additive: a new top-level `provides` key, verified against `contract_check.check_contract()` (reports zero
  problems with `provides` present - `check_contract` does not assert on this field, so nothing needed to change
  there) and against `evals/contract_evals.py`'s `C11_contract_registry_consistency`.

## ADR-023 Error classes FATAL / RETRYABLE / BLOCKED, additive to codes
- Context: AI-video-production-OS `FAILURE_RECOVERY.md` section 2 separates terminal failures (validation, security,
  missing Provider) from bounded-retryable ones (timeout, transient process failure) and proposes that terminal ones
  never consume a retry budget. A caller could derive that from our codes, but every caller would derive it
  differently.
- Decision: each error code carries a class, published in `contract --json` → `errors.classes` and on every error
  (`error.class`, result / response `error_class`). Codes, messages and exit numbers are unchanged. `ANALYSIS_FAILED`
  is RETRYABLE because a non-zero ffmpeg / ffprobe exit that is not a parameter error is the OS's "transient tool
  failure" category; `CACHE_MISS` is FATAL because retrying `cache_policy: only` unchanged cannot succeed.

## ADR-024 The OS denylist, applied recursively, in addition to the parameter schemas
- Context: unknown parameter names were already `INVALID_INPUT`, so `api_key` inside `parameters` was rejected — but
  as "unknown parameter", not as a security rejection, and only inside `parameters`. `SKILL_SPEC.md` section 3.1
  asks for the canonical list (`command, argv, shell, exec, filter_complex, filter, api_key, token, env`) to be
  rejected recursively and never narrowed.
- Decision: `AnalysisRequest.FORBIDDEN_KEYS` is that list plus our own `args`, `cmd`; `forbidden_keys()` walks the
  whole request document case-insensitively and reports the paths. The list is published under `security` in the
  contract and drift-checked; a contract claiming a narrower list is a `contract --check` failure.

## ADR-025 doctor reports per Capability, from `provides`
- `doctor --json` → `capabilities[]` maps each published Capability id to AVAILABLE / MISSING through the tool that
  implements it. This is derived from `provides` and the analyzer availability rows, never a separate table, so it
  cannot disagree with the contract.

## ADR-026 The JSON protocol is UTF-8 bytes, not the platform text encoding
- Context: `print(json.dumps(..., ensure_ascii=False))` went through Python's text layer, whose encoding on a
  Windows pipe is cp1252 (cp932 on a Japanese console). A Japanese file name in `asset.path` raised
  `UnicodeEncodeError`: no document on stdout, a traceback on stderr, exit 1 — the protocol was broken exactly for the
  media this Skill exists for. Reproduced with `PYTHONIOENCODING=cp1252` on Linux.
- Decision: `--json` output is written to `sys.stdout.buffer` as UTF-8; stdin request documents are read from
  `sys.stdin.buffer` and decoded as UTF-8 (`INVALID_INPUT` if they are not); human-readable streams are reconfigured
  with `errors="backslashreplace"` so they degrade instead of crashing. The contract's `execution.stdin` /
  `execution.stdout` state UTF-8. `ensure_ascii=False` is kept so paths stay readable.
## ADR-027 A self-conformance command, separate from contract drift checking
- Context: AI-video-production-OS `SKILL_SPEC.md` section 8 defines 8 conformance checks; 3 (`publishes_contract`,
  `lifecycle_declared`, `dependency_version_ranges`) are answerable from a contract document alone and the OS's own
  `registry/conformance.py` already implements those; the other 5 need "a callable submitting a request... and
  returning the Skill's response" — per-Skill wiring the OS repository documents as future work, not built there yet.
- Decision: `media-analysis conformance --json` implements all 8 checks against this installation directly, since
  this Skill already owns the request path being tested. Two checks are honestly `NOT_IMPLEMENTED` rather than
  forced to a vacuous PASS: `no_clobber_input` (no tool ever produces output — `produces_output` is false everywhere
  — so there is no output-path parameter that could equal an input) and `dependency_version_ranges` (no
  `dependencies` field is published). This is separate from `contract_check.py`: that module asks whether a *saved*
  contract document still matches the implementation (drift); this one asks whether the implementation actually
  behaves as claimed, by submitting real requests and scanning real source. Neither replaces the other.

## ADR-028 The cache is keyed by a per-analyzer algorithm version, not the package version
- Context: ADR-002 made every analyzer's `version` the package version and put it in the analysis identity, on the
  premise that the version moves when a measurement changes (README "Versioning": a measurement change bumps the
  minor version, "which invalidates cached Observations by design"). Since PR #17 the version is single-sourced from
  `pyproject.toml`, and the release workflow bumps it (patch, by default) on every merge to `main`. So every release
  — v0.1.4 was a docs-only one — changed every identity and every observation id and emptied every cache, which is
  what neither ADR-002 nor the README intended: both tie invalidation to a change in what is measured.
- Options: (a) key on the package `major.minor` only, as the README's wording implies; (b) a version per analyzer,
  bumped by hand when that analyzer's output changes. (a) still empties all ten analyzers' caches when one of them
  changes, and relies on every measurement change also being labelled as a minor release; (b) invalidates exactly
  what changed and is checked where the change is made (the analyzer's own class).
- Decision: (b). Each concrete analyzer declares `algorithm_version` (a decimal integer string, `"1"` for all ten
  at introduction; a test fails if an analyzer inherits it instead of declaring it). The analysis identity and the
  cache key are `sha256(canonical_json({asset_fingerprint, analyzer, algorithm_version, kind, parameters}))`; the
  cache entry format is `media-analysis-cache/2` with `metadata.algorithm_version` (and `metadata.package_version`
  kept as a record). Provenance is unchanged: `tools[].version`, `source = media-analysis/<tool>@<package version>`
  and `analysis.analyzer_version` still carry the package version (ADR-001, ADR-002). A cache hit returns the
  Observation as it was measured, so its `source` names the package version that measured it, which can be older
  than the installed package — that is the honest provenance of the value. New Observations add
  `analysis.algorithm_version` so the identity can be recomputed from the Observation alone.
- Rule for contributors: change how an analyzer measures (a default, a filter option, a parser, a rounding, a new or
  renamed `data` key) → bump that analyzer's `algorithm_version` in the same PR (and the minor version as README
  "Versioning" says, by labelling the PR). Anything else (docs, CI, other analyzers, refactors with identical output)
  leaves it alone.
- Contract impact (`contract@1`, `schema_versions` unchanged): added `tools[].algorithm_version` and the optional
  Observation schema property `analysis.algorithm_version` (`contract --check` now compares
  `tools[].algorithm_version`, fixture `algorithm_version_mismatch`); the descriptive lists `cache.key` and
  `identity.analysis_identity` now name `algorithm_version` instead of `analyzer_version`. Those two lists describe
  how this Skill computes its own keys; no request or response field changes shape, and video-production-agent's
  adapter (`check_contract`, response checks) reads neither list, so it accepts the new contract unchanged. Cost:
  one last invalidation — identities computed under ADR-002 are not looked up again, and observation ids change
  once.


## ADR-029 Acceptance measurements for the new Skills: a noise floor / SNR estimate and the A/V start offset
- Context: roadmap step 3 asks for "measurements new skills need for acceptance — noise floor / SNR (voice-enhance),
  A/V offset — only if not qc-skill's". Read on 2026-10-31 (read-only): every sibling `ECOSYSTEM_ROADMAP.md` in the
  ecosystem, `voice-enhance-skill/docs/decisions.md` ADR-001, qc-skill's contract (`measurements`, `provides`) and
  ffmpeg-skill's `_contract.py`. The acceptance criteria ("Done when") that need a measured value are:

  | Skill / step | acceptance criterion | measurement it needs | who measures it today |
  |---|---|---|---|
  | voice-enhance 3 ISOLATE | "SNR rises on a synthetic voice+noise fixture" | SNR / noise floor | nobody: qc-skill has peak / RMS / clipping / true peak (`astats`, `ebur128`), no floor or SNR; ffmpeg-skill computes a 20th-percentile floor inside `caption.py` but does not publish it |
  | voice-enhance 4 DEREVERB | "same pattern" (ADR-001: "SNR / reverberation measures") | a reverberation measure (RT60 / DRR / C50) | nobody |
  | voice-enhance 5 BLEED_REDUCE | "lower cross-talk energy on a 2-track fixture" | energy of one track / channel | per channel: nobody (qc-skill's per-channel levels are whole-file peak / RMS, not a floor) |
  | voice-enhance 6 remux, dubbing 4/5 | "video stream byte-identical", "A/V duration match" | A/V duration difference, A/V start offset | duration: here (`timing.av_mismatch`) and qc-skill; start offset: nobody |
  | dubbing 3, sound-design 2 | "duration within tolerance" | duration | here (`duration`), qc-skill, ffmpeg-skill `probe` |
  | video-production-agent 10 | sharpness / exposure scores for take selection | picture sharpness / exposure | nobody (named there as a gap) |

  The roadmap scopes this step to the first two rows' family (noise floor / SNR, A/V offset); sharpness / exposure is
  recorded as a further gap, not implemented here.
- Decision 1 — new kind `noise_floor` (tool `media-analysis/noise`, Capability `measure.audio.noise_floor`): ffmpeg
  `astats` with `reset=1` after `asetnsamples` gives the RMS level of consecutive non-overlapping windows (default
  50 ms, sized from the stream's own sample rate); `ametadata=print` puts one value per window on stderr (log level
  `repeat+info`, so ffmpeg never folds identical lines). The noise floor is a low nearest-rank percentile of those
  levels (default p10), the signal level a high one (p95), and `snr_db` their difference — the standard no-reference
  estimate for speech with pauses. All channels pooled by default; `channel` (0..63) measures one channel, which is
  what BLEED_REDUCE's "cross-talk energy" on a 2-track fixture needs (two streams: use `stream`). Digital-silence
  windows stay `-inf`: a percentile that lands on one is `null` and listed in `unmeasurable`, never a number.
  The eval pair `noisy.wav` / `cleaned.wav` (same tone bursts, noise 20 dB apart) measures SNR 35.7 → 55.7 dB, which
  is exactly voice-enhance step 3's acceptance shape.
  - Not chosen: a reference-based SNR (`10·log10(Σref² / Σ(x−ref)²)`). It is exact on synthetic fixtures but needs a
    second input aligned to the sample, which no request of this Skill takes (one `input` per request), and the
    real use — a field recording before / after ISOLATE — has no clean reference. The percentile estimate works on
    both. Not chosen: ffmpeg's own `astats` `Noise_floor`: it is a whole-stream single number (the minimum of the
    local peak level over `length`-second windows), a peak-based floor that is not on the same scale as an RMS signal
    level, comes with no matching "signal level" to subtract it from, and cannot be re-derived at another percentile.
    Window RMS levels keep the whole distribution (`percentiles_dbfs`), so the caller sees how the floor was chosen.
  - Not chosen now: a reverberation measure for DEREVERB. Blind RT60 / DRR estimation from speech is a model-based
    estimate (no deterministic, published, threshold-free method that this Skill could state without a confidence
    claim, which ADR-001 / CLAUDE.md forbid). It stays a gap until a method is chosen in its own ADR; voice-enhance
    step 4 can meanwhile measure its fixture's noise floor and SNR with this kind.
- Decision 2 — `timing` adds `av_start_offset`: the first audio stream's `start_time` minus the first video stream's,
  `{status, video_index, audio_index, video_start_time, audio_start_time, offset, tolerance, within_tolerance}`,
  `not_performed` without both start times. This is the offset a mux introduces (an `-itsoffset`, an edit list, a
  replaced audio track — voice-enhance step 6 and dubbing put audio back with ffmpeg-skill `audio --replace`); the
  existing `av_mismatch` only compares durations, which stay equal when the whole track is shifted. New parameter
  `av_start_offset_tolerance`, default 0.045 s: ITU-R BT.1359-1's detectability threshold is audio 45 ms early /
  125 ms late; the default uses the tighter bound on both sides. As with `av_mismatch`, `within_tolerance` is a
  convenience comparison against the caller's number, not a verdict.
  - Not chosen: lip-sync offset from content (does the sound match the mouth?). It needs a sync marker (flash + beep)
    or a model; neither is deterministic measurement of arbitrary media. Not chosen: the offset between two
    recordings by audio cross-correlation — that is ffmpeg-skill's `sync` (offset / drift JSON), so re-implementing
    it here would be a second owner for the same job.
- Versions: `noise` starts at `algorithm_version` "1"; `timing` goes to "2" (a new `data` key, ADR-028 rule), which
  invalidates cached `duration` / `timing` Observations once. Contract `@1` additive: one tool, one kind, one
  `provides` entry, one parameter, `filter:astats` in `capability_names`. `measure.audio.noise_floor` is new — not in
  AI-video-production-OS `CAPABILITY_MATRIX.md` yet, and not a collision (qc-skill publishes no noise measurement).
  video-production-agent pins this contract (`tools/media_analysis/contract_*.json`) and needs a re-pin to see the
  new tool.

## ADR-030 An editing-style profile (`edit_style`) and a profile diff
- Context: roadmap step 7. An editor matching a reference edit (a channel's house style, a client's sample) needs the
  reference's pacing, subtitle habits and audio level as numbers, and the same numbers for their own cut, so the gap
  is stated per item rather than by eye. Every input to that already exists as a filter this Skill runs (`scdet`,
  `ebur128`, `silencedetect`) or as a stream ffmpeg can read (text subtitles); what is missing is one profile that
  puts them on one time base, in frames, and a comparison of two profiles.
- Decision: new kind `edit_style` (tool `media-analysis/style`, Capability `measure.video.edit_style`), one file per
  request like every other kind.
  - Shots are measured in frames: the cut is the frame index scdet flags (`frame:N` of `metadata=print`), the last
    shot ends at the number of frames scdet saw, so a shot length is a difference of integers and hard cuts are exact
    to the frame. Seconds are frames / fps. `min_shot_duration` (default 0.25 s, rounded to frames) drops a cut that
    follows the previous kept cut too closely (a flash or a dissolve flagged on consecutive frames). Distribution:
    median (average of the two middle shots), nearest-rank p10 / p25 / p75 / p90, min / max / mean, and a histogram
    on fixed edges (0, 0.5, 1, 2, 4, 8, 16 s) so two profiles always share buckets.
  - Subtitles: each text subtitle stream is converted by ffmpeg to ASS on stdout (`-c:s ass -f ass -`, the one place
    this Skill reads a non-null output, still a pipe, never a file); events give count, events per minute, the share
    of the running time covered (overlaps merged) and the vertical band of each event from the style `Alignment`,
    `\an` / `\a` overrides and `\pos` / `\move` against `PlayResY`. This measures subtitle events that exist; it does
    not make or edit subtitles (still out of scope).
  - Audio: integrated loudness, loudness range and true peak (EBU R128) and the silence share / longest silence, from
    one ffmpeg pass (`ebur128,silencedetect`).
  - A missing stream is `status: not_performed` inside the profile, not an error, so an audio-only or video-only edit
    still has a profile and the diff reports the missing side as `unavailable`.
  - `media-analysis style profile <input> --out <file>` saves the Observation (profile + provenance; the file must be
    inside the workspace, the same write rule as the cache). `media-analysis style diff <reference> <candidate>`
    returns, per metric, candidate − reference and the relative difference, and equality for fps / subtitle presence /
    dominant position. The diff never says which edit is better or what to change: the caller's rule does.
- Not measured, and listed in every profile's `unmeasured`: burned-in (open) subtitles, which need text detection in
  the picture, and whether a music bed runs under speech, which needs an audio classifier. Both would be model
  outputs with a confidence, not filter measurements (CLAUDE.md invariant); `audio.active_share` is reported instead.
  Not chosen: averaging scdet's `time` values into seconds-based shot lengths — floating timestamps of VFR or
  edit-listed files round differently, while frame counts are what an editor counts.
- Versions: `style` starts at `algorithm_version` "1"; other analyzers unchanged. Contract `@1` additive: one tool,
  one kind, one `provides` entry, six parameters; `capability_names` unchanged (the three filters were already
  required). video-production-agent's adapter accepts extra tools; it sees `edit_style` after a re-pin of its
  contract snapshot.

## ADR-031 A B-roll library index as a CLI workflow over the existing kinds
- Context: roadmap step 8. An editor's own footage folder is searched again for every project ("a portrait clip under
  10 s with no sound", "the beach shots"). The facts needed to answer most of that are measurements this Skill already
  makes per file; what is missing is running them over a folder, keeping them, and querying them. Content labels
  ("beach", "interview") are a model's job, which this Skill does not do (CLAUDE.md invariant).
- Decision: `media-analysis library index` / `library search`, a CLI workflow in `library.py`, not a new analysis kind.
  - Why not a kind: a request has one `input` file and primitive parameters, and an analyzer never writes files; an
    index is many files in and one file out. Every number in the index still comes from an Observation the engine
    produced (`media_probe`, `scene_detection`, `loudness`), under the same path policy (the folder is the only
    allowed input root, the index must be inside the workspace), timeouts and optional cache.
  - Metadata and measured tags follow fixed, documented rules (orientation with rotation applied, resolution class of
    the short side, frame-rate class, codec, shot structure, audio present / silent, duration class, extension).
    "Scene type" is shot structure (`single_shot` / `multi_shot` / `audio_only`), not what the picture shows. The
    representative frame is a time (the middle of the longest shot); no image is extracted, so the folder and the
    workspace get no media written to them.
  - Tags from elsewhere (for example a vision model run by the caller) come in through `--tags` and are kept apart
    from measured tags (`external_tags` vs `measured_tags`, both in `tags`), with the fingerprint of the file they
    describe; a changed file drops them (`external_tags_dropped`) rather than carrying a stale label.
  - Incremental re-scan: size and modification time (ns) equal to the index entry → carried over without opening or
    hashing the file; otherwise measured again (the engine fingerprints it). This is the usual trade: a same-size
    rewrite with a restored mtime is not noticed; `--cache-dir` still makes a forced full re-scan cheap, because
    unchanged content hits the Observation cache.
  - Search is plain filtering: every condition must hold and every query word must prefix-match a tag or a word of the
    relative path; ranking is matched words + exact tag matches, ties by path, so results are deterministic.
- Not chosen: a database (SQLite) — one JSON file is enough for a personal folder, diffable and readable by the agent
  without a driver. Not chosen: thumbnails — writing images is media output, and ffmpeg-skill extracts a frame from
  `representative_time` when one is wanted. Not chosen: a motion / brightness class — it needs a further filter pass
  per clip (`signalstats`) and no consumer asked for it yet; it can be added as a measured tag later.
- Contract unchanged (no tool, kind or `provides` entry added); the agent pin is not affected. The agent can call
  `library` as a subprocess like `run`, but it is not part of the `run` protocol.

## ADR-032 Highlight candidates from a long recording, on sentence boundaries
- Context: roadmap step 9. From a talk or a lecture, an editor wants a short list of spans that work on their own
  (for a Short or a trailer), with why each one was picked, and boundaries that do not cut a sentence in half.
  Picking the final clip and cutting it stay with the editor and the editing tools.
- Decision: `media-analysis highlights`, a CLI workflow in `highlights.py` (not an analysis kind: it needs a
  transcript and optional signal spans, which a request's primitive parameters cannot carry).
  - Boundaries come from the transcript, not from audio: sentences end on sentence-final punctuation (Latin and CJK)
    or a speaker change, using word times when the transcript has them. A candidate is a run of whole sentences, so
    its start and end are sentence boundaries by construction (and the tests check it).
  - Signals: speech-energy lift is measured here (window RMS with astats, the `noise_floor` method, over the whole
    recording; mean of the span minus the recording's median). Keyword density and speaker-change edges come from
    the transcript. Applause and laughter are taken as spans from the caller: detecting them needs an audio
    classifier, whose engine and licence roadmap step 5 decides; until then this workflow does not guess them from
    loudness, and says in `not_measured` when they were not given.
  - Scoring is a fixed, documented sum (weights and curves in the output itself), chosen so that padding a strong
    span with weaker sentences lowers its score: energy uses lift / (lift + 6 dB), which never saturates, and keywords
    are counted per sentence. Speaker edges get a small weight (0.25) so they break ties between equal spans without
    pulling long spans ahead. The best non-overlapping candidates are returned with their components and reasons.
    The score orders measured evidence; it is not a judgement that a span is good.
- Not chosen: aligning boundaries to silences instead of sentences — a pause is not always a sentence end and
  sentences run on without one; the transcript knows where sentences end. Not chosen: running a speech model to get
  a transcript — transcription-skill owns that; its output shape is accepted as is.
- Contract unchanged (no tool, kind or `provides` entry); the agent pin is not affected.

## ADR-033 Burned-in subtitles in `edit_style`, from an edge map
- Context: roadmap step 10. ADR-030 left `subtitles.burned_in` as `not_measured`. Most finished edits on video
  platforms carry their subtitles drawn into the picture, so a style profile without them misses the habit that is
  easiest to see: how much of the time text is on screen, where, how large, and whether it sits on a band or has an
  outline.
- Considered first: qc-skill's `video.text_regions` as an optional dependency. It measures contrast and ink height
  inside boxes the caller names; it does not locate text. Using it would still need the location from somewhere, so
  the location is measured here and qc-skill is not called. Feeding the boxes found here to it for a WCAG contrast
  figure is a possible follow-up (a caller can do it today with the reported boxes' positions and sizes).
- Decision: `burnin.py`, called by the `style` analyzer, only ffmpeg on PATH as before.
  - One ffmpeg pass samples a frame every `burned_in_interval` seconds (default 1 s; 0 turns it off), scales it to
    480 px wide, and writes the grey picture and its `edgedetect` edge map side by side to stdout (raw bytes, no file).
  - Text rows are rows whose edge map has at least 8 separate edge runs (glyph strokes give many short vertical
    edges; a horizon, a box border or a picture edge gives a few long ones). Runs of text rows 2–20 % of the frame
    high are lines; within a line, edge columns joined across gaps shorter than 1.5 line heights are regions (words
    of a subtitle join; two subtitles side by side stay apart).
  - Per region: position by thirds; text height = rows around the line where a tenth of the region's pixels are far
    (40 levels) from the background level next to it — ink plus outline — over the frame height (the edge map is
    blurred by edgedetect's smoothing, so the picture gives the tighter figure); approximate characters = width /
    (0.55 × height); band = the strip just above / below differs from the picture further out by 24 levels and is
    flat; outline = among the pixels far from the background, at least 8 % lie on the other side of it from the ink.
  - Per recording: coverage = sampled frames with text / frames; switches = text appearing, or its regions moving by
    more than half a line height or changing in number; per minute over the duration; position counts; medians.
  - `style diff` compares the new numbers and `present` / `dominant_position`.
  - Frames are read from ffmpeg's stdout one at a time (`runner.stream_records`: fixed-size records, stderr drained on
    a thread, a watchdog kills the process group at the timeout) and reduced to their regions at once, so memory
    stays at one frame for any length of input. No sampled frame (a clip shorter than the interval) is
    `not_performed`, so a diff sees it as unavailable rather than as "no subtitles".
  - Video streams are selected as `0:V:n` (capital V, which skips attached pictures) here and in the `style`
    analyzer's scdet pass, matching `streams_of()`'s numbering when cover art comes first.
- Limits, stated in the output's `method` and in docs/tools.md: no OCR, so any text in the picture counts (signs,
  captions, a lower third) and very detailed pictures (foliage, fine patterns, a test card's digits) can read as text;
  timing resolution is the sampling interval; coverage is a share of sampled frames. A model-based text detector
  would be more selective but is a model, which this Skill does not run.
- Versions: `style` `algorithm_version` "1" → "2" (`burned_in` changes from a string to an object, ADR-028 rule), so
  cached `edit_style` Observations are recomputed once. Contract `@1` additive: one parameter, `filter:edgedetect` in
  `style`'s required capabilities and in `capability_names`, the tool description. video-production-agent's
  `check_contract` accepts it unchanged; a re-pin shows the new parameter and capability.
