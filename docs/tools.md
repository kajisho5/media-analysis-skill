# Tools and analysis kinds

Skill id `media-analysis`, package `media-analysis-skill`, version `0.1.0`. Tool id = `media-analysis/<tool>`.
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
