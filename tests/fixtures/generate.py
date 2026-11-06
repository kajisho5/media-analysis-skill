"""Synthetic test fixtures, generated with ffmpeg at test time (nothing binary is committed).

Every fixture has a known construction so expected values in tests and evals are derivable from it, not guessed:

  av.mp4          6 s, 320x180 @25 fps H.264 + mono AAC 48 kHz; 1 kHz sine amplitude 0.1 only between t=2..5 s
  video_only.mp4  4 s, 320x180 @25 fps H.264, no audio stream
  stereo.wav      3 s, stereo PCM 24-bit 48 kHz (WAVE_FORMAT_EXTENSIBLE, declares the layout), 1 kHz sine amplitude 0.1 on both channels
  mono.wav        3 s, mono PCM 24-bit 44.1 kHz (declares the layout), 440 Hz sine amplitude 0.1
  silence.wav     2 s, mono PCM 48 kHz, digital silence
  multi.mp4       4 s, two video streams (320x180 @25, 160x90 @30) + two audio streams (mono 48 kHz, stereo 44.1 kHz)
  loud.wav        5 s, mono PCM 48 kHz, 1 kHz sine amplitude 0.1 continuous -> -23.0 LUFS, true peak -20.0 dBTP
  short.mp4       0.4 s (10 frames @25 fps), 320x180 H.264 + mono AAC
  scenes.mp4      6 s, three 2 s hard-cut segments (testsrc2 / smptebars / solid blue), no audio
  corrupt.mp4     av.mp4 with 3000 bytes zeroed in the middle of the mdat (decoder errors)
  noisy.wav       4 s, mono PCM 16-bit 48 kHz: 440 Hz sine amplitude 0.5 on for the first 0.5 s of every second (RMS -9.03 dBFS)
                  + white noise (anoisesrc, uniform, amplitude 0.01, seed 1: RMS 0.01/sqrt(3) = -44.77 dBFS) throughout
  cleaned.wav     noisy.wav with the noise amplitude at 0.001 (-64.77 dBFS): the same "voice", 20 dB less noise
  av_offset.mkv   3 s, 320x180 @25 H.264 + mono PCM 48 kHz whose first sample is muxed 0.5 s after the first frame (-itsoffset 0.5)
  style_a.mp4     edit with known cuts: solid-colour shots of 12, 25, 37, 50, 62 frames @25 fps (186 frames; median 37),
                  1 kHz tone amplitude 0.1 (-23.0 LUFS) for the whole 7.44 s; no subtitles
  style_b.mkv     edit with known cuts: shots of 10, 10, 20, 30, 30, 40, 40 frames @25 fps (180 frames; median 30),
                  1 kHz tone amplitude 0.2 (-17.0 LUFS) only for t < 4 s (3.2 s trailing silence), and an SRT stream of
                  4 events of 1 s each, the last one positioned at the top with {\\an8}
  portrait.mp4    2 s, 180x320 (portrait) @30 fps H.264, no audio
  talk.wav        180 s, mono PCM 8 kHz "talk": 220 Hz tone amplitude 0.05 throughout, raised to 0.2 (+12 dB) for
                  t = 90..110 s, plus white noise amplitude 0.3 for t = 110.2..113 s (the "applause")
  burned.mp4      10 s, 640x360 @25 grey (0x808080) with burned-in "subtitles" drawn as rows of white glyph-like bars
                  (6 px wide, 5 px apart, 24 px tall = 0.0667 of the height): 1-4 s bottom (y 298) on a black band,
                  5-8 s bottom with a 2 px black outline (28 px tall overall = 0.0778), 8.5-9.5 s top (y 30), plain.
                  On screen 7 of 10 s (coverage 0.7), 3 subtitles (18 per minute), 3 of 4 sampled seconds at the bottom
  clean.mp4       burned.mp4 without the subtitles (the same grey picture)
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Dict

FF = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin"]
X264 = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
TONE_GATED = "0.1*sin(2*PI*1000*t)*between(t\\,2\\,5)"
TONE = "0.1*sin(2*PI*1000*t)"
STYLE_A_SHOTS = (12, 25, 37, 50, 62)
STYLE_B_SHOTS = (10, 10, 20, 30, 30, 40, 40)
STYLE_COLOURS = ("red", "lime", "blue", "white", "black", "yellow", "magenta")
STYLE_B_SRT = ("1\n00:00:00,500 --> 00:00:01,500\nFirst line.\n\n2\n00:00:02,000 --> 00:00:03,000\nSecond line.\n\n"
               "3\n00:00:04,000 --> 00:00:05,000\nThird line.\n\n4\n00:00:06,000 --> 00:00:07,000\n{\\an8}Top line.\n\n")
BURSTS = "0.5*sin(2*PI*440*t)*lt(mod(t\\,1)\\,0.5)"


def available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(args):
    subprocess.run(FF + args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _shots_video(shots):
    """Hard cuts between solid colours, each shot exactly the given number of frames at 25 fps."""
    inputs, chains = [], []
    for i, frames in enumerate(shots):
        inputs += ["-f", "lavfi", "-i", f"color=c={STYLE_COLOURS[i % len(STYLE_COLOURS)]}:size=320x180:rate=25"]
        chains.append(f"[{i}:v]trim=end_frame={frames},setpts=PTS-STARTPTS[v{i}]")
    concat = "".join(f"[v{i}]" for i in range(len(shots))) + f"concat=n={len(shots)}:v=1:a=0[v]"
    return inputs, ";".join(chains) + ";" + concat


def _glyph_bars(x0, y, n, enable, outline=False):
    """A row of n white bars standing in for one line of text, shown while `enable` is true."""
    out = []
    for i in range(n):
        x = x0 + i * 11
        if outline:
            out.append(f"drawbox=x={x - 2}:y={y - 2}:w=10:h=28:color=black:t=fill:enable='{enable}'")
        out.append(f"drawbox=x={x}:y={y}:w=6:h=24:color=white:t=fill:enable='{enable}'")
    return out


BURNED_IN = ",".join(["drawbox=x=150:y=286:w=340:h=48:color=black:t=fill:enable='between(t,1,4)'", *_glyph_bars(170, 298, 28, "between(t,1,4)"),
                      *_glyph_bars(220, 298, 18, "between(t,5,8)", outline=True), *_glyph_bars(250, 30, 12, "between(t,8.5,9.5)")])


def build_all(directory: Path) -> Dict[str, Path]:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    f: Dict[str, Path] = {k: d / v for k, v in {
        "av": "av.mp4", "video_only": "video_only.mp4", "stereo": "stereo.wav", "mono": "mono.wav", "silence": "silence.wav",
        "multi": "multi.mp4", "loud": "loud.wav", "short": "short.mp4", "scenes": "scenes.mp4", "corrupt": "corrupt.mp4",
        "noisy": "noisy.wav", "cleaned": "cleaned.wav", "av_offset": "av_offset.mkv",
        "style_a": "style_a.mp4", "style_b": "style_b.mkv", "portrait": "portrait.mp4", "talk": "talk.wav",
        "burned": "burned.mp4", "clean": "clean.mp4"}.items()}
    _run(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25", "-f", "lavfi", "-i", f"aevalsrc='{TONE_GATED}':s=48000", "-t", "6",
          *X264, "-c:a", "aac", "-shortest", str(f["av"])])
    _run(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25", "-t", "4", *X264, str(f["video_only"])])
    _run(["-f", "lavfi", "-i", f"aevalsrc='{TONE}|{TONE}':s=48000:c=stereo", "-t", "3", "-c:a", "pcm_s24le", str(f["stereo"])])
    _run(["-f", "lavfi", "-i", "aevalsrc='0.1*sin(2*PI*440*t)':s=44100:c=mono", "-t", "3", "-c:a", "pcm_s24le", str(f["mono"])])
    _run(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "2", "-c:a", "pcm_s16le", str(f["silence"])])
    _run(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25", "-f", "lavfi", "-i", "smptebars=size=160x90:rate=30",
          "-f", "lavfi", "-i", f"aevalsrc='{TONE}':s=48000", "-f", "lavfi", "-i", f"aevalsrc='{TONE}|{TONE}':s=44100:c=stereo",
          "-map", "0:v", "-map", "1:v", "-map", "2:a", "-map", "3:a", "-t", "4", *X264, "-c:a", "aac",
          "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=jpn", str(f["multi"])])
    _run(["-f", "lavfi", "-i", f"aevalsrc='{TONE}':s=48000", "-t", "5", "-c:a", "pcm_s16le", str(f["loud"])])
    _run(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25", "-f", "lavfi", "-i", f"aevalsrc='{TONE}':s=48000", "-t", "0.4",
          *X264, "-c:a", "aac", "-shortest", str(f["short"])])
    _run(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:d=2", "-f", "lavfi", "-i", "smptebars=size=320x180:rate=25:d=2",
          "-f", "lavfi", "-i", "color=c=blue:size=320x180:rate=25:d=2", "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
          "-map", "[v]", *X264, str(f["scenes"])])
    for key, noise_amp in (("noisy", "0.01"), ("cleaned", "0.001")):
        _run(["-f", "lavfi", "-i", f"aevalsrc='{BURSTS}':s=48000", "-f", "lavfi", "-i", f"anoisesrc=r=48000:a={noise_amp}:c=white:seed=1",
              "-filter_complex", "[0:a][1:a]amix=inputs=2:normalize=0", "-t", "4", "-c:a", "pcm_s16le", str(f[key])])
    _run(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25", "-itsoffset", "0.5", "-f", "lavfi", "-i", f"aevalsrc='{TONE}':s=48000",
          "-t", "3", *X264, "-c:a", "pcm_s16le", str(f["av_offset"])])
    inputs, graph = _shots_video(STYLE_A_SHOTS)
    _run([*inputs, "-f", "lavfi", "-i", f"aevalsrc='{TONE}':s=48000", "-filter_complex", graph, "-map", "[v]",
          "-map", f"{len(STYLE_A_SHOTS)}:a", "-t", "7.44", *X264, "-c:a", "aac", str(f["style_a"])])
    srt = d / "style_b.srt"
    srt.write_text(STYLE_B_SRT, encoding="utf-8")
    inputs, graph = _shots_video(STYLE_B_SHOTS)
    n = len(STYLE_B_SHOTS)
    _run([*inputs, "-f", "lavfi", "-i", "aevalsrc='0.2*sin(2*PI*1000*t)*lt(t\\,4)':s=48000", "-i", str(srt), "-filter_complex", graph,
          "-map", "[v]", "-map", f"{n}:a", "-map", f"{n + 1}:s", "-t", "7.2", *X264, "-c:a", "pcm_s16le", "-c:s", "srt", str(f["style_b"])])
    _run(["-f", "lavfi", "-i", "testsrc2=size=180x320:rate=30", "-t", "2", *X264, str(f["portrait"])])
    _run(["-f", "lavfi", "-i", "aevalsrc='0.05*sin(2*PI*220*t)*(1+3*between(t\\,90\\,110))':s=8000",
          "-f", "lavfi", "-i", "anoisesrc=r=8000:a=0.3:c=white:seed=2", "-filter_complex",
          "[1:a]volume=0:enable='not(between(t\\,110.2\\,113))'[n];[0:a][n]amix=inputs=2:normalize=0", "-t", "180",
          "-c:a", "pcm_s16le", str(f["talk"])])
    for key, extra in (("burned", ["-vf", BURNED_IN]), ("clean", [])):
        _run(["-f", "lavfi", "-i", "color=c=0x808080:size=640x360:rate=25", *extra, "-t", "10", *X264, str(f[key])])
    data = bytearray(f["av"].read_bytes())
    mid = len(data) // 2
    data[mid:mid + 3000] = b"\x00" * 3000
    f["corrupt"].write_bytes(bytes(data))
    return f
