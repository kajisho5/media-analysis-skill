# Architecture

```text
CLI (cli.py)  ──▶  AnalysisEngine.run(document) (engine.py)   document = request | [request] | {requests, budget}
                     │  0. per request (failures become error results, never abort the batch):
                     │  1. AnalysisRequest validation (contract.py, schemas.py)
                     │  2. PathPolicy: resolve input, allowed roots (security.py)
                     │  3. AnalyzerRegistry: kind → analyzer (registry.py)
                     │  4. CapabilitySet: required capabilities present? (capabilities.py)
                     │  5. asset fingerprint (sha256) → analysis identity (canonical.py)
                     │  6. ObservationCache lookup (cache.py)          ── hit ──▶ verified Observation
                     │  7. BudgetTracker check + effective timeout (budget.py)
                     │  8. Analyzer.analyze(AnalysisContext)           (analyzers/*.py → runner.py → ffprobe / ffmpeg)
                     │  9. make_observation + verify_observation (verify.py)
                     │ 10. cache put
                     ▼
                result {analysis_id, asset_id, kind, status, observation | error, cache{status, policy, key}, usage}
                response {schema, skill, status ok|partial|error, results[], observations[], usage, budget, warnings}
```

## Layers

| module | responsibility | knows about |
|---|---|---|
| `contract.py` | Skill / Tool contract (tools derived from the registry), `ANALYSIS_KINDS`, parameter schemas, `AnalysisRequest`, Observation envelope, analysis identity | registry (lazily, for the contract only) |
| `schemas.py` | transport-independent JSON schemas of request / batch / result / response / observation + a minimal validator | nothing |
| `engine.py` | the single pipeline above; `run()` builds the response document, `exit_code_for()` maps it to a process exit code | everything |
| `registry.py` | fixed list of analyzers; refuses to start if it disagrees with the contract | analyzers, contract |
| `analyzers/` | one class per tool: `plan()` (dry-run description) and `analyze()` (measurement) | `probe.py`, `packets.py`, `runner.py` |
| `probe.py`, `packets.py` | ffprobe execution + pure parsers (format / streams, packet timestamps) | runner |
| `runner.py` | argv-only subprocess execution with timeout, process-group kill, minimal environment | nothing |
| `capabilities.py` | detection of ffmpeg / ffprobe / filters (`doctor`) | runner |
| `cache.py`, `budget.py`, `verify.py`, `security.py` | Observation reuse, enforced limits, result verification, path policy | contract / canonical |
| `canonical.py` | canonical JSON, sha256 | nothing |

## Analyzer contract

```python
class Analyzer:
    id: str                      # tool name; tool id = "media-analysis/<id>"
    version: str                 # == package version; bump when a measurement changes
    supported_kinds: tuple       # AnalysisKinds served by this tool
    required_capabilities: tuple # "ffprobe", "ffmpeg", "filter:<name>"
    def plan(ctx, kind, parameters) -> [{"executable", "purpose"}]   # dry-run, runs nothing
    def analyze(ctx, kind, parameters) -> dict                       # data only; no envelope, no interpretation
```

`AnalysisContext` gives an analyzer the resolved input path, the detected executables, the effective timeout and a
lazily-run, shared ffprobe result. Analyzers never see `asset_id`, never write files and never receive argv.

Analyzers: `ProbeAnalyzer` (media_probe), `StreamAnalyzer` (stream_layout), `VideoAnalyzer` (video_format),
`AudioAnalyzer` (audio_format), `SilenceAnalyzer`, `LoudnessAnalyzer`, `IntegrityAnalyzer`, `SceneAnalyzer`
(scene_detection), `TimingAnalyzer` (duration, timing). The registry is a literal list; there is no plugin loader.

## Determinism

`analysis.identity = sha256(canonical_json({asset_fingerprint, analyzer, analyzer_version, kind, parameters}))`

- `asset_fingerprint` is the sha256 of the file content (not path, not mtime): a moved file keeps its identity, an
  edited file loses it.
- `parameters` are the *effective* parameters (defaults applied, validated), so `{}` and `{"stream": 0}` are the
  same analysis and `{"threshold_db": -40}` and `{"threshold_db": -40.0}` are the same analysis.
- The derived `analysis_id` (`analysis-<identity[:16]>`) and the observation id (`obs_<identity[:16]>`) follow from
  it. A caller-supplied `analysis_id` is a label and does not enter the identity.
- `asset_id` is the caller's label and does not enter the identity either (docs/decisions.md ADR-004).

Analysis id (what this analysis is called) and cache key (where its result is stored) are separate concepts that
happen to be computed from the same identity; the cache additionally verifies its stored metadata and result hash.

## Cache

