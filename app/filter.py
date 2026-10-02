"""キーワード事前絞り込みと引用照合（SPEC 0.3 第 4.1・4.3 章）。語彙は仮。"""

from __future__ import annotations

import re

from app.config import CONTRADICTION_KEYWORDS, QUOTE_CHAR_LIMIT, QUOTE_WORD_LIMIT
from app.textutil import nfc


def fold_ws(text: str) -> str:
    """仮 Q33: NFC のうえ連続空白・改行・タブを 1 空白に潰す。"""
    return re.sub(r"\s+", " ", nfc(text or "")).strip()


def haystack(title: str, body: str) -> str:
    return fold_ws(f"{title or ''} {body or ''}").lower()


def looks_like_contradiction(
    title: str, body: str, keywords: tuple[str, ...] | None = None
) -> bool:
    text = haystack(title, body)
    if not text:
        return False
    words = keywords if keywords is not None else CONTRADICTION_KEYWORDS
    for raw in words:
        needle = fold_ws(raw).lower()
        if needle and needle in text:
            return True
    return False


def normalize_for_quote(text: str) -> str:
    return fold_ws(text).lower()


def quote_in_body(quote: str, body: str) -> bool:
    q = normalize_for_quote(quote)
    b = normalize_for_quote(body)
    if not q:
        return False
    return q in b


def quote_within_limit(
    quote: str,
    char_limit: int = QUOTE_CHAR_LIMIT,
    word_limit: int = QUOTE_WORD_LIMIT,
) -> bool:
    """原文断片の上限(SPEC Q7・仮)。日本語など非ASCIIを含む断片は字数(char_limit)、
    英語のみの断片は語数(word_limit)で判定する。

    以前は言語を問わず先に「char_limit字以内」を要求していたため、プロンプトで指示している
    「英語は40語以内」(約240字)を守っても英語の引用は必ず不合格になっていた
    (実収集で「引用が長すぎ」による除外が候補の約7割を占めた)。上限そのものは緩めていない。
    """
    text = nfc(quote or "").strip()
    if not text:
        return False
    words = [w for w in re.split(r"\s+", text) if w]
    if words and all(all(ord(c) < 128 for c in w) for w in words):
        # 英語のみ: 語数で判定(語数上限の12倍を字数の安全上限とし、極端に長い1語対策にする)
        return len(words) <= word_limit and len(text) <= word_limit * 12
    return len(text) <= char_limit


def published_sort_key(published_at: str | None) -> tuple[int, str]:
    """新しい順。published_at が無い件は新しいとみなさない。"""
    if published_at:
        return (1, published_at)
    return (0, "")
