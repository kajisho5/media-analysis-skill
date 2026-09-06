# Testing

```text
pip install -e . pytest
python -m pytest -q               # 106 tests: unit + contract + integration + evals
python evals/run.py               # 9 measurement eval cases with expected values and tolerances
python evals/contract_evals.py    # 17 contract eval cases
```

FFmpeg is required. Integration tests and evals are **not skipped** when it is missing; the session fails, so a
green run always means the real ffmpeg / ffprobe were exercised. CI (`.github/workflows/tests.yml`) runs the whole
suite on Linux (Python 3.9, 3.11), Windows (choco ffmpeg) and macOS (brew ffmpeg).

## Fixtures (`tests/fixtures/generate.py`)

Nothing binary is committed; fixtures are generated with ffmpeg into a temporary directory (a few hundred KB, ~2 s):

| fixture | construction | used for |
|---|---|---|
| `av.mp4` | 6 s testsrc2 320x180 @25 H.264 + mono AAC 48 kHz; 1 kHz tone amplitude 0.1 only in t=2..5 s | probe, video, silence (leading 0–2, trailing 5–end), integrity PASS, cache, budget, CLI |
| `video_only.mp4` | 4 s video, no audio | no-audio paths (`UNSUPPORTED_FORMAT` for audio kinds), integrity without frames shortfall |
| `stereo.wav` | 3 s pcm_s24le 48 kHz stereo, same tone on both channels | audio_format, loudness −20.0 LUFS |
| `mono.wav` | 3 s pcm_s24le 44.1 kHz mono, 440 Hz | audio_format, integrity audio-only |
| `silence.wav` | 2 s digital silence | `entirely_silent`, loudness below the absolute gate |
| `multi.mp4` | 2 video (320x180 @25, 160x90 @30) + 2 audio (mono 48 kHz eng, stereo 44.1 kHz jpn) | stream ordinals, per-stream selection |
| `loud.wav` | 5 s continuous 1 kHz tone amplitude 0.1 → −23.0 LUFS, −20.0 dBTP, LRA 0 | loudness eval |
| `short.mp4` | 0.4 s (10 frames) | short video |
| `scenes.mp4` | three 2 s hard-cut segments | scene cuts at 2.0 and 4.0 s |
| `corrupt.mp4` | `av.mp4` with 3000 bytes zeroed inside `mdat` | integrity FAIL |

## Unit tests (`tests/test_unit.py`, no ffmpeg)

contract ↔ registry agreement (no undeclared or unimplemented tools) · AnalysisRequest validation · command / argv
rejection · kind validation · parameter validation (unknown, type, range, NaN) · probe / stream / video (CFR, VFR,
unknown) / audio parsers · silence parser + classification · ebur128 parser (incl. `-inf`) · integrity status
(PASS / WARN / FAIL) · scdet parser · malformed result · wrong asset / kind / analysis id / source · deterministic
identity (parameter order, asset / version / parameter changes) · cache hit / miss / relabel / disabled · cache
invalidation by asset, analyzer version, parameters · tampered and unreadable cache entries · timeout (real
subprocess) · budget (calls, total seconds, cache hits free) · unsupported analyzer · dry-run runs nothing and still
validates · path policy (roots, symlink escape, directories, NUL, workspace writes, argv shape) · no shell in source ·
secret leakage (env value, secret-looking key, child environment) · command / argv leakage · error model.

## Contract tests (`tests/test_contract.py`)

