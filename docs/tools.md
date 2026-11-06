# Tools and analysis kinds

Skill id `media-analysis`, package `media-analysis-skill`; version: the package version (`media-analysis --version`,
`contract --json` → `version`; single-sourced from `pyproject.toml`). Tool id = `media-analysis/<tool>`; each tool also
publishes `algorithm_version`, the measurement version that keys its cache (README "Versioning", ADR-028).
Only implemented tools are declared (`media-analysis contract --json`); the registry refuses to start if the code
and the contract disagree.

| tool id | kinds | required capabilities | executes |
|---|---|---|---|
| `media-analysis/probe` | `media_probe` | ffprobe | ffprobe `-show_entries format,stream` |
| `media-analysis/streams` | `stream_layout` | ffprobe | same probe |
| `media-analysis/video` | `video_format` | ffprobe | probe + ffprobe `-show_packets` of the selected video stream |
| `media-analysis/audio` | `audio_format` | ffprobe | probe |
| `media-analysis/timing` | `duration`, `timing` | ffprobe | probe (+ `-show_packets` of all streams for `timing`) |
| `media-analysis/silence` | `silence` | ffprobe, ffmpeg, filter:silencedetect | probe + ffmpeg `-af silencedetect -f null -` |
| `media-analysis/loudness` | `loudness` | ffprobe, ffmpeg, filter:ebur128 | probe + ffmpeg `-af ebur128=peak=true -f null -` |
| `media-analysis/integrity` | `integrity` | ffprobe, ffmpeg | probe + ffmpeg full decode `-f null -` + `-show_packets` |
| `media-analysis/scenes` | `scene_detection` | ffprobe, ffmpeg, filter:scdet | probe + ffmpeg `-vf scdet,metadata=print -f null -` |
| `media-analysis/noise` | `noise_floor` | ffprobe, ffmpeg, filter:astats | probe + ffmpeg `-af [pan,]asetnsamples,astats=reset=1,ametadata=print -f null -` |
| `media-analysis/style` | `edit_style` | ffprobe, ffmpeg, filter:scdet, filter:ebur128, filter:silencedetect | probe + ffmpeg `-vf scdet,metadata=print -f null -` + each text subtitle stream `-c:s ass -f ass -` (stdout) + `-af ebur128,silencedetect -f null -` |

Every analyzer runs ffprobe at most once per request (shared context). Stream ordinals (`stream` parameter) follow
ffmpeg's `0:v:N` / `0:a:N` numbering; attached pictures (cover art) are not video streams.

## `media_probe` → `data`

`container{format, format_long_name, duration, size, bitrate, start_time, stream_count, probe_score}`,
`video` (first video stream or null: `index, codec, profile, width, height, fps, pixel_format, color_space,
color_transfer, color_primaries, color_range, bitrate, rotation`), `audio` (first audio stream or null: `index, codec,
profile, sample_rate, channels, channel_layout, sample_format, bitrate`), `stream_counts{type: n}`,
`video_stream_count`, `audio_stream_count`. Container duration falls back to the longest stream duration when the
container declares none.

## `stream_layout`

`stream_count`, `counts{type: n}`, `streams[]` each with `index, type, type_ordinal, codec, profile, language, title,
disposition[], start_time, duration, bitrate, nb_frames` plus video (`width, height, fps, pixel_format, attached_pic`)
or audio (`sample_rate, channels, channel_layout, sample_format`) fields. Two video and two audio streams are
reported as `(0,video,0) (1,video,1) (2,audio,0) (3,audio,1)`: index is the container index, `type_ordinal` is the
per-type ordinal.

## `video_format` (parameter `stream`)

`stream_index, stream_ordinal, codec, profile, width, height, coded_width, coded_height, sample_aspect_ratio,
display_aspect_ratio, fps, r_frame_rate, avg_frame_rate, frame_rate_mode, frame_rate_mode_basis, measured_fps,
frame_count, frame_count_basis, duration, start_time, pixel_format, bits_per_raw_sample, color_space, color_transfer,
color_primaries, color_range, field_order, rotation, bitrate`.

