"""Word counting as section 5.3 defines it: a non-whitespace run is one word, except
that each CJK character counts as one word on its own."""

from __future__ import annotations

import re

# CJK Unified Ideographs, extension A, compatibility ideographs, kana, hangul.
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]")


def count_words(text: str) -> int:
    """Count words so every gateway computes the same number for one post."""
    total = 0
    for run in text.split():
        cjk = len(_CJK.findall(run))
        rest = _CJK.sub("", run)
        total += cjk + (1 if rest else 0)
    return total
