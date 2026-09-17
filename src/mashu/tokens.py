"""Estimate the token cost of pushed content."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable

_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿ｦ-ﾟ]")

#: Extra token cost per pushed row for its id and framing.
ROW_OVERHEAD = 8


def estimate_tokens(text: str) -> int:
    """Roughly how many tokens a string costs."""
    cjk = len(_CJK.findall(text))
    return math.ceil(cjk + (len(text) - cjk) / 4)


def pushed_cost(contents: Iterable[str]) -> int:
    """What a set of rows costs on the wire, content plus framing."""
    return sum(estimate_tokens(c) + ROW_OVERHEAD for c in contents)
