"""課題定義の下書き。LLM 実装は差し替え可能。生成物は必ず draft。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

from app.config import (
    CC_BY_SA,
    FIELD_MAX,
    LLM_FAIL_STREAK_LIMIT,
    OLLAMA_BODY_CHAR_LIMIT,
    PAPER_SOURCE_CODES,
    SOURCE_LICENSE,
    STACKEXCHANGE_CODES,
)
from app.filter import quote_in_body, quote_within_limit
from app.textutil import (
    normalize_physical,
    build_evidence,
    nfc,
    redact_identity,
    truncate_evidence,
    truncate_to_limit,
)


def default_domain(collection_domain: str, source_code: str) -> str:
    if source_code in PAPER_SOURCE_CODES:
        return "research"
    if collection_domain == "accounting":
        return "workflow"
    return "software"


def license_for_source(source_code: str) -> str | None:
    if source_code in STACKEXCHANGE_CODES:
        return CC_BY_SA
    return SOURCE_LICENSE.get(source_code)


@dataclass
class DraftInput:
    collection_domain: str
    source_code: str
    source_url: str
    source_title: str
    body: str
    fetched_at: str
    published_at: str | None
    content_sha256: str
    doi: str | None
    language: str


@dataclass
class DraftResult:
    domain: str
    goal: str
    improve: str
    worsens: str
    ideal: str
    physical: str | None
    constraints: str
    resources: str
    evidence: str
    truncated: int
    skip_reason: str | None = None


class DraftProvider:
    def draft(self, item: DraftInput) -> DraftResult:
        raise NotImplementedError


def _excerpt(body: str, limit: int = 80) -> str:
    text = nfc(redact_identity(body)).replace("\r\n", "\n").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) <= limit:
        return text
    return text[:limit]


def _evidence(item: DraftInput, reason: str) -> str:
    return build_evidence(
        item.source_url,
        item.fetched_at,
        item.content_sha256,
        reason,
        license_label=license_for_source(item.source_code),
    )


def _empty_result(item: DraftInput, skip_reason: str) -> DraftResult:
    return DraftResult(
        domain=default_domain(item.collection_domain, item.source_code),
        goal="",
        improve="",
        worsens="",
        ideal="",
        physical=None,
        constraints="",
        resources="",
        evidence=_evidence(item, ""),
        truncated=0,
        skip_reason=skip_reason,
    )


def _apply_limits(result: DraftResult) -> DraftResult:
    truncated = 0
    mapping = {
        "goal": result.goal,
        "improve": result.improve,
        "worsens": result.worsens,
        "ideal": result.ideal,
        "constraints": result.constraints,
        "resources": result.resources,
    }
    out = {}
    for key, value in mapping.items():
        cut, was = truncate_to_limit(value or "", FIELD_MAX)
        out[key] = cut
        if was:
            truncated = 1
        if key != "goal" and was and not cut.strip():
            result.skip_reason = "skipped_truncated_empty"
    physical = normalize_physical(result.physical)
    if physical is not None:
        physical = nfc(physical)
        cut, was = truncate_to_limit(physical, FIELD_MAX)
        physical = cut
        if was:
            truncated = 1
    try:
        evidence, was = truncate_evidence(result.evidence, FIELD_MAX)
    except ValueError:
        result.skip_reason = "skipped_evidence_meta_too_long"
        return result
    if was:
        truncated = 1
        if "根拠:" in evidence:
            reason = evidence.split("根拠:", 1)[1].lstrip(" ")
            if reason.strip() == "":
                result.skip_reason = "skipped_truncated_empty"
    result.goal = out["goal"]
    result.improve = out["improve"]
    result.worsens = out["worsens"]
    result.ideal = out["ideal"]
    result.constraints = out["constraints"]
    result.resources = out["resources"]
    result.physical = physical
    result.evidence = evidence
    result.truncated = truncated
    return result


class NoneDraftProvider(DraftProvider):
    def draft(self, item: DraftInput) -> DraftResult:
        domain = default_domain(item.collection_domain, item.source_code)
        excerpt = _excerpt(item.body)
        result = DraftResult(
            domain=domain,
            goal=nfc(item.source_title),
            improve="",
            worsens="",
            ideal="",
            physical=None,
            constraints="",
            resources="",
            evidence=_evidence(item, excerpt),
            truncated=0,
        )
        return _apply_limits(result)


OLLAMA_PROMPT = """次の公開情報から、技術的矛盾（ある特性 A を良くすると別の特性 B が悪化する）と
その解決の工夫が読み取れるかを判定する。読み取れないときは readable を false にし、
他の文字列は空文字、evidence_quote は空文字、physical は null にする。
読み取れるときだけ日本語で課題定義を書く。原文の長文転載はしない。
個人名、メール、ユーザーハンドルは残さない。
evidence_quote には、矛盾の根拠となる本文中の原文を、本文から一字一句コピーして入れる。
言い換えや創作の引用は禁止。80 字以内（英語は 40 語以内）。

情報源タイトル: {title}
出典URL: {url}
本文:
{body}

次の JSON オブジェクトだけを返す。キー:
readable (true または false),
domain (software / workflow / research のいずれか),
goal, improve, worsens, ideal, physical (無いときは null),
constraints, resources,
evidence_quote (本文からの原文引用。URL・日時・ハッシュは書かない)
"""

OLLAMA_RETRY_PROMPT = """前回の evidence_quote は本文に含まれていなかった。
evidence_quote は次の本文から一字一句コピーすること。言い換え・要約・創作は禁止。
80 字以内（英語は 40 語以内）。読み取れないときは readable を false にする。

情報源タイトル: {title}
出典URL: {url}
本文:
{body}

