"""文字列・URL・ハッシュ・切り詰め。数え方は SPEC 6.4（仮 Q18）。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.config import DROP_QUERY_KEYS, FIELD_MAX


def nfc(text: str) -> str:
    if text is None:
        return ""
    return unicodedata.normalize("NFC", text)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso8601_to_utc(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    candidate = raw
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    encoded = nfc(text).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_url(url: str) -> str:
    parsed = urlparse(nfc(url).strip())
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if not scheme or not host:
        raise ValueError("URL を正規化できない")
    port = parsed.port
    netloc = host
    if port is not None:
        if not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            netloc = f"{host}:{port}"
    kept = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lower = key.lower()
        if lower.startswith("utm_") or lower in DROP_QUERY_KEYS:
            continue
        kept.append((key, value))
    query = urlencode(kept, doseq=True)
    path = parsed.path or ""
    return urlunparse((scheme, netloc, path, "", query, ""))


SENTENCE_SEPS = ("。", "．", ".", "!", "?", "\n")


def truncate_to_limit(text: str, limit: int = FIELD_MAX) -> tuple[str, bool]:
    text = nfc(text)
    if len(text) <= limit:
        return text, False
    window = text[:limit]
    best = -1
    for sep in SENTENCE_SEPS:
        idx = window.rfind(sep)
        if idx > best:
            best = idx
    if best >= 0:
        cut = window[: best + 1]
        if cut:
            return cut, True
    return window, True


def split_evidence(evidence: str) -> tuple[str, str, str, str] | None:
    """evidence をメタ + 根拠として読む。欠けていれば None。"""
    text = nfc(evidence).replace("\r\n", "\n")
    lines = text.split("\n")
    if len(lines) < 4:
        return None
    url_line, fetched_line, sha_line = lines[0], lines[1], lines[2]
    rest_lines = lines[3:]
    if not url_line.startswith("出典URL: "):
        return None
    if not fetched_line.startswith("取得日時: "):
        return None
    if not sha_line.startswith("本文SHA-256: "):
        return None
    if rest_lines and rest_lines[0].startswith("ライセンス:"):
        rest_lines = rest_lines[1:]
    rest = "\n".join(rest_lines)
    if not rest.startswith("根拠:"):
        return None
    url = url_line[len("出典URL: ") :].strip()
    fetched_at = fetched_line[len("取得日時: ") :].strip()
    sha = sha_line[len("本文SHA-256: ") :].strip()
    reason = rest[len("根拠:") :].lstrip(" ")
    return url, fetched_at, sha, reason


def evidence_license(evidence: str) -> str | None:
    text = nfc(evidence).replace("\r\n", "\n")
    for line in text.split("\n"):
        if line.startswith("ライセンス:"):
            return line[len("ライセンス:") :].strip()
    return None


def build_evidence(
    source_url: str,
    fetched_at: str,
    content_sha256: str,
    reason: str,
    license_label: str | None = None,
) -> str:
    reason = nfc(reason)
    lines = [
        f"出典URL: {source_url}",
        f"取得日時: {fetched_at}",
        f"本文SHA-256: {content_sha256}",
    ]
    if license_label:
        lines.append(f"ライセンス: {nfc(license_label)}")
    lines.append(f"根拠: {reason}")
    return "\n".join(lines)


def truncate_evidence(evidence: str, limit: int = FIELD_MAX) -> tuple[str, bool]:
    parsed = split_evidence(evidence)
    if parsed is None:
        return truncate_to_limit(evidence, limit)
    url, fetched_at, sha, reason = parsed
    license_label = evidence_license(evidence)
    meta_lines = [
        f"出典URL: {url}",
        f"取得日時: {fetched_at}",
        f"本文SHA-256: {sha}",
    ]
    if license_label:
        meta_lines.append(f"ライセンス: {license_label}")
    meta = "\n".join(meta_lines) + "\n根拠: "
    if len(nfc(meta)) >= limit:
        raise ValueError("evidence のメタ行だけで上限を超える")
    room = limit - len(nfc(meta))
    truncated_reason, was_cut = truncate_to_limit(reason, room)
    rebuilt = meta + truncated_reason
    return rebuilt, was_cut


def field_lengths_ok(fields: dict[str, str | None], limit: int = FIELD_MAX) -> list[str]:
    bad = []
    for name, value in fields.items():
        if value is None:
            continue
        if len(nfc(value)) > limit:
            bad.append(name)
    return bad


EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b")
AT_HANDLE_RE = re.compile(r"(?<![\w/])@[\w-]+")
HTML_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    return HTML_TAG_RE.sub(" ", nfc(text or ""))


def redact_identity(text: str) -> str:
    """個人を特定し得るメール・ハンドルを残さない（SPEC 5.1.1 / 6.6）。"""
    text = nfc(text)
    text = EMAIL_RE.sub("[email]", text)
    text = AT_HANDLE_RE.sub("@contributor", text)
    return text


_PHYSICAL_EMPTY_VALUES = frozenset(
    {"false", "none", "null", "n/a", "na", "nil", "not applicable", "not specified", "-", "--",
     "なし", "無し", "該当なし", "特になし", "不明", "未記載"}
)


def normalize_physical(value):
    """physical(任意項目)で「空」を意味するだけの値を None にそろえる。

    LLM が "False" / "None" / "Not applicable" などの文字列を返すことがあり、そのまま
    保存すると下流(LOCALSAPORTER の validate_problem)で無意味な記述として扱われるため。
    """
    if value is None:
        return None
    text = str(value).strip()
    if text == "" or text.lower().strip(" .。") in _PHYSICAL_EMPTY_VALUES:
        return None
    return text
