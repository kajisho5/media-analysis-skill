import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

from fixtures.generate import available, build_all  # noqa: E402


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    if not available():
        pytest.fail("ffmpeg / ffprobe are required for integration tests and evals (install FFmpeg); they are not skipped")
    return build_all(tmp_path_factory.mktemp("fixtures"))


@pytest.fixture
def workspace(tmp_path):
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(old)


def talk_transcript():
    """Transcript of fixtures talk.wav (tests/fixtures/generate.py): 18 segments of 10 s, two 5 s sentences each, with
    word times; the four sentences in 90..110 s carry the keyword; speaker A until 150 s, then B. The highlight the
    fixture was built around is 90..110 s (louder, keywords, applause right after it)."""
    segs = []
    for k in range(18):
        start = k * 10.0
        words = []
        for half in range(2):
            n = 2 * k + half
            text = f"The breakthrough result number {n} works!" if 90 <= start + 5 * half < 110 else f"Sentence {n} covers the plan."
            toks = text.split()
            t0 = start + 5 * half
            step = 4.5 / len(toks)
            words += [{"start": round(t0 + i * step, 3), "end": round(t0 + (i + 1) * step, 3), "text": w} for i, w in enumerate(toks)]
        segs.append({"start": start, "end": start + 9.5, "text": " ".join(w["text"] for w in words), "words": words,
                     "speaker_id": "A" if start < 150 else "B"})
    return {"segments": segs}
