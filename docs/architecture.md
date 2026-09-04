# Architecture

```text
CLI (cli.py)  ──▶  AnalysisEngine (engine.py)
                     │  1. AnalysisRequest validation (contract.py)
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
                Observation JSON
```

## Layers

| module | responsibility | knows about |
|---|---|---|
| `contract.py` | Skill / Tool contract, `ANALYSIS_KINDS`, parameter schemas, `AnalysisRequest`, Observation envelope, analysis identity | nothing below |
| `engine.py` | the single pipeline above | everything |
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
A hit returns the stored Observation (relabelled with the request's `asset_id` / `analysis_id`, re-verified) and runs
no analyzer. Invalidation: asset changed, analyzer version changed, parameters changed, entry tampered or unreadable.
It is a reuse cache only: no job state, no resume, no dependency between entries.

## Budget

`Budget(max_analysis_calls, timeout, max_total_seconds)` is enforced by `BudgetTracker` per engine instance
(per CLI invocation). Cache hits do not consume calls. The effective per-run timeout is the minimum of the request
timeout, the budget timeout and the remaining total seconds. Exceeding a limit raises `BUDGET_EXCEEDED` before the
analyzer starts; a timeout kills the ffmpeg / ffprobe process group and raises `ANALYZER_TIMEOUT`. Budgets that this
package cannot measure (storage, GPU time, API cost) do not exist here.

## Connecting to video-production-agent (future adapter, not in this repository)

The agent's `SkillPackage` / `ToolSpec` (skills/contract.py) map one-to-one onto `media-analysis contract --json`:
`skill_id` → `skill_id`, `tools[].tool_id / version / required_capabilities / deterministic / produces_output=False`
→ `ToolSpec`. An adapter (`tools/media_analysis/` on the agent side) would:

1. locate the package (`media-analysis --version`, `doctor --json`) and register the `SkillPackage` with the detected version;
2. implement `ToolAdapter.measure(tool, args)` as `media-analysis run - --json` with an `AnalysisRequest` on stdin
   (process boundary, like ffmpeg-skill per ADR-001 of the agent), mapping `{"error": {...}}` to `ToolError`;
3. take `observation` from the result as the agent's `Observation` (`kind`, `asset_id`, `source`, `data`, `id`,
   `observed_at` are already in the agent's field names; `Observation.from_dict` drops the extra `analysis` / `asset`
   fields or the adapter keeps them in `data`);
4. add `media-analysis/<tool>` as tool candidates to the production skills `media_probe`, `silence_analysis`,
   `loudness_analysis` in the agent's registry. Capability names (`ffmpeg`, `ffprobe`, `filter:*`) already match its
   `CapabilityResolver` vocabulary.

Nothing in this repository imports or depends on the agent.
