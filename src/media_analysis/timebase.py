"""Frame rates as exact fractions, read by the ecosystem timebase spec velisa/timebase@1 (Velisa ADR-026, ADR-038).

Only what this Skill uses: reading a rate (rule TB-2) and its nominal integer rate. Standard library only."""
from __future__ import annotations

import math
import re
from fractions import Fraction
from typing import Any

NTSC_BASES = (24, 30, 48, 60, 120, 240)   # a decimal within NTSC_TOLERANCE of base * 1000/1001 is that NTSC rate
NTSC_TOLERANCE = Fraction(5, 1000)
MAX_RATE = Fraction(1000)
_DECIMAL = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_RATIONAL = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")


class RateError(ValueError):
    """A value that is not a frame rate (the spec's INVALID_RATE)."""

    code = "INVALID_RATE"


def ntsc_near(rate: Fraction) -> Fraction:
    """The NTSC rate base * 1000/1001 within 0.005 of `rate`, else `rate` itself."""
    for base in NTSC_BASES:
        ntsc = Fraction(base * 1000, 1001)
        if abs(rate - ntsc) <= NTSC_TOLERANCE:
            return ntsc
    return rate


def parse_rate(value: Any) -> Fraction:
    """A frame rate as an exact fraction (TB-2): 'N/D' (or a Fraction) as written; an integer as N/1; a decimal that
    is not an integer and lies within 0.005 of an NTSC rate as that rate (29.97 is 30000/1001, never 2997/100); any
    other decimal as its exact value. A float is read through its shortest text (repr). In (0, 1000]."""
    if isinstance(value, bool):
        raise RateError(f"frame rate {value!r} is not a number")
    if isinstance(value, Fraction):
        rate = value
    elif isinstance(value, int):
        rate = Fraction(value)
    else:
        if isinstance(value, float):
            if not math.isfinite(value):
                raise RateError(f"frame rate {value!r} is not finite")
            text = repr(value)
        elif isinstance(value, str):
            text = value.strip()
        else:
            raise RateError(f"frame rate {value!r} is not a number")
        m = _RATIONAL.match(text)
        if m:
            if int(m.group(2)) == 0:
                raise RateError(f"frame rate {value!r} has a zero denominator")
            rate = Fraction(int(m.group(1)), int(m.group(2)))
        elif _DECIMAL.match(text):
            rate = Fraction(text)
            if rate.denominator != 1:
                rate = ntsc_near(rate)
        else:
            raise RateError(f"frame rate {value!r} is not N/D, an integer or a decimal")
    if rate <= 0 or rate > MAX_RATE:
        raise RateError(f"frame rate {value!r} is out of range (0, 1000]")
    return rate


def nominal(rate: Any) -> int:
    """Timecode frames per second: the rate rounded half up (24 for 23.976, 30 for 29.97)."""
    return max(1, math.floor(parse_rate(rate) + Fraction(1, 2)))