- `frame_rate_mode` is `constant` only when every measured presentation interval equals the median within 1.5 ms,
  `variable` when any deviates, `unknown` with fewer than 3 timestamped packets. The basis string says which.
  `r_frame_rate != avg_frame_rate` alone is never used to claim VFR.
- `frame_count_basis` is `nb_frames` (declared by the container) or `packet_count` (counted) or null.

## `audio_format` (parameter `stream`)

`stream_index, stream_ordinal, codec, profile, sample_rate, channels, channel_layout, sample_format, bits_per_sample,
bitrate, duration, start_time, language, nb_frames`. `channel_layout` is null when the container does not declare
one (e.g. 16-bit WAV); it is never guessed from the channel count.

## `duration` / `timing`

`duration`: `container_duration, container_start_time, streams[{index, type, start_time, duration}],
longest_stream_duration, shortest_stream_duration`.
`timing` adds `gap_factor`, `av_mismatch{status, video_index, audio_index, video_duration, audio_duration,
difference, tolerance, within_tolerance}` (first video vs first audio stream; `not_performed` without both),
`av_start_offset{status, video_index, audio_index, video_start_time, audio_start_time, offset, tolerance,
within_tolerance}` (audio `start_time` − video `start_time`: positive = the first audio sample is presented after the
first frame; parameter `av_start_offset_tolerance`, default 0.045 s, ITU-R BT.1359-1's 45 ms detectability bound;
ADR-029 — a muxing offset, not lip sync),
`packet_timestamps{<index>: {packet_count, first_pts, last_pts, first_dts, missing_pts, missing_dts,
non_monotonic_dts, negative_pts, duplicate_pts, median_interval, min_interval, max_interval, gaps[], gap_count,
keyframes}}`, `anomalies[{stream_index, type, count}]` (types `non_monotonic_dts`, `timestamp_gap`,
`duplicate_pts`), `anomaly_count`. A leading negative pts (AAC priming / edit list) is reported in
`negative_pts` but is not an anomaly.

## `silence` (parameters `stream, threshold_db, min_duration, edge_tolerance`)

`stream_index, stream_ordinal, duration, threshold_db, min_duration, edge_tolerance, segments[{start, end, duration,
type, runs_to_end}], segment_count, silent_seconds, leading, trailing, entirely_silent`. `type` is `leading`
(starts within `edge_tolerance` of 0), `trailing` (ends within `edge_tolerance` of the stream duration or runs to the
end), `entire` (both) or `internal`. Values are what `silencedetect` reports; a trailing segment can end slightly
after the declared duration because of codec padding.

## `loudness` (parameter `stream`)

`integrated_lufs, integrated_threshold_lufs, loudness_range_lu, lra_low_lufs, lra_high_lufs, true_peak_dbtp,
unmeasurable[], integrated_below_absolute_gate, stream_index, stream_ordinal, standard, measurement`. ffmpeg reports
`-inf` / `nan` for silent input: those fields become null and are listed in `unmeasurable`; an integrated value of
-70 LUFS is the BS.1770 absolute gate floor and is flagged by `integrated_below_absolute_gate`. Loudness is measured,
never normalised (ffmpeg-skill/loudness does that).

## `integrity` (parameter `max_error_lines`)

`status` (PASS / WARN / FAIL), `reasons[]`, `checks{decode, frames, timestamps}`.

- `decode`: full decode of all streams; `exit_code`, `errors{error_line_count, categories{missing_reference,
  corrupt_data, timestamp, packet_submit, other}, samples[]}` (pointer addresses stripped, ≤ `max_error_lines` samples).
  Any error line or a non-zero exit → FAIL.
- `frames`: `expected_video_frames` (container `nb_frames` of the first video stream), `decoded_video_frames`,
  `decoded_time`; fewer decoded than declared → WARN; `not_performed` without a video stream or without `nb_frames`.
