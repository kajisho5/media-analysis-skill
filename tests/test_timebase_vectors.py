"""The ecosystem timebase spec's conformance vectors (velisa/timebase@1) against media_analysis.timebase (ADR-038).

tests/fixtures/timebase/timebase-vectors.json is a byte-for-byte copy of Velisa's docs/specs/timebase-vectors.json at
Velisa commit VELISA_COMMIT (its sha256 is checked). This Skill reads rates (parse_rate, nominal, same rate); every
vector of those must pass. The others (frames, timecode, formatting, joins) and the optional p / i suffix are not
offered and are counted, so a new vector cannot go unnoticed."""
import hashlib
import json
from fractions import Fraction
from pathlib import Path

from media_analysis.library import _fps_class
from media_analysis.timebase import RateError, nominal, parse_rate

VECTORS = Path(__file__).resolve().parent / "fixtures" / "timebase" / "timebase-vectors.json"
VELISA_COMMIT = "b7df1d82586f0b7c00fed4074aecac75cbadf893"
VECTORS_SHA256 = "fecd9730470fd79fb0b8dce6a55ef1ac8fd743ea044a1f1541474ebd249e5aa1"
OFFERED = 36


def _text(v):
    if isinstance(v, Fraction):
        return str(v.numerator) if v.denominator == 1 else f"{v.numerator}/{v.denominator}"
    return v


FUNCTIONS = {
    "parse_rate": lambda value: parse_rate(value),
    "nominal": lambda rate: nominal(rate),
    "same_rate": lambda a, b: parse_rate(a) == parse_rate(b),
}


def _offered(vec):
    return vec["fn"] in FUNCTIONS and not vec.get("optional")


def test_the_copy_is_velisas_file():
    raw = VECTORS.read_bytes()
    assert json.loads(raw)["spec"] == "velisa/timebase@1"
    assert hashlib.sha256(raw).hexdigest() == VECTORS_SHA256, f"re-copy the vectors from Velisa {VELISA_COMMIT}"


def test_every_offered_vector_passes():
    doc = json.loads(VECTORS.read_text(encoding="utf-8"))
    vecs = [v for v in doc["vectors"] if _offered(v)]
    failures = []
    for vec in vecs:
        try:
            got = _text(FUNCTIONS[vec["fn"]](**vec["args"]))
        except RateError as e:
            if vec.get("error") != e.code:
                failures.append((vec["id"], e.code))
            continue
        if "error" in vec or got != vec["expect"]:
            failures.append((vec["id"], got))
    assert failures == []
    assert len(vecs) == OFFERED
    assert len(doc["vectors"]) - len(vecs) == 98


def test_fps_class_names_every_ntsc_rate_from_the_exact_fraction():
    assert _fps_class(None, "48000/1001") == "47.952fps"   # was 48fps
    assert _fps_class(None, "120000/1001") == "119.88fps"   # was 120fps
    assert _fps_class(None, None, "24000/1001") == "23.976fps"
    assert _fps_class(29.97) == "29.97fps"
    assert _fps_class(59.94, "60000/1001") == "59.94fps"
    assert _fps_class(25.0, "25/1") == "25fps"
    assert _fps_class(None, "25/2") == "13fps"   # a half up, not round()'s 12
    assert _fps_class(30.0, "0/0", "30/1") == "30fps"
    assert _fps_class(None, "0/0", None) is None
