"""承認済み課題定義のエクスポート（SPEC 第 8 章）。"""

from __future__ import annotations

import json
import re
from typing import Iterable

from app.config import CC_BY_SA, STACKEXCHANGE_CODES
from app.textutil import evidence_license, nfc, normalize_physical, split_evidence, utc_now

SHA_RE = re.compile(r"^[0-9a-f]{64}$")

EXPORT_KEYS = (
    "domain",
    "goal",
    "improve",
    "worsens",
    "ideal",
    "physical",
    "constraints",
    "resources",
    "evidence",
)


def evidence_complete(row: dict | object) -> bool:
    evidence = _get(row, "evidence") or ""
    parsed = split_evidence(evidence)
    if parsed is None:
        return False
    url, fetched_at, sha, _reason = parsed
    if not url.strip() or not fetched_at.strip() or not SHA_RE.fullmatch(sha):
        return False
    source_url = _get(row, "source_url") or ""
    fetched_col = _get(row, "fetched_at") or ""
    sha_col = _get(row, "content_sha256") or ""
    if source_url != url or fetched_col != fetched_at or sha_col != sha:
        return False
    source_code = _get(row, "source_code") or ""
    if source_code in STACKEXCHANGE_CODES:
        if evidence_license(evidence) != CC_BY_SA:
            return False
    return True


def required_fields_filled(row: dict | object) -> bool:
    for key in ("goal", "improve", "worsens", "ideal", "constraints", "resources", "evidence"):
        value = _get(row, key)
        if value is None or not nfc(str(value)).strip():
            return False
    domain = _get(row, "domain")
    if domain not in ("software", "workflow", "research"):
        return False
    return True


def row_to_problem(row: dict | object) -> dict:
    physical = normalize_physical(_get(row, "physical"))
    if physical is not None:
        physical = nfc(physical)
    return {
        "domain": _get(row, "domain"),
        "goal": nfc(_get(row, "goal") or ""),
        "improve": nfc(_get(row, "improve") or ""),
        "worsens": nfc(_get(row, "worsens") or ""),
        "ideal": nfc(_get(row, "ideal") or ""),
        "physical": physical,
        "constraints": nfc(_get(row, "constraints") or ""),
        "resources": nfc(_get(row, "resources") or ""),
        "evidence": nfc(_get(row, "evidence") or ""),
    }


def exportable(row: dict | object) -> bool:
    if _get(row, "status") != "approved":
        return False
    if not required_fields_filled(row):
        return False
    if not evidence_complete(row):
        return False
    return True


def wrap_export(items: Iterable[dict]) -> dict:
    return {
        "format": "localsaporter-problem-definition",
        "format_version": "1",
        "exported_at": utc_now(),
        "items": list(items),
    }


def dumps_export(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def export_filename(now: str | None = None) -> str:
    stamp = (now or utc_now()).replace("-", "").replace(":", "")
    if stamp.endswith("Z") and "T" in stamp:
        return f"triz-problems-{stamp}.json"
    compact = stamp.replace("+0000", "Z")
    return f"triz-problems-{compact}.json"


def _get(row: dict | object, key: str):
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, None)