- `timestamps`: per-stream packet report (as in `timing`); non-monotonic DTS, gaps or missing timestamps → WARN.

A check that was not performed never contributes PASS.

## `scene_detection` (parameters `stream, threshold, min_scene_duration`)

`method` ("ffmpeg scdet score"), `threshold`, `min_scene_duration`, `cuts[{frame, time, score}]`, `cut_count`,
`scenes[{index, start, end, duration, representative_time, cut_score}]`, `scene_count`. A cut is a frame whose scdet
score is above `threshold`; cuts closer than `min_scene_duration` to the previous kept cut are dropped. Scenes are
the intervals between kept cuts; `representative_time` is the scene start. This is picture-change detection, not
semantic scene understanding.

## `noise_floor` (parameters `stream, channel, window, noise_percentile, signal_percentile`)

`stream_index, stream_ordinal, channel, channels, sample_rate, window, window_samples, window_count,
digital_silence_windows, noise_floor_percentile, signal_percentile, noise_floor_dbfs, signal_level_dbfs, snr_db,
percentiles_dbfs{p5, p10, p25, p50, p75, p90, p95}, min_window_rms_dbfs, max_window_rms_dbfs, unmeasurable[],
method, measurement` (ADR-029).

- The stream (all channels pooled, or one `channel` via `pan`) is cut into consecutive non-overlapping windows of
  `window` seconds (`window_samples` = round(sample rate × window); the last window may be shorter) and `astats`
  reports each window's RMS level in dBFS.
- `noise_floor_dbfs` is the `noise_percentile` nearest-rank percentile of the window levels, `signal_level_dbfs` the
  `signal_percentile` one, `snr_db` = signal − noise. This is a no-reference estimate: it assumes the recording has
  pauses (at least `noise_percentile` % of windows without the wanted signal). On continuous signal the floor is the
  signal itself and `snr_db` is near 0 — a fact about the input, not an error.
