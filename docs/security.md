# Security boundary

## What is enforced

| rule | where |
|---|---|
| No shell: every child process is started from an argv list (`subprocess.Popen(argv)`), never through a shell; `os.system`, `shell=True`, `eval`, `exec` and `os.popen` do not appear in the package (tested) | `runner.py`, `tests/test_unit.py::test_no_shell_execution_in_source` |
| No command passthrough: `AnalysisRequest` rejects `command`, `argv`, `args`, `cmd`, `shell`, `exec` fields and unknown fields; parameters are validated against a per-kind schema (name, type, range); batch `budget` accepts only the three enforced names | `contract.py`, `budget.py` |
| No executable configuration: ffmpeg / ffprobe are found by `PATH` lookup only; no request field, CLI flag or environment variable names an executable (tested: the process environment is read in exactly two places, the child-environment filter and the secret-leak check) | `capabilities.py`, `runner.py`, `tests/test_contract.py` |
| ffprobe / ffmpeg argv are built by this package from structured data only; the only user-controlled argument is the resolved absolute input path, always placed after `-i`; filter strings are formatted from validated numbers | `runner.py`, `analyzers/*` |
| `-protocol_whitelist file`: an input can never be a network, pipe, concat or device source; `-nostdin` | `runner.py` |
| Inputs must be existing regular files; symlinks are resolved before checks; `--allowed-input ROOT` confines inputs to roots (default: any readable regular file, like a CLI user would expect; set the roots when embedding) | `security.py` |
| Writes (cache) resolve to a location inside `--workspace` (default: current directory); `..`, absolute paths outside and symlinked directories outside are refused | `security.py` |
| Child processes receive a minimal environment (`PATH`, `HOME`, temp and locale variables only); secrets in the parent environment are not inherited | `runner.py` |
| Timeouts kill the whole process group (`start_new_session` + `killpg(SIGKILL)` on POSIX; on Windows the child is killed directly, and ffmpeg / ffprobe spawn no grandchildren); no partial observation, no cache entry is written (tested with a grandchild process and with real ffmpeg) | `runner.py`, `tests/test_contract.py`, `tests/test_integration.py` |
| stdout / stderr contract: with `--json` stdout carries exactly one response document, on success and failure alike, so an adapter parses JSON and never text; stderr is diagnostics only | `cli.py` |
| Observations are verified before they leave: no `command` / `argv` / `cmd` / `shell` keys, no secret-looking keys (`token`, `password`, `api_key`, …), no value equal to a secret-looking environment variable, finite numbers, timestamp range, source format | `verify.py` |
| stdout / stderr of ffmpeg / ffprobe are parsed and discarded; only structured values enter Observations. Error details carry at most the last few stderr lines of a failed run | `analyzers/*`, `probe.py` |
| No credentials are read, stored or needed. No network access | whole package |

## What is not enforced

- Reading: without `--allowed-input`, any regular file the process can read is accepted (same posture as
  video-production-agent ADR-010 for user-provided paths). Embedders (the agent adapter) should set the roots.
  Relative inputs resolve against the process working directory, symlinks are resolved before the root check,
  and a symlink inside a root that points outside is refused.
- Resource use: a decode-based analysis (`integrity`, `loudness`, `silence`, `scene_detection`) reads the whole
  file; the budget limits wall time and calls, not memory or disk.
- The cache directory is trusted for confidentiality: Observations contain absolute input paths and file
  fingerprints. Tampering is detected (result hash) but the directory should not be world-writable.

## Reporting

Open an issue on the repository. Do not include media that you cannot share; the fixture generator can usually
reproduce a container layout synthetically.
