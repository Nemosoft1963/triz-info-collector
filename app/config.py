"""実行時設定。SPEC で仮とされた値はここでも仮のまま持つ。"""

from __future__ import annotations

import os


APP_NAME = "DesignNetaCollector"
APP_VERSION = "0.6"

USER_AGENT_TEMPLATE = "DesignNetaCollector/0.6 (contact: {email})"

SOURCE_CODES = (
    "github_api",
    "k8s_blog_rss",
    "martinfowler_atom",
    "infoq_rss",
    "openai_news_rss",
    "xbrl_org_rss",
    "cloudflare_blog_rss",
    "github_eng_blog_rss",
    "se_softwareengineering",
    "se_quant",
    "python_peps",
    "github_rust_rfcs",
    "github_k8s_keps",
    "github_pytorch_rfcs",
    "github_tf_rfcs",
    "github_vllm_rfc",
    "github_hf_blog",
    "google_research_rss",
    "meta_eng_rss",
    "nvidia_devblog_rss",
    "se_so_xbrl",
    "se_so_accounting",
    "se_so_invoice",
    "se_swe_ledger",
    "github_beancount",
    "azure_arch_center",
    "langchain_docs",
    "llamaindex_docs",
    "qiita_troubleshooting",
    "qiita_performance_tuning",
    "qiita_architecture_design",
    "qiita_refactoring",
)

QIITA_SOURCES = (
    ("qiita_troubleshooting", "software_dev", "トラブルシューティング"),
    ("qiita_performance_tuning", "software_dev", "パフォーマンスチューニング"),
    ("qiita_architecture_design", "software_dev", "アーキテクチャ設計"),
    ("qiita_refactoring", "software_dev", "リファクタリング"),
)
QIITA_SOURCE_CODES = frozenset(code for code, _domain, _tag in QIITA_SOURCES)

STACKEXCHANGE_CODES = frozenset(
    {
        "se_softwareengineering",
        "se_quant",
        "se_so_xbrl",
        "se_so_accounting",
        "se_so_invoice",
        "se_swe_ledger",
    }
)
PAPER_SOURCE_CODES = frozenset({"google_research_rss"})
CC_BY = "CC BY 4.0"
MIT_LICENSE = "MIT"
# 本文保存時に evidence へ出すライセンス。Stack Exchange は STACKEXCHANGE_CODES 側。
SOURCE_LICENSE = {
    "azure_arch_center": CC_BY,
    "langchain_docs": MIT_LICENSE,
    "llamaindex_docs": MIT_LICENSE,
}

COLLECTION_DOMAINS = ("software_dev", "ai", "accounting")
LOCALSAPORTER_DOMAINS = ("software", "workflow", "research")
STATUSES = ("draft", "approved", "rejected")

FIELD_MAX = 2500
DROP_QUERY_KEYS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid", "ref"})
SCHEDULE_HOUR_UTC = 4
SCHEDULE_MINUTE_UTC = 0
JOB_HISTORY_LIMIT = 20
BODY_RETENTION_DAYS = 7
RETRY_DELAYS_SEC = (1, 2, 4)
LLM_FAIL_STREAK_LIMIT = 5
HTTP_TIMEOUT_SEC = 30
RSS_MIN_INTERVAL_SEC = 1.0
OLLAMA_BODY_CHAR_LIMIT = 8000
QUOTE_CHAR_LIMIT = 80
QUOTE_WORD_LIMIT = 40
SOURCE_INGEST_LIMIT = 50
DEFAULT_OLLAMA_HOST = "http://host.docker.internal:11434"
CC_BY_SA = "CC BY-SA"

CONTRADICTION_KEYWORDS = (
    "trade-off",
    "tradeoff",
    "at the cost of",
    "at the expense of",
    "bottleneck",
    "versus",
    "latency",
    "throughput",
    "however",
    "work around",
    "workaround",
    "mitigat",
    "トレードオフ",
    "両立",
    "ボトルネック",
    "犠牲",
    "一方で",
    "課題",
    "回避策",
    "工夫",
)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    if value == "":
        return default
    return value


def load_config(overrides: dict | None = None) -> dict:
    contact = _env("CONTACT_EMAIL", "") or ""
    ua_contact = contact if contact else "not-configured"
    draft_provider = (_env("DRAFT_PROVIDER", "none") or "none").lower()
    config = {
        "DATABASE_PATH": _env("DATABASE_PATH", "/data/collector.db"),
        "CONTACT_EMAIL": contact,
        "GITHUB_TOKEN": _env("GITHUB_TOKEN", "") or "",
        "SEBAS_BASE_URL": _env("SEBAS_BASE_URL", "") or "",
        "SEBAS_PROJECT_ID": _env("SEBAS_PROJECT_ID", "default") or "default",
        "TZ": _env("TZ", "UTC") or "UTC",
        "DRAFT_PROVIDER": draft_provider,
        "OLLAMA_HOST": _env("OLLAMA_HOST", DEFAULT_OLLAMA_HOST) or DEFAULT_OLLAMA_HOST,
        "OLLAMA_MODEL": _env("OLLAMA_MODEL", "") or "",
        "BIND_HOST": _env("BIND_HOST", "127.0.0.1") or "127.0.0.1",
        "PORT": int(_env("PORT", "8080") or "8080"),
        "USER_AGENT": USER_AGENT_TEMPLATE.format(email=ua_contact),
        "FIELD_MAX": FIELD_MAX,
        "JOB_HISTORY_LIMIT": JOB_HISTORY_LIMIT,
        "BODY_RETENTION_DAYS": BODY_RETENTION_DAYS,
        "HTTP_TIMEOUT_SEC": HTTP_TIMEOUT_SEC,
        "SCHEDULE_HOUR_UTC": SCHEDULE_HOUR_UTC,
        "SCHEDULE_MINUTE_UTC": SCHEDULE_MINUTE_UTC,
        "SOURCE_INGEST_LIMIT": SOURCE_INGEST_LIMIT,
        "CONTRADICTION_KEYWORDS": CONTRADICTION_KEYWORDS,
        "QUOTE_CHAR_LIMIT": QUOTE_CHAR_LIMIT,
        "QUOTE_WORD_LIMIT": QUOTE_WORD_LIMIT,
    }
    if overrides:
        config.update(overrides)
    return config
