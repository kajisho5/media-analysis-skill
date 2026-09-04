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