`ObservationCache` stores `{format, metadata{key, analyzer, analyzer_version, kind, parameters, asset_fingerprint,
created_at, result_hash, package_version}, observation}` as one JSON file per key under `<workspace>/<cache-dir>/`.
A hit returns the stored Observation (relabelled with the request's `asset_id` / `analysis_id`, re-verified), runs
no analyzer and counts `usage.analyzer_calls = 0`. Result `cache.status`: `hit`, `miss` (no entry; measured and
stored), `invalid` (entry present but stale or tampered; removed, measured, stored), `bypass` (request
`cache_policy: bypass`; neither read nor written), `disabled` (no cache configured). Request `cache_policy: only`
never runs an analyzer: a hit or `CACHE_MISS`. Invalidation: asset changed, analyzer version changed, parameters
changed, entry tampered or unreadable. It is a reuse cache only: no job state, no resume, no dependency between entries.

## Budget

`Budget(max_analysis_calls, timeout, max_total_seconds)` is enforced by `BudgetTracker` per engine instance
(per CLI invocation; a batch document's `budget` replaces it for that run). `Budget.from_dict` rejects any other
name. Cache hits do not consume calls. The effective per-run timeout is the minimum of the request
timeout, the budget timeout and the remaining total seconds. Exceeding a limit raises `BUDGET_EXCEEDED` before the
analyzer starts; a timeout kills the ffmpeg / ffprobe process group and raises `ANALYZER_TIMEOUT`. Budgets that this
package cannot measure (storage, GPU time, API cost) do not exist here.

## Connecting to video-production-agent

Nothing in this repository imports or depends on the agent; the agent is not modified by this repository. The
consumer's rules this contract satisfies are recorded in `tests/contract/agent_skill_package_contract.json`
(SkillPackage / ToolSpec / Observation rules of the agent's `skills/contract.py`, PR #7) and tested in
`tests/test_contract.py`.

```text
Agent                                                  media-analysis-skill
  1. capability / package detection  ───────────────▶  media-analysis doctor --json ; media-analysis contract --json
  2. SkillRegistry.register_package(SkillPackage)      contract.skill_id / version / tools[] map 1:1 onto SkillPackage / ToolSpec
  3. adapter.measure(tool, args) builds a request  ─▶  media-analysis run - --json      (AnalysisRequest JSON on stdin)
  4. adapter reads exactly one response document  ◀─  stdout                            (stderr: diagnostics only)
  5. Observation layer stores response.observations[]  Observation{kind, asset_id, source, data, id, observed_at}
  6. Inference / Decision / Plan / IR / Execution       — agent only —
```

Adapter recipe (agent side, `tools/media_analysis/`, not part of this PR):

1. **Locate and detect**: run `media-analysis doctor --json`; the package is AVAILABLE when `status` is `ok` (or
   `degraded` if only some tools are needed: use `unavailable_tools`). Read `media-analysis contract --json` once and
   build the `SkillPackage`: `skill_id`, `name`, `version`, `description`, `capabilities`, `repository`, `role` and one
   `ToolSpec` per `tools[]` entry (`tool_id`, `skill_id`, `version`, `description`, `required_capabilities`, `inputs`,
   `produces_output=False`, `deterministic=True`, `result_keys`). Capability names are already `CapabilityResolver`
   names (`ffmpeg`, `ffprobe`, `filter:<name>`).
2. **Measure**: `ToolAdapter.measure(tool, args)` writes an AnalysisRequest (`asset_id`, `input`, `kind` from
   `contract.kind_to_tool`, `parameters`, optional `timeout`, `cache_policy`) or a batch with a `budget`, starts
   `media-analysis run - --json` as a subprocess (process boundary, like the agent's ffmpeg-skill adapter, its
   ADR-001), feeds stdin, parses stdout as one JSON document. Never pass `--allowed-input`-violating paths, commands,
   argv, executable paths or credentials; the request schema rejects unknown fields and `PATH_NOT_ALLOWED` /
   `INVALID_INPUT` come back as error results.
3. **Map results**: `response.results[i].status == "ok"` → `ToolResult(ok=True, data=result)`; otherwise
   `ToolResult(ok=False, data=result["error"])` with `error_kind` as the failure class. Exit code 0 means every result
   was ok. `usage.analyzer_calls` and `cache.status` feed the agent's analysis budget accounting; a `hit` cost no call.
4. **Observations**: each `response.observations[j]` already has the agent's Observation field names (`kind`,
   `asset_id`, `source`, `data`, `id`, `observed_at`); `Observation.from_dict` drops `analysis` / `asset` (or keep them
   in `data`). `source` satisfies the agent validator (`"@" in source`, not `ai*`) and `_version_of` (package prefix
   before `/`).
5. **Registry**: add `media-analysis/probe`, `media-analysis/silence`, `media-analysis/loudness` as tool candidates to
   the production skills `media_probe`, `silence_analysis`, `loudness_analysis`; the other tools back new
   measurement skills when the agent needs them.

Never send from the agent to this Skill: arbitrary commands, shell strings, argv, executable paths, credentials, AI
reasoning, production plans, Project IR, job state. Never expect from this Skill: decisions, inferences, plans,
events, edited media.