- Digital silence is `-inf` dBFS: a percentile landing on it is `null` and listed in `unmeasurable` (with `snr_db`).
- Before / after comparisons (a denoiser's acceptance) should use the same parameters on both files.

## `edit_style` (parameters `video_stream, audio_stream, threshold, min_shot_duration, silence_threshold_db, silence_min_duration`)

`profile_schema` (`media-analysis/edit-style@1`), `duration`, `video{...}`, `subtitles{...}`, `audio{...}`,
`unmeasured[{item, reason}]` (ADR-030).

- `video`: `status` (`measured` / `not_performed` with `reason` when the stream is missing or has no frame rate),
  `stream_index, stream_ordinal, fps, frame_count` (frames scdet saw), `duration`, `threshold, min_shot_duration,
  min_shot_frames`, `cut_count, cuts_per_minute, shot_count`, `shot_frames` and `shot_seconds` (`min, p10, p25,
  median, p75, p90, max, mean`: the median averages the two middle shots, the other percentiles are nearest-rank),
  `shot_length_histogram[{from_seconds, to_seconds, count}]` (edges 0, 0.5, 1, 2, 4, 8, 16 s, the last open),
  `shots[{index, start_frame, frames, start, end, seconds}]`. A cut is the frame scdet flags; a cut closer than
  `min_shot_duration` (rounded to frames) to the previous kept cut is dropped. Shot lengths are frame counts, so the
  measurement is exact to the frame on hard cuts.
- `subtitles`: `soft_stream_count`, `present` (a text subtitle stream with at least one event), `streams[{stream_index,
  stream_ordinal, codec, language, status, events, events_per_minute, coverage_share, event_seconds, positions{bottom,
  middle, top}, dominant_position}]`, and the first measured stream's `events_per_minute, coverage_share,
  dominant_position` (`summary_stream_index`). Text streams (SubRip, ASS / SSA, mov_text, WebVTT) are read as ASS;
  the position of an event is its style's `Alignment`, overridden by `\an` / `\a` and by `\pos` / `\move` (y / PlayResY:
  top third, middle, bottom third). Bitmap streams (PGS, DVD) are `not_performed`. `burned_in` is always `not_measured`.
- `audio`: `status, stream_index, stream_ordinal, duration, integrated_lufs, loudness_range_lu, true_peak_dbtp,
  integrated_below_absolute_gate, silence_threshold_db, silence_min_duration, silence_share, active_share,
  longest_silence, silence_count`, `music_bed` (always `not_measured`).
- `media-analysis style diff <reference> <candidate> --json` → `{schema: media-analysis/edit-style-diff@1, direction:
  "candidate - reference", compared, different[], items[{metric, status, reference, candidate, difference, relative,
  equal}]}`; a metric missing on either side is `status: unavailable` with `missing_in`, never 0. Inputs may be a
  saved profile Observation (`style profile --out`), its `data`, or a response document with one `edit_style`
  Observation. The diff states differences; it does not say which edit is better.

## `library index` / `library search` (CLI workflow, ADR-031)

Not an analysis kind: it reads a folder and writes one index file. Every measurement goes through the engine
(`media_probe`; `scene_detection` when there is video; `loudness` when there is audio), with the folder as the only
allowed input root and the index confined to the workspace.

- `library index <folder> --index <file> [--tags <file>] [--cache-dir DIR] [--timeout S] [--workspace DIR] --json` →
  `{schema: media-analysis/library-scan@1, index, root, clip_count, report{analysed[], reused[], removed[],
  failed[{path, code, message}], external_tags_dropped[]}, analyzer_executions}`.
- Index (`media-analysis/library-index@1`): `root, updated_at, skill_version, clip_count, clips[], external_tags{path:
  {tags, source, fingerprint}}, failed[]`. A clip: `path` (relative, `/`), `fingerprint` (sha256), `file_size`,
  `mtime_ns`, `duration`, `container`, `size`, `video{codec, width, height, fps, orientation, resolution_class,
  fps_class}` or null, `shots{count, cut_count, longest_shot_seconds}` or null, `representative_time` (middle of the
  longest shot; a time, no image is written), `scene_type` (`single_shot` / `multi_shot` / `audio_only`),
  `audio{codec, channels, sample_rate, integrated_lufs, silent}` or null, `measured_tags[]`, `external_tags[]`,
  `tags[]` (both), `name_words[]` (words of the relative path), `analysed_with{kinds, skill_version}`.
- Measured tags, by fixed rules: orientation (`landscape` / `portrait` / `square`, rotation applied), resolution class
  of the short side (`sd` < 720 ≤ `hd` < 1080 ≤ `fhd` < 1440 ≤ `qhd` < 2160 ≤ `uhd`), frame-rate class (`25fps`,
  `29.97fps`, ...), video codec, `single-shot` / `multi-shot` / `audio-only`, `has-audio` / `no-audio` /
  `silent-audio` (integrated loudness below the −70 LUFS gate), duration class (`short` < 5 s ≤ `medium` < 30 s ≤
  `long`), file extension.
- `--tags`: `{"<relative path>": ["tag", ...]}` or `[{"path", "tags", "source"}]`; tags are lower-cased and trimmed.
  A path not in the folder is `INVALID_INPUT`. Stored with the fingerprint of the file they were given for; when the
  file changes they are dropped and listed in `external_tags_dropped`.
- Re-scan: a file whose size and modification time equal the index entry is carried over without being opened or
  hashed; anything else is measured again; files no longer present are removed. Hidden files and folders are skipped;
  only media extensions are scanned.
- `library search --index <file> [--query W]... [--tag T]... [--min-duration S] [--max-duration S] [--orientation O]
  [--min-height PX] [--has-audio | --no-audio] [--scene-type T] [--limit N] --json` →
  `{schema: media-analysis/library-search@1, root, result_count, results[{path, score, matched[{word, tags,
  name_words}], duration, representative_time, tags}], conditions}`. Every condition must hold; every query word must
  prefix-match a tag or a path word. `score` = matched words + exact tag matches; ties by path.
