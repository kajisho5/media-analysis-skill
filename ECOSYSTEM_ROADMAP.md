# Roadmap — media-analysis-skill

Part of the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS/blob/main/docs/ecosystem/ROADMAP.md) ecosystem: department **検証 (Verification)**, status `active`.
Written from a read-only audit of this repository on 2026-10-30. Each step below is meant to be one pull request; tick it off by linking the PR.

## Where it stands

Version single-sourced from `pyproject.toml` (#17): `contract --json` → `version` equals the newest tag (0.1.16 at `v0.1.16`, checked 2026-11-12). Cache keyed by per-analyzer algorithm version, so releases no longer empty it (ADR-028). 15 measurement kinds (step 3 added `noise_floor`, step 7 `edit_style`, step 4 `speech_regions`, step 5 `audio_events` and `highlight_signals`). 164 tests collected, CI on 3 OSes. STATE.md current (#18).

## What this repository owns

`ecosystem.json` gives each job to exactly one repository. Work outside this list belongs to its owner and is handed over, not re-implemented.

| Owned job | Status | Note |
|---|---|---|
| メディア計測 | done |  |
| 発話区間 | done | `speech_regions` (step 4); transcription-skill takes it as an input; added by the [Velisa ownership decisions 2026-11-12](https://github.com/kajisho5/Velisa/blob/main/docs/ecosystem/DECISION_LOG.md) |

## Wiring

The agent pins this contract at 0.1.16 (first pinned at 0.1.7 in video-production-agent#93, ADR-078), `noise_floor` included. Calls ffmpeg/ffprobe directly (overlap with ffmpeg-skill probe/silence/loudness/scenes is intentional).

## Steps

- [x] **1. Single-source the version** — coordinated with the agent's version gate — [#17](https://github.com/kajisho5/media-analysis-skill/pull/17)
  - Done when: contract version == tag
- [x] **2. Refresh STATE.md** — [#18](https://github.com/kajisho5/media-analysis-skill/pull/18)
- [x] **3. Measurements new skills need for acceptance** — noise floor / SNR (voice-enhance), A/V offset -- only if not qc-skill's — new kind `noise_floor` (`measure.audio.noise_floor`) and `timing.av_start_offset`; selection, sources and what stays open (reverberation, sharpness / exposure) in ADR-029 — [#21](https://github.com/kajisho5/media-analysis-skill/pull/21)
  - Done when: evals
- [x] **4. Speech regions (VAD)** — new kind `speech_regions` (`measure.audio.speech_regions`): where someone speaks, as time ranges with a probability, the speech share and the longest pause, measured by silero-vad (MIT code and weights, ONNX on CPU) as the optional extra `vad`; without it the kind is `UNKNOWN`, never guessed from silence. Different from ffmpeg-skill `silence` / `scenes --speech` (level and spectrum, not a speech model); the ADR compares the three on the same fixtures. Consumers: the agent's edit view (video-production-agent step 14), cut boundaries that must not land mid-word, dubbing gaps. Done (ADR-035): tool `media-analysis/vad`, model `silero_vad_16k_op15.onnx` shipped as package data (no download), onnxruntime + numpy as extra `vad`, `UNKNOWN` (not cached) without it; F1 0.943 over music, 0.949 over noise, 0 s of speech on music alone; ffmpeg-skill `silence` (default) scores 0.61 on both and calls all of the music speech, `scenes --speech` 0.77 / 0.00 — [#30](https://github.com/kajisho5/media-analysis-skill/pull/30)
  - Done when: ADR; on speech over music and over noise, speech-region F1 above 0.9 against hand labels; offline, nothing downloaded at run time
- [x] **5. Highlight signals for clips** — new kinds measured, never judged: applause and laughter spans (an audio classifier, engine and weights licence checked in an ADR; YAMNet, Apache-2.0, is a candidate), speech-energy peaks, speaking-rate changes, and screen-change bursts, each as time ranges with a score. Field review 2026-11-05 (Velisa ROADMAP, third round). Done (ADR-036): kind `audio_events` (`measure.audio.events`) — applause and laughter spans with a score from YAMNet (Apache-2.0 code and weights; converted to ONNX by `tools/export_yamnet_onnx.py`, float16, shipped as package data with the licence and change notice, nothing downloaded), optional extra `events`, `UNKNOWN` (not cached) without it; kind `highlight_signals` (`measure.media.highlight_signals`) — speech-energy peaks over the median speech-band level, speaking-rate changes from a syllable-nucleus rate, screen-change bursts from scdet, no model; `highlights --signals` takes the `audio_events` observation as it is. On a fixture of a CC0 laugh (3.0–8.01 s) and CC0 applause (11.0–16.51 s): laughter 3.05–7.95 s, applause 11.05–16.25 s; no span on speech, music, tones or noise. Laughter weakens below about 25 dB SNR (ADR-036) — [#32](https://github.com/kajisho5/media-analysis-skill/pull/32)
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
- [x] **9. Highlight candidates from long recordings** — from a long video (a talk, a lecture), list self-contained spans as candidates with reasons, using loudness peaks, applause, laughter (spans from step 5 once it lands, or passed in by the caller), speaker changes and transcript keywords, with each start / end on a sentence boundary. Lists candidates only; cutting stays with the editing tools. Done (ADR-032): `media-analysis highlights <input> --transcript <file> [--signals <file>] [--keyword W]` scores every run of whole sentences (15–60 s by default) by measured speech-energy lift (window RMS), applause / laughter spans, keyword density and speaker-change edges, and returns the best non-overlapping ones with reasons. Not done here: applause and laughter are not detected by this Skill — they are taken from the caller (`--signals`), which since step 5 can be this Skill's own `audio_events` observation; without them the output says so in `not_measured` — [#26](https://github.com/kajisho5/media-analysis-skill/pull/26)
  - なぜ要るか：長い講演から、単独で成り立つ見せ場を理由付きで素早く拾える
  - Done when: on a fixture, the span marked as the highlight is among the top candidates, and every candidate's boundaries fall on sentence boundaries

- [x] **10. Burned-in subtitle measurement** — measure `edit_style`'s `subtitles.burned_in` (until now `not_measured`): sample frames at a fixed interval, find the text areas in the picture, and report the share of the running time with subtitles on screen, subtitle switches per minute, position (top / middle / bottom and left / right), text height relative to the frame, text areas / approximate characters per frame, and a band or outline behind the text where it can be told; `style diff` compares them. Done (ADR-033): `edit_style` (algorithm version 2, parameter `burned_in_interval`, default 1 s, 0 = off) finds text-like lines from an ffmpeg `edgedetect` edge map and measures all of these on its own (no OCR, no model); `diff` compares seven numbers and two categories. qc-skill's `video.text_regions` was considered first, but it measures contrast and ink height inside boxes the caller already knows — it does not find where text is — so it is not called; pairing the boxes found here with it for a contrast figure is left as a proposal. Limit: any text in the picture counts, and very detailed pictures can look like text — [#27](https://github.com/kajisho5/media-analysis-skill/pull/27)
  - なぜ要るか：お手本の焼き込み字幕の出し方（どれだけ・どこに・どの大きさで・帯や縁取り）を数値で掴み、自分の編集と比べられる
  - Done when: on synthetic fixtures with and without burned-in subtitles, coverage, position and text height match the known values within tolerance, and the diff shows the difference
- [ ] **11. (agent side) speech regions planned for long inputs** — the agent adds SkillSpec `speech_region_analysis` and plans `speech_regions` → `transcribe(speech_regions=...)` ([Velisa ownership decisions 2026-11-12](https://github.com/kajisho5/Velisa/blob/main/docs/ecosystem/DECISION_LOG.md))
  - Done when: tracked in video-production-agent (step 33)

## Boundaries

Cut detection is layered, not a collision ([Velisa ownership decisions 2026-11-12](https://github.com/kajisho5/Velisa/blob/main/docs/ecosystem/DECISION_LOG.md)): ffmpeg-skill `scenes` is the fast list, this repository's `scene_detection` is the observation, detection-skill `shots` is the frame-accurate shot table, qc-skill only judges, and slide-sync-skill handles slides. Speech regions (`発話区間`) are measured here; transcription-skill receives them.

---
Engine for every media operation: [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) (3.x, 42 tools, contract_version 2.0; treated as complete, proposals go to Velisa's ROADMAP). Orchestrator: [video-production-agent](https://github.com/kajisho5/video-production-agent).
