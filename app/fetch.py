"""情報源からの取得。HTTP クライアントは差し替え可能。"""

from __future__ import annotations

import base64
import html as _html
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, NamedTuple
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import feedparser
import httpx

from app.config import QIITA_SOURCE_CODES, QIITA_SOURCES, RETRY_DELAYS_SEC, RSS_MIN_INTERVAL_SEC
from app.textutil import nfc, parse_iso8601_to_utc, strip_html


class FetchError(Exception):
    def __init__(self, message: str, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


@dataclass
class FetchedDocument:
    source_code: str
    collection_domain: str
    source_url: str
    source_title: str
    body: str
    published_at: str | None
    doi: str | None = None
    language: str = "en"


class HttpClient:
    def get(self, url: str, headers: dict | None = None) -> tuple[int, str, dict]:
        raise NotImplementedError


class HttpxClient(HttpClient):
    def __init__(self, user_agent: str, timeout: float, token: str = ""):
        self.user_agent = user_agent
        self.timeout = timeout
        self.token = token

    def get(self, url: str, headers: dict | None = None) -> tuple[int, str, dict]:
        merged = {"User-Agent": self.user_agent, "Accept": "*/*"}
        if self.token and "api.github.com" in url:
            merged["Authorization"] = f"Bearer {self.token}"
            merged["Accept"] = "application/vnd.github+json"
            merged["X-GitHub-Api-Version"] = "2022-11-28"
        if headers:
            merged.update(headers)
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            response = client.get(url, headers=merged)
        text = response.text
        hdrs = {k.lower(): v for k, v in response.headers.items()}
        return response.status_code, text, hdrs


class RecordingClient(HttpClient):
    def __init__(self, responses: dict[str, tuple[int, str, dict]] | None = None):
        self.responses = responses or {}
        self.calls: list[str] = []

    def get(self, url: str, headers: dict | None = None) -> tuple[int, str, dict]:
        self.calls.append(url)
        if url in self.responses:
            return self.responses[url]
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        if url == robots_url:
            return 404, "not found", {}
        raise FetchError(f"フィクスチャ未登録: {url}")


class RateLimiter:
    def __init__(self, min_interval_sec: float):
        self.min_interval_sec = min_interval_sec
        self._last = 0.0

    def wait(self, sleeper: Callable[[float], None] | None = None) -> None:
        if self.min_interval_sec <= 0:
            return
        now = time.monotonic()
        gap = self.min_interval_sec - (now - self._last)
        if gap > 0:
            (sleeper or time.sleep)(gap)
        self._last = time.monotonic()


class RobotsCache:
    def __init__(self, ttl_hours: int = 24):
        self.ttl = timedelta(hours=ttl_hours)
        self._entries: dict[str, tuple[datetime, RobotFileParser | None]] = {}

    def allowed(self, client: HttpClient, url: str, user_agent: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        now = datetime.now(timezone.utc)
        cached = self._entries.get(origin)
        if cached is None or now - cached[0] >= self.ttl:
            robots_url = origin + "/robots.txt"
            parser: RobotFileParser | None
            try:
                status, text, _ = client.get(robots_url)
                if status >= 400:
                    parser = None
                else:
                    parser = RobotFileParser()
                    parser.set_url(robots_url)
                    parser.parse(text.splitlines())
            except Exception:
                parser = None
            self._entries[origin] = (now, parser)
            cached = self._entries[origin]
        parser = cached[1]
        if parser is None:
            return True
        try:
            return parser.can_fetch(user_agent, url)
        except Exception:
            return True


def request_with_retry(
    client: HttpClient,
    url: str,
    headers: dict | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> tuple[int, str, dict]:
    sleep = sleeper or time.sleep
    last_error: FetchError | None = None
    attempts = 1 + len(RETRY_DELAYS_SEC)
    for i in range(attempts):
        try:
            status, text, hdrs = client.get(url, headers=headers)
        except FetchError as exc:
            last_error = exc
            if i < len(RETRY_DELAYS_SEC):
                sleep(RETRY_DELAYS_SEC[i])
                continue
            raise
        except Exception as cop:
            last_error = FetchError(str(cop))
            if i < len(RETRY_DELAYS_SEC):
                sleep(RETRY_DELAYS_SEC[i])
                continue
            raise last_error from cop

        if status == 422:
            raise FetchError(
                "HTTP 422。GitHub 検索構文または検証失敗の疑い",
                http_status=422,
            )
        if status in (401, 403):
            details = []
            if "x-ratelimit-remaining" in hdrs:
                details.append(f"x-ratelimit-remaining: {hdrs['x-ratelimit-remaining']}")
            if "retry-after" in hdrs:
                details.append(f"retry-after: {hdrs['retry-after']}")
            detail_str = f" [{', '.join(details)}]" if details else ""
            search_hint = ""
            if status == 403:
                search_hint = "。GitHub Search APIのレート制限超過(認証時30回/分)または権限不足の可能性があります"
            raise FetchError(
                f"認証または拒否。User-Agent / トークン / 設定を確認する{detail_str}{search_hint}",
                http_status=status,
            )
        if status == 429:
            retry_after = hdrs.get("retry-after")
            wait_s = None
            if retry_after:
                try:
                    wait_s = float(retry_after)
                except ValueError:
                    wait_s = None
            if wait_s is None:
                if i < len(RETRY_DELAYS_SEC):
                    wait_s = RETRY_DELAYS_SEC[i]
                else:
                    raise FetchError("HTTP 429 が継続した", http_status=429)
            sleep(wait_s)
            last_error = FetchError("HTTP 429", http_status=429)
            continue
        if status >= 500:
            last_error = FetchError(f"HTTP {status}", http_status=status)
            if i < len(RETRY_DELAYS_SEC):
                sleep(RETRY_DELAYS_SEC[i])
                continue
            raise last_error
        if status >= 400:
            raise FetchError(f"HTTP {status}", http_status=status)
        return status, text, hdrs
    if last_error:
        raise last_error
    raise FetchError("取得失敗")


def _entry_link(entry) -> str:
    if getattr(entry, "link", None):
        return str(entry.link).strip()
    links = getattr(entry, "links", None) or []
    for link in links:
        href = link.get("href") if isinstance(link, dict) else getattr(link, "href", None)
        if href:
            return str(href).strip()
    return ""


def _entry_summary(entry) -> str:
    for attr in ("summary", "description", "content"):
        value = getattr(entry, attr, None)
        if not value:
            continue
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, dict):
                text = first.get("value") or ""
            else:
                text = getattr(first, "value", "") or str(first)
            if text:
                return nfc(str(text))
        else:
            return nfc(str(value))
    return ""


def _entry_published(entry) -> str | None:
    for attr in ("published", "updated", "created"):
        value = getattr(entry, attr, None)
        parsed = parse_iso8601_to_utc(value) if value else None
        if parsed:
            return parsed
    for attr in ("published_parsed", "updated_parsed"):
        struct = getattr(entry, attr, None)
        if struct:
            try:
                dt = datetime(*struct[:6], tzinfo=timezone.utc)
                return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            except Exception:
                continue
    return None


def parse_feed(
    xml_text: str,
    source_code: str,
    collection_domain: str,
) -> list[FetchedDocument]:
    parsed = feedparser.parse(xml_text)
    if parsed.bozo and not parsed.entries:
        raise FetchError("フィードのパースに失敗した")
    docs: list[FetchedDocument] = []
    for entry in parsed.entries:
        url = _entry_link(entry)
        title = nfc(str(getattr(entry, "title", "") or "")).strip()
        body = strip_html(_entry_summary(entry)).strip()
        body = " ".join(body.split())
        if not url or not title or not body:
            continue
        docs.append(
            FetchedDocument(
                source_code=source_code,
                collection_domain=collection_domain,
                source_url=url,
                source_title=title,
                body=body,
                published_at=_entry_published(entry),
                language="en",
            )
        )
    return docs


def parse_github_issues(
    items: list,
    source_code: str,
    collection_domain: str,
) -> list[FetchedDocument]:
    docs: list[FetchedDocument] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = (item.get("html_url") or "").strip()
        title = nfc(str(item.get("title") or "")).strip()
        body = nfc(str(item.get("body") or "")).strip()
        if not url or not title or not body:
            continue
        published = parse_iso8601_to_utc(item.get("updated_at") or item.get("created_at"))
        docs.append(
            FetchedDocument(
                source_code=source_code,
                collection_domain=collection_domain,
                source_url=url,
                source_title=title,
                body=body,
                published_at=published,
                language="en",
            )
        )
    return docs


def parse_github_search(
    json_obj: dict, collection_domain: str, source_code: str = "github_api"
) -> list[FetchedDocument]:
    items = json_obj.get("items") or []
    return parse_github_issues(items, source_code, collection_domain)


def parse_github_repo_issues(
    payload, source_code: str, collection_domain: str
) -> list[FetchedDocument]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as cop:
            raise FetchError("GitHub 応答が JSON として不正") from cop
    if not isinstance(payload, list):
        raise FetchError("GitHub Issue 一覧が配列ではない")
    return parse_github_issues(payload, source_code, collection_domain)


def parse_stackexchange_search(
    json_obj: dict, source_code: str, collection_domain: str
) -> list[FetchedDocument]:
    items = json_obj.get("items") or []
    docs: list[FetchedDocument] = []
    for item in items:
        url = (item.get("link") or "").strip()
        title = _html.unescape(nfc(str(item.get("title") or ""))).strip()
        body = item.get("body_markdown") or item.get("body") or ""
        body = _html.unescape(strip_html(nfc(str(body)))).strip()
        body = " ".join(body.split())
        if not url or not title or not body:
            continue
        published = None
        creation = item.get("creation_date")
        if isinstance(creation, (int, float)):
            published = datetime.fromtimestamp(int(creation), tz=timezone.utc).replace(
                microsecond=0
            ).isoformat().replace("+00:00", "Z")
        docs.append(
            FetchedDocument(
                source_code=source_code,
                collection_domain=collection_domain,
                source_url=url,
                source_title=title,
                body=body,
                published_at=published,
                language="en",
            )
        )
    return docs


def parse_qiita_items(
    payload, source_code: str, collection_domain: str
) -> list[FetchedDocument]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as cop:
            raise FetchError("Qiita 応答が JSON として不正") from cop
    if not isinstance(payload, list):
        raise FetchError("Qiita 記事一覧が配列ではない")
    docs: list[FetchedDocument] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or "").strip()
        title = nfc(str(item.get("title") or "")).strip()
        body = nfc(str(item.get("body") or "")).strip()
        if not url or not title or not body:
            continue
        docs.append(
            FetchedDocument(
                source_code=source_code,
                collection_domain=collection_domain,
                source_url=url,
                source_title=title,
                body=body,
                published_at=parse_iso8601_to_utc(item.get("created_at")),
                language="ja",
            )
        )
    return docs


def decode_github_file_content(item: dict) -> str:
    if item.get("encoding") == "base64" and item.get("content"):
        raw = str(item["content"]).replace("\n", "")
        return base64.b64decode(raw).decode("utf-8", errors="replace")
    if item.get("content"):
        return str(item["content"])
    return ""


def parse_github_contents_list(payload) -> list[dict]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as cop:
            raise FetchError("GitHub Contents が JSON として不正") from cop
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return payload
    raise FetchError("GitHub Contents の形が不正")


def pep_number_from_rss_title_or_link(title: str, url: str) -> str | None:
    import re

    for text in (title, url):
        m = re.search(r"pep[-_ ]?0*(\d{1,4})", text or "", re.I)
        if m:
            return m.group(1).zfill(4)
    return None


def strip_yaml_frontmatter(text: str) -> str:
    raw = nfc(text or "").replace("\r\n", "\n")
    if not raw.startswith("---"):
        return raw
    end = raw.find("\n---", 3)
    if end < 0:
        return raw
    rest = raw[end + 4 :]
    if rest.startswith("\n"):
        rest = rest[1:]
    return rest


def markdown_title(text: str, fallback: str = "") -> str:
    import re

    raw = nfc(text or "").replace("\r\n", "\n")
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end >= 0:
            for line in raw[3:end].splitlines():
                if line.lower().startswith("title:"):
                    title = line.split(":", 1)[1].strip().strip("\"'")
                    if " | " in title:
                        title = title.split(" | ", 1)[0].strip()
                    if title:
                        return title
    body = strip_yaml_frontmatter(raw)
    for line in body.splitlines():
        m = re.match(r"^#\s+(.+?)\s*#*\s*$", line)
        if m:
            return m.group(1).strip()
    return fallback


def public_doc_url(path: str, public_base: str, strip_prefix: str) -> str:
    rel = (path or "").strip()
    if strip_prefix and rel.startswith(strip_prefix):
        rel = rel[len(strip_prefix) :]
    for ext in (".mdx", ".md"):
        if rel.endswith(ext):
            rel = rel[: -len(ext)]
            break
    return public_base.rstrip("/") + "/" + rel.lstrip("/")


def parse_markdown_doc(
    text: str,
    source_code: str,
    collection_domain: str,
    source_url: str,
    fallback_title: str = "",
    published_at: str | None = None,
) -> FetchedDocument | None:
    title = markdown_title(text, fallback_title).strip()
    body = strip_yaml_frontmatter(text).strip()
    if not title or not body:
        return None
    return FetchedDocument(
        source_code=source_code,
        collection_domain=collection_domain,
        source_url=source_url,
        source_title=title,
        body=body,
        published_at=published_at,
        language="en",
    )


class GithubMarkdownSource(NamedTuple):
    source_code: str
    collection_domain: str
    owner: str
    repo: str
    dir_paths: tuple[str, ...]
    extra_files: tuple[str, ...]
    extensions: tuple[str, ...]
    public_base: str
    strip_prefix: str


# Azure Architecture Center の AI/ML 部門。本文は CC BY 4.0 の GitHub ソース。
GITHUB_MARKDOWN_SOURCES: tuple[GithubMarkdownSource, ...] = (
    GithubMarkdownSource(
        source_code="azure_arch_center",
        collection_domain="ai",
        owner="MicrosoftDocs",
        repo="architecture-center",
        dir_paths=("docs/ai-ml/architecture", "docs/ai-ml/guide"),
        extra_files=(),
        extensions=(".md",),
        public_base="https://learn.microsoft.com/azure/architecture/",
        strip_prefix="docs/",
    ),
)

# 公式が機械取得用に公開している Markdown。URL は調査で 200 を確認したページのみ。
# (markdown_url, canonical_url, fallback_title)
MARKDOWN_PAGE_SOURCES: list[tuple[str, str, tuple[tuple[str, str, str], ...]]] = [
    (
        "langchain_docs",
        "ai",
        (
            (
                "https://docs.langchain.com/oss/python/concepts/context.md",
                "https://docs.langchain.com/oss/python/concepts/context",
                "Context overview",
            ),
            (
                "https://docs.langchain.com/oss/python/concepts/memory.md",
                "https://docs.langchain.com/oss/python/concepts/memory",
                "Memory overview",
            ),
            (
                "https://docs.langchain.com/oss/python/langchain/agents.md",
                "https://docs.langchain.com/oss/python/langchain/agents",
                "Agents",
            ),
            (
                "https://docs.langchain.com/oss/python/langchain/context-engineering.md",
                "https://docs.langchain.com/oss/python/langchain/context-engineering",
                "Context engineering in agents",
            ),
            (
                "https://docs.langchain.com/oss/python/langchain/component-architecture.md",
                "https://docs.langchain.com/oss/python/langchain/component-architecture",
                "Component architecture",
            ),
            (
                "https://docs.langchain.com/oss/python/deepagents/rag.md",
                "https://docs.langchain.com/oss/python/deepagents/rag",
                "Retrieval Augmented Generation (RAG)",
            ),
        ),
    ),
    (
        "llamaindex_docs",
        "ai",
        (
            (
                "https://developers.llamaindex.ai/python/framework/use_cases/index.md",
                "https://developers.llamaindex.ai/python/framework/use_cases/",
                "Use Cases",
            ),
            (
                "https://developers.llamaindex.ai/python/framework/use_cases/q_and_a/index.md",
                "https://developers.llamaindex.ai/python/framework/use_cases/q_and_a/",
                "Question-Answering (RAG)",
            ),
            (
                "https://developers.llamaindex.ai/python/framework/use_cases/agents/index.md",
                "https://developers.llamaindex.ai/python/framework/use_cases/agents/",
                "Agents",
            ),
        ),
    ),
]


RSS_SOURCES: list[tuple[str, str, str]] = [
    ("k8s_blog_rss", "software_dev", "https://kubernetes.io/feed.xml"),
    ("martinfowler_atom", "software_dev", "https://martinfowler.com/feed.atom"),
    ("infoq_rss", "software_dev", "https://feed.infoq.com/articles"),
    ("openai_news_rss", "ai", "https://openai.com/blog/rss.xml"),
    ("xbrl_org_rss", "accounting", "https://www.xbrl.org/feed/"),
    ("cloudflare_blog_rss", "software_dev", "https://blog.cloudflare.com/rss"),
    ("github_eng_blog_rss", "software_dev", "https://github.blog/engineering/feed/"),
    ("google_research_rss", "ai", "https://research.google/blog/rss/"),
    ("meta_eng_rss", "ai", "https://engineering.fb.com/feed/"),
    ("nvidia_devblog_rss", "ai", "https://developer.nvidia.com/blog/feed"),
]

PEP_RSS_URL = "https://peps.python.org/peps.rss"

STACKEXCHANGE_ADVANCED: list[tuple[str, str, str, str, str]] = [
    ("se_softwareengineering", "software_dev", "softwareengineering", "tradeoff", "design"),
    ("se_quant", "accounting", "quant", "tradeoff", "data"),
    ("se_so_xbrl", "accounting", "stackoverflow", "", "xbrl"),
    ("se_so_accounting", "accounting", "stackoverflow", "", "accounting"),
    ("se_so_invoice", "accounting", "stackoverflow", "", "invoice"),
    ("se_swe_ledger", "accounting", "softwareengineering", "accounting ledger", ""),
]

GITHUB_ISSUE_REPOS: list[tuple[str, str, str, str]] = [
    ("github_beancount", "accounting", "beancount", "beancount"),
]


# Qiita API v2検索。認証なし60回/時間のため、全タグで共有する6秒間隔を適用する。
QIITA_API_URL = "https://qiita.com/api/v2/items"
QIITA_PER_PAGE_MAX = 50
QIITA_MIN_INTERVAL_SEC = 6.0


GITHUB_SEARCH_SOURCES = frozenset({"github_api", "github_vllm_rfc"})
GITHUB_SEARCH_INTERVAL_AUTH_SEC = 3.0
GITHUB_SEARCH_INTERVAL_UNAUTH_SEC = 10.0


def source_interval(source_code: str, github_authenticated: bool) -> float:
    if source_code in GITHUB_SEARCH_SOURCES:
        # GitHub Search API (/search/issues) は認証時 30回/分(=2秒/回)、未認証 10回/分(=6秒/回)。
        # 短時間の連続実行による 403 (Rate Limit) を防ぐため、安全側に余裕を持たせた間隔を適用する。
        return GITHUB_SEARCH_INTERVAL_AUTH_SEC if github_authenticated else GITHUB_SEARCH_INTERVAL_UNAUTH_SEC
    githubish = (
        "github_rust_rfcs",
        "github_k8s_keps",
        "python_peps",
        "github_pytorch_rfcs",
        "github_tf_rfcs",
        "github_hf_blog",
        "github_beancount",
        "azure_arch_center",
    )
    if source_code in githubish:
        return 1.0 if github_authenticated else 6.0
    if source_code in ("se_softwareengineering", "se_quant", "se_so_xbrl", "se_so_accounting", "se_so_invoice", "se_swe_ledger"):
        return 1.0
    if source_code in QIITA_SOURCE_CODES:
        return QIITA_MIN_INTERVAL_SEC
    return RSS_MIN_INTERVAL_SEC


@dataclass
class FetchContext:
    client: HttpClient
    user_agent: str
    contact_email: str
    github_token: str
    robots: RobotsCache
    limiters: dict[str, RateLimiter] = field(default_factory=dict)
    sleeper: Callable[[float], None] | None = None

    def limiter_for(self, source_code: str) -> RateLimiter:
        if source_code in GITHUB_SEARCH_SOURCES:
            key = "github_search"
        elif source_code in QIITA_SOURCE_CODES:
            key = "qiita"
        else:
            key = source_code
        if key not in self.limiters:
            self.limiters[key] = RateLimiter(
                source_interval(source_code, bool(self.github_token))
            )
        return self.limiters[key]

    def get(self, source_code: str, url: str, headers: dict | None = None) -> tuple[int, str, dict]:
        if not self.robots.allowed(self.client, url, self.user_agent):
            raise FetchError(f"robots.txt により取得しない: {url}")
        self.limiter_for(source_code).wait(self.sleeper)
        return request_with_retry(self.client, url, headers=headers, sleeper=self.sleeper)
