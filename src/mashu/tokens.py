"""Estimating what a piece of content costs to push.

An estimate, deliberately: the real count depends on the tokenizer of
whichever agent receives the context, and the seat count only has to keep the
bootstrap pushable. CJK characters run close to one token each; other scripts
run nearer four characters to a token.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable

_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿ｦ-ﾟ]")

#: What one pushed row costs beyond its content: the id and the framing that
#: carries it. v1 measured its opening at twice the counted content because
#: the scaffolding went uncounted; this constant is the correction, kept
#: deliberately simple.
ROW_OVERHEAD = 8


def estimate_tokens(text: str) -> int:
    """Roughly how many tokens a string costs."""
    cjk = len(_CJK.findall(text))
    return math.ceil(cjk + (len(text) - cjk) / 4)


def pushed_cost(contents: Iterable[str]) -> int:
    """What a set of rows costs on the wire, content plus framing."""
    return sum(estimate_tokens(c) + ROW_OVERHEAD for c in contents)
