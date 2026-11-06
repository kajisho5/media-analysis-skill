# Roadmap — media-analysis-skill

Part of the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS/blob/main/docs/ecosystem/ROADMAP.md) ecosystem: department **検証 (Verification)**, status `active`.
Written from a read-only audit of this repository on 2026-10-30. Each step below is meant to be one pull request; tick it off by linking the PR.

## Where it stands

Version single-sourced from `pyproject.toml` (#17): `contract --json` → `version` equals the newest tag (0.1.7 at `v0.1.7`, checked 2026-11-05). Cache keyed by per-analyzer algorithm version, so releases no longer empty it (ADR-028). 12 measurement kinds (step 3 added `noise_floor`, step 7 `edit_style`). 98 test functions (129 collected), CI on 3 OSes. STATE.md current (#18).

## What this repository owns

`ecosystem.json` gives each job to exactly one repository. Work outside this list belongs to its owner and is handed over, not re-implemented.

| Owned job | Status | Note |
|---|---|---|
| メディア計測 | done |  |

## Wiring

The agent pins this contract at 0.1.7 (video-production-agent#93, ADR-078), `noise_floor` included. Calls ffmpeg/ffprobe directly (overlap with ffmpeg-skill probe/silence/loudness/scenes is intentional).

## Steps

- [x] **1. Single-source the version** — coordinated with the agent's version gate — [#17](https://github.com/kajisho5/media-analysis-skill/pull/17)
  - Done when: contract version == tag
- [x] **2. Refresh STATE.md** — [#18](https://github.com/kajisho5/media-analysis-skill/pull/18)
- [x] **3. Measurements new skills need for acceptance** — noise floor / SNR (voice-enhance), A/V offset -- only if not qc-skill's — new kind `noise_floor` (`measure.audio.noise_floor`) and `timing.av_start_offset`; selection, sources and what stays open (reverberation, sharpness / exposure) in ADR-029 — [#21](https://github.com/kajisho5/media-analysis-skill/pull/21)
  - Done when: evals
- [ ] **4. Speech regions (VAD)** — new kind `speech_regions` (`measure.audio.speech_regions`): where someone speaks, as time ranges with a probability, the speech share and the longest pause, measured by silero-vad (MIT code and weights, ONNX on CPU) as the optional extra `vad`; without it the kind is `UNKNOWN`, never guessed from silence. Different from ffmpeg-skill `silence` / `scenes --speech` (level and spectrum, not a speech model); the ADR compares the three on the same fixtures. Consumers: the agent's edit view (video-production-agent step 14), cut boundaries that must not land mid-word, dubbing gaps
  - Done when: ADR; on speech over music and over noise, speech-region F1 above 0.9 against hand labels; offline, nothing downloaded at run time
- [ ] **5. Highlight signals for clips** — new kinds measured, never judged: applause and laughter spans (an audio classifier, engine and weights licence checked in an ADR; YAMNet, Apache-2.0, is a candidate), speech-energy peaks, speaking-rate changes, and screen-change bursts, each as time ranges with a score. Field review 2026-11-05 (Velisa ROADMAP, third round)
  - なぜ要るか：講演や配信のアーカイブから、Shorts に切り出す候補を素早く挙げられる
  - Done when: ADR; a fixture with applause and a laugh gives both spans within 0.5 s
- [ ] **6. Incremental measurement on growing files** — a measurement run on a file still being written measures only the new part since the last run and appends to the cached result
  - なぜ要るか：収録中の長尺ファイルでも、計測を最初からやり直さずに追いかけられる
  - Done when: a fixture appended twice gives the same result as one run on the final file
- [x] **7. Editing-style profile and comparison** — measure a reference edit's style — cut rate, shot-length distribution, whether subtitles are present, where they sit and how often they appear, loudness (LUFS), music bed — into a style profile (JSON), measure another edit with the same metrics and report the difference item by item. Done (ADR-030): new kind `edit_style` (`measure.video.edit_style`), shot lengths in frames from scdet, soft-subtitle events / coverage / position, EBU R128 loudness and silence share; `media-analysis style profile --out` saves a profile, `style diff` compares two. Not measured, listed in every profile's `unmeasured`: burned-in subtitles and music-bed presence (both need a model, not a filter) — [#24](https://github.com/kajisho5/media-analysis-skill/pull/24)
  - なぜ要るか：お手本の編集テンポや字幕・音量の癖を数値で掴み、自分の編集とのずれを項目ごとに確かめられる
  - Done when: on two synthetic fixtures with known cut intervals, the median shot length matches the true value within ±1 frame, and the diff report shows the correct differences
- [x] **8. B-roll library index** — scan a folder of the user's own footage, give each clip metadata (duration, resolution, representative frame, shot / scene type) and tags, keep an index, search it by keyword and condition. Tags come from this Skill's own measurements; tags from an AI model are passed in by the caller (the Skill holds no model). A re-scan analyses only files that changed. Done (ADR-031): `media-analysis library index <folder> --index <file> [--tags <file>]` measures each clip through the engine (`media_probe`, `scene_detection`, `loudness`) into metadata (duration, resolution / orientation / frame-rate class, shot count, representative frame time = middle of the longest shot, scene type single / multi shot or audio only, loudness) and measured tags; caller tags are kept with the file fingerprint and dropped when the file changes; `library search` by keyword (tags and path words), tag, duration, orientation, height, audio and scene type; a re-scan skips files whose size and mtime are unchanged without opening them. Scene type is shot structure only, not content (beach, interview): content labels come in through `--tags`. No thumbnail is written — the representative frame is a time — [#25](https://github.com/kajisho5/media-analysis-skill/pull/25)
  - なぜ要るか：手持ちの素材から使えるカットを、毎回見返さずに条件で探せる
  - Done when: indexing a fixture folder then searching returns the expected clips, and a second scan does not re-analyse unchanged files
- [x] **9. Highlight candidates from long recordings** — from a long video (a talk, a lecture), list self-contained spans as candidates with reasons, using loudness peaks, applause, laughter (spans from step 5 once it lands, or passed in by the caller), speaker changes and transcript keywords, with each start / end on a sentence boundary. Lists candidates only; cutting stays with the editing tools. Done (ADR-032): `media-analysis highlights <input> --transcript <file> [--signals <file>] [--keyword W]` scores every run of whole sentences (15–60 s by default) by measured speech-energy lift (window RMS), applause / laughter spans, keyword density and speaker-change edges, and returns the best non-overlapping ones with reasons. Not done here: applause and laughter are not detected by this Skill — they are taken from the caller (`--signals`) until step 5 adds a detector; without them the output says so in `not_measured`
  - なぜ要るか：長い講演から、単独で成り立つ見せ場を理由付きで素早く拾える
  - Done when: on a fixture, the span marked as the highlight is among the top candidates, and every candidate's boundaries fall on sentence boundaries

---
Engine for every media operation: [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) (3.x, 42 tools, contract_version 2.0; treated as complete, proposals go to Velisa's ROADMAP). Orchestrator: [video-production-agent](https://github.com/kajisho5/video-production-agent).
