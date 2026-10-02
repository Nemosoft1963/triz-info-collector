"""GitHub Search クエリ。公式に確認できた構文だけ使う（SPEC 0.5 / 0.6）。"""

from __future__ import annotations

import re

MAX_BOOLEAN_OPERATORS = 5
OPERATOR_RE = re.compile(r"\b(?:AND|OR|NOT)\b", re.IGNORECASE)

GITHUB_QUERIES = {
    "software_dev": (
        "RFC OR design OR proposal OR tradeoff OR drawback is:issue is:public"
    ),
    "ai": (
        "LLM OR design OR proposal OR tradeoff OR drawback is:issue is:public"
    ),
}

# 演算子 0。修飾子とキーワードのみ（SPEC 0.6）
VLLM_RFC_QUERY = "repo:vllm-project/vllm RFC is:issue"


def boolean_operator_count(query: str) -> int:
    return len(OPERATOR_RE.findall(query or ""))


def validate_github_search_query(query: str) -> str:
    q = (query or "").strip()
    if not q:
        raise ValueError("empty query")
    if "(" in q or ")" in q:
        raise ValueError("括弧は GitHub REST 検索クエリに使わない")
    if boolean_operator_count(q) > MAX_BOOLEAN_OPERATORS:
        raise ValueError("AND/OR/NOT は 5 個まで")
    return q
