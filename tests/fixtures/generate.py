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


def available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(args):
    subprocess.run(FF + args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def build_all(directory: Path) -> Dict[str, Path]:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    f: Dict[str, Path] = {k: d / v for k, v in {
        "av": "av.mp4", "video_only": "video_only.mp4", "stereo": "stereo.wav", "mono": "mono.wav", "silence": "silence.wav",
        "multi": "multi.mp4", "loud": "loud.wav", "short": "short.mp4", "scenes": "scenes.mp4", "corrupt": "corrupt.mp4"}.items()}
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
    data = bytearray(f["av"].read_bytes())
    mid = len(data) // 2
    data[mid:mid + 3000] = b"\x00" * 3000
    f["corrupt"].write_bytes(bytes(data))
    return f