schema validator · contract JSON self-consistency (sections, schema versions, error / exit tables, budget / cache
vocab) · tools and kinds derived from the registry (`tool_spec(analyzer)` equality, coverage of all 10 kinds,
capability names) · consumer rules from `tests/contract/agent_skill_package_contract.json` (video-production-agent
SkillPackage / ToolSpec / Observation / capability-name rules) · request schema ↔ `AnalysisRequest` agreement (incl.
`strategy` / `budget` / `cache_policy` rejection) · response / result / observation schema roundtrip through
`engine.run` (ok, partial, error, dry-run, rejected document) · exit code table · process-group kill of a grandchild
· executable path not configurable · OS registry rules from `tests/contract/os_registry_contract.json` (provides
shape, one id per kind, documented collision published, denylist superset, recursive rejection) · error classes in
results and contract · doctor per-Capability AVAILABLE / MISSING · `check_contract` accepts the live contract ·
self-conformance (`conformance.py`, SKILL_SPEC.md section 8): all 8 checks PASS or honestly NOT_IMPLEMENTED, and
each fallible check is proven to actually detect a broken implementation (a validator that stops rejecting, a leaky
PathPolicy stub, a real `shell=True` planted in a scanned file) · CLI smoke ·
**21 drift fixtures**
(`tests/contract/cases`: valid, unsupported schema, missing / extra tool, tool / capability / kind / kind_to_tool /
version / schema-version / schema / invocation / provenance / exit-code mismatch, provides missing / renamed /
bad lifecycle / extra kind, denylist narrowed, error class drift) · `contract --check` CLI (file,
stdin, unreadable) · no confidence / judgement vocabulary in analyzers · batch identity independent of order.

## Integration tests (`tests/test_integration.py`, real ffmpeg)

MP4 probe · video stream analysis (CFR, frame count, short file) · audio stream analysis (stereo / mono) · silence
(leading / trailing, entirely silent, identity changes with threshold) · loudness (−23.0 / −20.0 LUFS, silent input) ·
integrity (PASS, FAIL on corruption, video-only, audio-only) · multiple streams (ordinals, per-stream video / audio /
loudness, out-of-range ordinal) · no-audio video · scene detection · timing / duration · **cache hit skips the
analyzer** (execution log: first run executed ffmpeg, second run executed nothing, a new engine reuses the disk cache)
· cache policies (`bypass`, `only` → `CACHE_MISS`) · budget and timeout with real processes · **timeout leaves no
ffmpeg process and no cache entry** (`/proc` scan on Linux) · determinism across runs · **all 10 kinds on real
multi-stream media, response validated against the published schemas, second batch entirely from cache** · CLI smoke
(doctor, probe, analyze multi-kind, cache hit, dry-run JSON / text, error inside the JSON document vs stderr, partial
batch with budget error, `run` with request file, argv rejection, `--allowed-input`, contract) · `run -` over stdin
with a batch and budget (partial status, per-result errors, exactly one stdout document), invalid JSON on stdin,
unknown budget field · **invalid-input matrix** over stdin (invalid JSON, missing asset / kind, unknown kind,
invalid / unknown parameter, path traversal, nonexistent file, non-media file, command field, executable override →
one parseable error response each, stderr empty) · absolute vs relative input (one identity) and `--allowed-input` ·
timeout inside a batch keeps the protocol (partial response, others served, no cache entry, no ffmpeg left) ·
**real-media matrix**: all 10 kinds × 10 fixtures validated against the schemas, expected `UNSUPPORTED_FORMAT` for
audio kinds on video-only and video kinds on audio-only media, second pass served from cache except the
never-cached errors · determinism across two processes.

## Contract evals (`evals/contract_evals.py`, `tests/test_evals.py`)

C01 contract JSON valid · C02 declared tools exist · C03 declared kinds exist · C04 observation schema valid ·
C05 request / response roundtrip · C06 unsupported field rejection (request, parameter, output_policy, batch budget) ·
C07 malicious command rejected · C08 path traversal rejected (input and cache dir) · C09 credential leakage rejected ·
C10 cache hit produces the identical observation with zero analyzer calls (real ffmpeg) · C11 contract / registry
consistency over every drift fixture · C12 invalid input JSON over `run -` · C13 timeout process tree (real ffmpeg) ·
C14 batch identity · C15 no-audio media · C16 deterministic result across engines (real ffmpeg) · C17 cache
invalidation (content, version, parameters, corruption).

## Measurement evals (`evals/cases/*.json`, `evals/run.py`, `tests/test_evals.py`)

Each case names its fixture, kind, parameters, the derivation of the expected values and a list of expectations
(`eq`, `approx` with tolerance, `gte`, `lte`, `startswith`) on paths in the Observation. Cases: known media probe,
known video format (stream 1 of a multi-stream file), known audio format, known silence, known loudness (mono and
stereo), integrity PASS and FAIL, known scene cuts. Tolerances: 0.05 s for container / segment times, 0.2 LU / dB
for loudness, exact for codecs / counts / dimensions.