次の JSON オブジェクトだけを返す。キー:
readable, domain, goal, improve, worsens, ideal, physical, constraints, resources, evidence_quote
"""


OLLAMA_RETRY_TOO_LONG_PROMPT = """前回の evidence_quote は長すぎた（上限は 80 字、英語は 40 語）。
本文から、矛盾を示す最も短い連続した断片を、一字一句コピーして返すこと。
1 文の一部でよい。言い換え・要約・創作は禁止。読み取れないときは readable を false にする。

情報源タイトル: {title}
出典URL: {url}
本文:
{body}

次の JSON オブジェクトだけを返す。キー:
readable, domain, goal, improve, worsens, ideal, physical, constraints, resources, evidence_quote
"""


def parse_ollama_response(raw: str) -> dict:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("not json")
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("not object")
    return data


class OllamaDraftProvider(DraftProvider):
    def __init__(self, host: str, model: str, timeout: float = 60.0, generate=None):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.fail_streak = 0
        self.stopped = False
        self._generate = generate

    def draft(self, item: DraftInput) -> DraftResult:
        if self.stopped:
            return _empty_result(item, "skipped_llm_stopped")
        if not self.model:
            self.fail_streak += 1
            return _empty_result(item, "skipped_llm_failed")
        body = nfc(redact_identity(item.body))
        if len(body) > OLLAMA_BODY_CHAR_LIMIT:
            body = body[:OLLAMA_BODY_CHAR_LIMIT]
        first = self._attempt(item, body, retry_reason=None)
        if first.skip_reason not in ("skipped_quote_not_found", "skipped_quote_too_long"):
            return first
        # 引用が本文に無い/長すぎる場合だけ、1回だけ理由を伝えて再判定する(検証は緩めない)
        return self._attempt(item, body, retry_reason=first.skip_reason)

    def _attempt(self, item: DraftInput, body: str, retry_reason: str | None) -> DraftResult:
        if retry_reason == "skipped_quote_too_long":
            template = OLLAMA_RETRY_TOO_LONG_PROMPT
        elif retry_reason is not None:
            template = OLLAMA_RETRY_PROMPT
        else:
            template = OLLAMA_PROMPT
        prompt = template.format(
            title=nfc(item.source_title),
            url=item.source_url,
            body=body,
        )
        try:
            raw = self._call(prompt)
        except Exception:
            self.fail_streak += 1
            if self.fail_streak >= LLM_FAIL_STREAK_LIMIT:
                self.stopped = True
            return _empty_result(item, "skipped_llm_failed")
        try:
            data = parse_ollama_response(raw)
        except (ValueError, json.JSONDecodeError):
            return _empty_result(item, "skipped_llm_invalid")
        self.fail_streak = 0
        try:
            return self._from_payload(item, data, body)
        except ValueError as exc:
            reason = str(exc) or "skipped_llm_invalid"
            if reason not in (
                "skipped_llm_invalid",
                "skipped_not_contradiction",
                "skipped_quote_not_found",
                "skipped_quote_too_long",
            ):
                reason = "skipped_llm_invalid"
            return _empty_result(item, reason)

    def _from_payload(self, item: DraftInput, data: dict, body_for_quote: str) -> DraftResult:
        readable = data.get("readable")
        if readable is False or readable == "false" or readable == 0:
            raise ValueError("skipped_not_contradiction")
        if readable not in (True, "true", 1, "True"):
            raise ValueError("skipped_llm_invalid")

        quote = nfc(str(data.get("evidence_quote") or "")).strip()
        if not quote:
            raise ValueError("skipped_llm_invalid")
        if not quote_within_limit(quote):
            raise ValueError("skipped_quote_too_long")
        if not quote_in_body(quote, body_for_quote) and not quote_in_body(quote, item.body):
            raise ValueError("skipped_quote_not_found")

        domain = data.get("domain") or default_domain(
            item.collection_domain, item.source_code
        )
        if domain not in ("software", "workflow", "research"):
            domain = default_domain(item.collection_domain, item.source_code)
        physical = normalize_physical(data.get("physical"))
        if physical is not None:
            physical = nfc(physical)
        result = DraftResult(
            domain=domain,
            goal=nfc(str(data.get("goal") or "")),
            improve=nfc(str(data.get("improve") or "")),
            worsens=nfc(str(data.get("worsens") or "")),
            ideal=nfc(str(data.get("ideal") or "")),
            physical=physical,
            constraints=nfc(str(data.get("constraints") or "")),
            resources=nfc(str(data.get("resources") or "")),
            evidence=_evidence(item, quote),
            truncated=0,
        )
        required = (
            result.goal,
            result.improve,
            result.worsens,
            result.ideal,
            result.constraints,
            result.resources,
        )
        if any(not (v or "").strip() for v in required):
            raise ValueError("skipped_llm_invalid")
        result = _apply_limits(result)
        if result.skip_reason:
            raise ValueError(result.skip_reason)
        return result

    def _call(self, prompt: str) -> str:
        if self._generate is not None:
            return self._generate(prompt)
        url = f"{self.host}/api/generate"
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                url,
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                },
            )
        if response.status_code != 200:
            raise RuntimeError(f"ollama HTTP {response.status_code}")
        payload = response.json()
        return payload.get("response") or ""


def create_draft_provider(config: dict) -> DraftProvider:
    name = (config.get("DRAFT_PROVIDER") or "none").lower()
    if name == "none":
        return NoneDraftProvider()
    if name == "ollama":
        return OllamaDraftProvider(
            host=config.get("OLLAMA_HOST") or "http://host.docker.internal:11434",
            model=config.get("OLLAMA_MODEL") or "",
        )
    raise ValueError(f"未知の DRAFT_PROVIDER: {name}")
