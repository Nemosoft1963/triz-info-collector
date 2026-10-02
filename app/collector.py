"""収集ジョブ。失敗は記録し、情報源単位で継続する。"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from app.config import OLLAMA_BODY_CHAR_LIMIT, QIITA_SOURCES, SOURCE_INGEST_LIMIT
from app.draft import DraftInput, DraftProvider, NoneDraftProvider, create_draft_provider
from app.export import evidence_complete, required_fields_filled
from app.fetch import (
    FetchContext,
    FetchError,
    FetchedDocument,
    GITHUB_ISSUE_REPOS,
    GITHUB_MARKDOWN_SOURCES,
    MARKDOWN_PAGE_SOURCES,
    PEP_RSS_URL,
    QIITA_API_URL,
    QIITA_PER_PAGE_MAX,
    RSS_SOURCES,
    STACKEXCHANGE_ADVANCED,
    decode_github_file_content,
    markdown_title,
    parse_feed,
    parse_github_contents_list,
    parse_github_repo_issues,
    parse_github_search,
    parse_markdown_doc,
    parse_qiita_items,
    parse_stackexchange_search,
    pep_number_from_rss_title_or_link,
    public_doc_url,
    strip_yaml_frontmatter,
)
from app.filter import looks_like_contradiction, published_sort_key
from app.github_query import GITHUB_QUERIES, VLLM_RFC_QUERY, validate_github_search_query
from app.sections import (
    ARCHITECTURE_HEADINGS,
    KEP_HEADINGS,
    PEP_HEADINGS,
    PYTORCH_HEADINGS,
    RFC_HEADINGS,
    TF_HEADINGS,
    extract_named_sections,
)
from app.textutil import (
    field_lengths_ok,
    nfc,
    normalize_url,
    redact_identity,
    sha256_text,
    utc_now,
)


def first_markdown_heading(text: str) -> str:
    for line in (text or "").splitlines():
        m = re.match(r"^#\s+(.+?)\s*#*\s*$", line)
        if m:
            return m.group(1).strip()
    return ""


class JobAlreadyRunning(Exception):
    pass


class Collector:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: dict,
        fetch_context: FetchContext,
        draft_provider: DraftProvider | None = None,
        enabled_sources: list[str] | None = None,
    ):
        self.conn = conn
        self.config = config
        self.fetch_context = fetch_context
        self.draft_provider = draft_provider or create_draft_provider(config)
        self.enabled_sources = enabled_sources
        self._lock = threading.Lock()
        self._running = False
        self._progress: dict = {
            "running": False,
            "started_at": None,
            "source_code": None,
            "run_id": None,
        }
        self._judged_this_run: dict[str, int] = {}
        self._skip_counts: dict[str, int] = {}

    def is_running(self) -> bool:
        return self._running

    def progress(self) -> dict:
        return dict(self._progress)

    def run(self, trigger: str) -> int:
        if trigger not in ("manual", "scheduled"):
            raise ValueError("trigger が不正")
        with self._lock:
            if self._running:
                raise JobAlreadyRunning("収集ジョブは実行中")
            self._running = True
        try:
            return self._run(trigger)
        finally:
            self._running = False
            self._progress = {
                "running": False,
                "started_at": None,
                "source_code": None,
                "run_id": None,
            }

    def _run(self, trigger: str) -> int:
        started = utc_now()
        self._judged_this_run = {}
        self._skip_counts = {}
        cur = self.conn.execute(
            """
            INSERT INTO collection_runs (started_at, trigger, status, draft_count, skipped_count, error_count)
            VALUES (?, ?, 'running', 0, 0, 0)
            """,
            (started, trigger),
        )
        self.conn.commit()
        run_id = int(cur.lastrowid)
        self._progress = {
            "running": True,
            "started_at": started,
            "source_code": None,
            "run_id": run_id,
        }
        draft_total = 0
        error_total = 0
        source_failures = 0
        job_failed = False
        try:
            for source_code, worker in self._source_plan():
                self._progress["source_code"] = source_code
                try:
                    fetched, drafts, _skipped = worker(run_id)
                    self.conn.execute(
                        """
                        INSERT INTO collection_run_sources
                            (run_id, source_code, status, fetched_count, draft_count, finished_at)
                        VALUES (?, ?, 'success', ?, ?, ?)
                        """,
                        (run_id, source_code, fetched, drafts, utc_now()),
                    )
                    self.conn.commit()
                    draft_total += drafts
                except FetchError as exc:
                    error_total += 1
                    source_failures += 1
                    self._record_error(run_id, source_code, exc.http_status, str(exc))
                    self.conn.execute(
                        """
                        INSERT INTO collection_run_sources
                            (run_id, source_code, status, fetched_count, draft_count, finished_at)
                        VALUES (?, ?, 'failed', 0, 0, ?)
                        """,
                        (run_id, source_code, utc_now()),
                    )
                    self.conn.commit()
                except sqlite3.Error:
                    job_failed = True
                    error_total += 1
                    self._record_error(run_id, source_code, None, "SQLite 書き込み失敗")
                    break
                except Exception as exc:
                    error_total += 1
                    source_failures += 1
                    self._record_error(run_id, source_code, None, str(exc))
                    self.conn.execute(
                        """
                        INSERT INTO collection_run_sources
                            (run_id, source_code, status, fetched_count, draft_count, finished_at)
                        VALUES (?, ?, 'failed', 0, 0, ?)
                        """,
                        (run_id, source_code, utc_now()),
                    )
                    self.conn.commit()
            self._purge_expired_bodies()
        except sqlite3.Error:
            job_failed = True

        if job_failed:
            status = "failed"
        elif source_failures:
            status = "partial"
        else:
            status = "success"

        skipped_total = sum(self._skip_counts.values())
        note = format_skip_counts(self._skip_counts)
        try:
            self.conn.execute(
                """
                UPDATE collection_runs
                SET finished_at = ?, status = ?, draft_count = ?, skipped_count = ?, error_count = ?, note = ?
                WHERE id = ?
                """,
                (utc_now(), status, draft_total, skipped_total, error_total, note, run_id),
            )
            self.conn.commit()
        except sqlite3.Error:
            pass
        return run_id

    def _source_plan(self) -> list[tuple[str, object]]:
        plan: list[tuple[str, object]] = []
        for source_code, collection_domain, _url in RSS_SOURCES:
            plan.append(
                (
                    source_code,
                    lambda run_id, sc=source_code, cd=collection_domain: self._collect_rss(
                        run_id, sc, cd
                    ),
                )
            )
        plan.append(("github_api", self._collect_github_all))
        plan.append(("python_peps", self._collect_python_peps))
        plan.append(("github_rust_rfcs", self._collect_rust_rfcs))
        plan.append(("github_k8s_keps", self._collect_k8s_keps))
        plan.append(("github_pytorch_rfcs", self._collect_pytorch_rfcs))
        plan.append(("github_tf_rfcs", self._collect_tf_rfcs))
        plan.append(("github_vllm_rfc", self._collect_vllm_rfc))
        plan.append(("github_hf_blog", self._collect_hf_blog))
        for source_code, collection_domain, owner, repo in GITHUB_ISSUE_REPOS:
            plan.append(
                (
                    source_code,
                    lambda run_id, sc=source_code, cd=collection_domain, ow=owner, rp=repo: self._collect_github_issues(
                        run_id, sc, cd, ow, rp
                    ),
                )
            )
        for source_code, collection_domain, site, q, tagged in STACKEXCHANGE_ADVANCED:
            plan.append(
                (
                    source_code,
                    lambda run_id, sc=source_code, cd=collection_domain, st=site, qq=q, tg=tagged: self._collect_stackexchange(
                        run_id, sc, cd, st, qq, tg
                    ),
                )
            )
        for source_code, collection_domain, tag in QIITA_SOURCES:
            plan.append(
                (
                    source_code,
                    lambda run_id, sc=source_code, cd=collection_domain, tg=tag: self._collect_qiita(
                        run_id, sc, cd, tg
                    ),
                )
            )
        for spec in GITHUB_MARKDOWN_SOURCES:
            plan.append(
                (
                    spec.source_code,
                    lambda run_id, s=spec: self._collect_github_markdown_dirs(run_id, s),
                )
            )
        for source_code, collection_domain, pages in MARKDOWN_PAGE_SOURCES:
            plan.append(
                (
                    source_code,
                    lambda run_id, sc=source_code, cd=collection_domain, pg=pages: self._collect_markdown_pages(
                        run_id, sc, cd, pg
                    ),
                )
            )
        if self.enabled_sources is None:
            return plan
        allowed = set(self.enabled_sources)
        return [row for row in plan if row[0] in allowed]

    def _collect_rss(self, run_id: int, source_code: str, collection_domain: str) -> tuple[int, int, int]:
        url = next(u for c, d, u in RSS_SOURCES if c == source_code)
        _status, text, _hdrs = self.fetch_context.get(source_code, url)
        docs = parse_feed(text, source_code, collection_domain)
        return self._ingest(run_id, source_code, docs)

    def _collect_github_all(self, run_id: int) -> tuple[int, int, int]:
        return self._collect_multi(
            run_id,
            "github_api",
            [
                lambda: self._collect_github(run_id, "software_dev"),
                lambda: self._collect_github(run_id, "ai"),
            ],
        )

    def _collect_multi(self, run_id: int, source_code: str, calls: list) -> tuple[int, int, int]:
        fetched = drafts = skipped = 0
        errors: list[FetchError] = []
        for call in calls:
            try:
                f, d, s = call()
                fetched += f
                drafts += d
                skipped += s
            except FetchError as exc:
                errors.append(exc)
        if errors and fetched == 0:
            raise errors[0]
        for exc in errors:
            self._record_error(run_id, source_code, exc.http_status, str(exc))
        return fetched, drafts, skipped

    def _collect_github(self, run_id: int, collection_domain: str) -> tuple[int, int, int]:
        query = validate_github_search_query(GITHUB_QUERIES[collection_domain])
        params = urlencode({"q": query, "per_page": "30", "sort": "updated"})
        url = f"https://api.github.com/search/issues?{params}"
        _status, text, _hdrs = self.fetch_context.get("github_api", url)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise FetchError("GitHub 応答が JSON として不正") from exc
        docs = parse_github_search(payload, collection_domain, "github_api")
        return self._ingest(run_id, "github_api", docs)

    def _collect_vllm_rfc(self, run_id: int) -> tuple[int, int, int]:
        query = validate_github_search_query(VLLM_RFC_QUERY)
        params = urlencode({"q": query, "per_page": "30", "sort": "updated"})
        url = f"https://api.github.com/search/issues?{params}"
        _status, text, _hdrs = self.fetch_context.get("github_vllm_rfc", url)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise FetchError("GitHub 応答が JSON として不正") from exc
        docs = parse_github_search(payload, "ai", "github_vllm_rfc")
        return self._ingest(run_id, "github_vllm_rfc", docs)

    def _collect_stackexchange(
        self,
        run_id: int,
        source_code: str,
        collection_domain: str,
        site: str,
        q: str,
        tagged: str,
    ) -> tuple[int, int, int]:
        query_params = {"order": "desc", "sort": "votes"}
        if q:  # 空なら全文検索語を付けない(q と tagged は AND になり 0 件になりやすい)
            query_params["q"] = q
        if tagged:
            query_params["tagged"] = tagged
        query_params.update(
            {
                "answers": "1",
                "site": site,
                "filter": "withbody",
                "pagesize": "30",
            }
        )
        params = urlencode(query_params)
        url = f"https://api.stackexchange.com/2.3/search/advanced?{params}"
        _status, text, _hdrs = self.fetch_context.get(source_code, url)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as cop:
            raise FetchError("Stack Exchange 応答が JSON として不正") from cop
        backoff = payload.get("backoff")
        if backoff:
            sleeper = self.fetch_context.sleeper
            if sleeper:
                sleeper(float(backoff))
        docs = parse_stackexchange_search(payload, source_code, collection_domain)
        return self._ingest(run_id, source_code, docs)

    def _collect_qiita(
        self, run_id: int, source_code: str, collection_domain: str, tag: str
    ) -> tuple[int, int, int]:
        limit = int(self.config.get("SOURCE_INGEST_LIMIT") or SOURCE_INGEST_LIMIT)
        per_page = min(max(limit, 1), QIITA_PER_PAGE_MAX)
        params = urlencode({"query": f"tag:{tag}", "per_page": str(per_page)})
        url = f"{QIITA_API_URL}?{params}"
        _status, text, _hdrs = self.fetch_context.get(source_code, url)
        docs = parse_qiita_items(text, source_code, collection_domain)
        return self._ingest(run_id, source_code, docs)

    def _collect_github_issues(
        self, run_id: int, source_code: str, collection_domain: str, owner: str, repo: str
    ) -> tuple[int, int, int]:
        params = urlencode({"state": "all", "per_page": "30", "sort": "updated"})
        url = f"https://api.github.com/repos/{owner}/{repo}/issues?{params}"
        _status, text, _hdrs = self.fetch_context.get(source_code, url)
        docs = parse_github_repo_issues(text, source_code, collection_domain)
        return self._ingest(run_id, source_code, docs)

    def _github_contents(self, source_code: str, url: str) -> list[dict]:
        headers = {"Accept": "application/vnd.github+json"}
        _status, text, _hdrs = self.fetch_context.get(source_code, url, headers=headers)
        return parse_github_contents_list(text)

    def _github_file_raw(self, source_code: str, api_url: str) -> tuple[str, str, str]:
        headers = {"Accept": "application/vnd.github.raw+json"}
        _status, text, _hdrs = self.fetch_context.get(source_code, api_url, headers=headers)
        try:
            payload = json.loads(text)
            if isinstance(payload, dict) and payload.get("content") is not None:
                body = decode_github_file_content(payload)
                html = payload.get("html_url") or ""
                title = payload.get("name") or html
                return html, body, title
        except json.JSONDecodeError:
            pass
        return "", text, first_markdown_heading(text)

    def _collect_python_peps(self, run_id: int) -> tuple[int, int, int]:
        _status, rss_text, _ = self.fetch_context.get("python_peps", PEP_RSS_URL)
        parsed = parse_feed(rss_text, "python_peps", "software_dev")
        docs: list[FetchedDocument] = []
        fetched = 0
        for entry in parsed[:SOURCE_INGEST_LIMIT]:
            fetched += 1
            num = pep_number_from_rss_title_or_link(entry.source_title, entry.source_url)
            if not num:
                self._count_skip("skipped_no_section")
                continue
            api = f"https://api.github.com/repos/python/peps/contents/peps/pep-{num}.rst"
            try:
                html, body, name = self._github_file_raw("python_peps", api)
            except FetchError:
                api = f"https://api.github.com/repos/python/peps/contents/peps/pep-{num}.txt"
                try:
                    html, body, name = self._github_file_raw("python_peps", api)
                except FetchError:
                    self._count_skip("skipped_no_section")
                    continue
            section = extract_named_sections(body, PEP_HEADINGS)
            if not section:
                self._count_skip("skipped_no_section")
                continue
            if len(section) > OLLAMA_BODY_CHAR_LIMIT:
                section = section[:OLLAMA_BODY_CHAR_LIMIT]
            docs.append(
                FetchedDocument(
                    source_code="python_peps",
                    collection_domain="software_dev",
                    source_url=html or entry.source_url,
                    source_title=entry.source_title or name,
                    body=section,
                    published_at=entry.published_at,
                )
            )
        _f2, d, s = self._ingest(run_id, "python_peps", docs)
        return fetched, d, s

    def _collect_md_rfc_dir(
        self,
        run_id: int,
        source_code: str,
        collection_domain: str,
        contents_url: str,
        headings: tuple[str, ...],
        skip_names: frozenset[str],
        require_section: bool,
    ) -> tuple[int, int, int]:
        items = self._github_contents(source_code, contents_url)
        files = [
            i
            for i in items
            if i.get("type") == "file"
            and str(i.get("name", "")).endswith(".md")
            and str(i.get("name", "")) not in skip_names
        ]
        files.sort(key=lambda i: str(i.get("name", "")), reverse=True)
        docs: list[FetchedDocument] = []
        fetched = 0
        for item in files[:SOURCE_INGEST_LIMIT]:
            fetched += 1
            api_url = item.get("url") or ""
            if not api_url:
                continue
            html, body, name = self._github_file_raw(source_code, api_url)
            section = extract_named_sections(body, headings)
            if require_section:
                if not section:
                    self._count_skip("skipped_no_section")
                    continue
                text = section
            else:
                text = section or body
            if not text.strip():
                self._count_skip("skipped_no_body")
                continue
            if len(text) > OLLAMA_BODY_CHAR_LIMIT:
                text = text[:OLLAMA_BODY_CHAR_LIMIT]
            docs.append(
                FetchedDocument(
                    source_code=source_code,
                    collection_domain=collection_domain,
                    source_url=html or item.get("html_url") or api_url,
                    source_title=item.get("name") or name,
                    body=text,
                    published_at=None,
                )
            )
        _f2, d, s = self._ingest(run_id, source_code, docs)
        return fetched, d, s

    def _collect_rust_rfcs(self, run_id: int) -> tuple[int, int, int]:
        return self._collect_md_rfc_dir(
            run_id,
            "github_rust_rfcs",
            "software_dev",
            "https://api.github.com/repos/rust-lang/rfcs/contents/text",
            RFC_HEADINGS,
            frozenset(),
            True,
        )

    def _collect_pytorch_rfcs(self, run_id: int) -> tuple[int, int, int]:
        return self._collect_md_rfc_dir(
            run_id,
            "github_pytorch_rfcs",
            "ai",
            "https://api.github.com/repos/pytorch/rfcs/contents",
            PYTORCH_HEADINGS,
            frozenset({"RFC-0000-template.md", "README.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md"}),
            True,
        )

    def _collect_tf_rfcs(self, run_id: int) -> tuple[int, int, int]:
        return self._collect_md_rfc_dir(
            run_id,
            "github_tf_rfcs",
            "ai",
            "https://api.github.com/repos/tensorflow/community/contents/rfcs",
            TF_HEADINGS,
            frozenset({".DS_Store"}),
            True,
        )

    def _collect_hf_blog(self, run_id: int) -> tuple[int, int, int]:
        return self._collect_md_rfc_dir(
            run_id,
            "github_hf_blog",
            "ai",
            "https://api.github.com/repos/huggingface/blog/contents",
            PYTORCH_HEADINGS,
            frozenset(),
            False,
        )

    def _collect_k8s_keps(self, run_id: int) -> tuple[int, int, int]:
        root = self._github_contents(
            "github_k8s_keps",
            "https://api.github.com/repos/kubernetes/enhancements/contents/keps",
        )
        docs: list[FetchedDocument] = []
        fetched = 0
        for entry in root:
            if fetched >= SOURCE_INGEST_LIMIT:
                break
            name = str(entry.get("name") or "")
            if name in ("NNNN-kep-template", "OWNERS"):
                continue
            if entry.get("type") != "dir":
                continue
            dir_url = entry.get("url")
            if not dir_url:
                continue
            try:
                children = self._github_contents("github_k8s_keps", dir_url)
            except FetchError:
                continue
            readmes = []
            for ch in children:
                n = str(ch.get("name") or "")
                if n.lower() == "readme.md" and ch.get("type") == "file":
                    readmes.append(ch)
                elif ch.get("type") == "dir" and fetched < SOURCE_INGEST_LIMIT:
                    nested_url = ch.get("url")
                    if not nested_url:
                        continue
                    try:
                        nested = self._github_contents("github_k8s_keps", nested_url)
                    except FetchError:
                        continue
                    for nch in nested:
                        if str(nch.get("name") or "").lower() == "readme.md" and nch.get("type") == "file":
                            readmes.append(nch)
            for rm in readmes:
                if fetched >= SOURCE_INGEST_LIMIT:
                    break
                fetched += 1
                api_url = rm.get("url") or ""
                if not api_url:
                    continue
                html, body, title = self._github_file_raw("github_k8s_keps", api_url)
                section = extract_named_sections(body, KEP_HEADINGS)
                if not section:
                    self._count_skip("skipped_no_section")
                    continue
                if len(section) > OLLAMA_BODY_CHAR_LIMIT:
                    section = section[:OLLAMA_BODY_CHAR_LIMIT]
                docs.append(
                    FetchedDocument(
                        source_code="github_k8s_keps",
                        collection_domain="software_dev",
                        source_url=html or rm.get("html_url") or api_url,
                        source_title=title or rm.get("path") or "KEP",
                        body=section,
                        published_at=None,
                    )
                )
        _f2, d, s = self._ingest(run_id, "github_k8s_keps", docs)
        return fetched, d, s

    def _collect_github_markdown_dirs(self, run_id: int, spec) -> tuple[int, int, int]:
        files: list[dict] = []
        for dir_path in spec.dir_paths:
            api = (
                f"https://api.github.com/repos/{spec.owner}/{spec.repo}/contents/{dir_path}"
            )
            try:
                items = self._github_contents(spec.source_code, api)
            except FetchError:
                continue
            for item in items:
                name = str(item.get("name") or "")
                if item.get("type") != "file":
                    continue
                if not any(name.endswith(ext) for ext in spec.extensions):
                    continue
                files.append(item)
        for extra in spec.extra_files:
            api = (
                f"https://api.github.com/repos/{spec.owner}/{spec.repo}/contents/{extra}"
            )
            try:
                extra_items = self._github_contents(spec.source_code, api)
            except FetchError:
                continue
            files.extend(extra_items)
        files.sort(key=lambda i: str(i.get("path") or i.get("name") or ""))
        docs: list[FetchedDocument] = []
        fetched = 0
        for item in files[:SOURCE_INGEST_LIMIT]:
            fetched += 1
            api_url = item.get("url") or ""
            if not api_url:
                continue
            html, body, name = self._github_file_raw(spec.source_code, api_url)
            section = extract_named_sections(body, ARCHITECTURE_HEADINGS)
            text = section or strip_yaml_frontmatter(body)
            if not text.strip():
                self._count_skip("skipped_no_body")
                continue
            if len(text) > OLLAMA_BODY_CHAR_LIMIT:
                text = text[:OLLAMA_BODY_CHAR_LIMIT]
            repo_path = str(item.get("path") or "")
            source_url = html or public_doc_url(
                repo_path, spec.public_base, spec.strip_prefix
            )
            title = markdown_title(body, name or item.get("name") or "")
            docs.append(
                FetchedDocument(
                    source_code=spec.source_code,
                    collection_domain=spec.collection_domain,
                    source_url=source_url,
                    source_title=title,
                    body=text,
                    published_at=None,
                )
            )
        _f2, d, s = self._ingest(run_id, spec.source_code, docs)
        return fetched, d, s

    def _collect_markdown_pages(
        self,
        run_id: int,
        source_code: str,
        collection_domain: str,
        pages: tuple[tuple[str, str, str], ...],
    ) -> tuple[int, int, int]:
        docs: list[FetchedDocument] = []
        fetched = 0
        for markdown_url, canonical_url, fallback_title in pages[:SOURCE_INGEST_LIMIT]:
            fetched += 1
            try:
                _status, text, _hdrs = self.fetch_context.get(source_code, markdown_url)
            except FetchError:
                self._count_skip("skipped_no_body")
                continue
            doc = parse_markdown_doc(
                text,
                source_code,
                collection_domain,
                canonical_url,
                fallback_title=fallback_title,
            )
            if doc is None:
                self._count_skip("skipped_no_body")
                continue
            if len(doc.body) > OLLAMA_BODY_CHAR_LIMIT:
                doc.body = doc.body[:OLLAMA_BODY_CHAR_LIMIT]
            docs.append(doc)
        _f2, d, s = self._ingest(run_id, source_code, docs)
        return fetched, d, s

    def _ingest(
        self, run_id: int, source_code: str, docs: list[FetchedDocument]
    ) -> tuple[int, int, int]:
        fetched = 0
        drafts = 0
        skipped = 0
        passed: list[FetchedDocument] = []
        for doc in docs:
            fetched += 1
            prepared = self._prepare_keyword_and_dup(doc)
            if prepared is None:
                skipped += 1
            else:
                passed.append(prepared)

        passed.sort(key=lambda d: published_sort_key(d.published_at), reverse=True)
        limit = int(self.config.get("SOURCE_INGEST_LIMIT") or SOURCE_INGEST_LIMIT)
        already = self._judged_this_run.get(source_code, 0)
        to_judge: list[FetchedDocument] = []
        for doc in passed:
            if already >= limit:
                self._count_skip("skipped_over_limit")
                skipped += 1
                continue
            to_judge.append(doc)
            already += 1
        self._judged_this_run[source_code] = already

        for doc in to_judge:
            reason = self._save_draft(run_id, doc)
            if reason is None:
                drafts += 1
            else:
                skipped += 1
        return fetched, drafts, skipped

    def _prepare_keyword_and_dup(self, doc: FetchedDocument) -> FetchedDocument | None:
        try:
            url = normalize_url(doc.source_url)
        except ValueError:
            self._count_skip("skipped_bad_url")
            return None
        body = nfc(redact_identity(doc.body or "")).strip()
        title = nfc(doc.source_title or "").strip()
        if not body or not title:
            self._count_skip("skipped_no_body")
            return None
        keywords = self.config.get("CONTRADICTION_KEYWORDS")
        if not looks_like_contradiction(title, body, keywords):
            self._count_skip("skipped_not_contradiction")
            return None
        sha = sha256_text(body)
        existing_url = self.conn.execute(
            "SELECT id FROM items WHERE source_url = ?", (url,)
        ).fetchone()
        if existing_url is not None:
            self._count_skip("duplicate_url")
            return None
        existing_sha = self.conn.execute(
            "SELECT id FROM items WHERE content_sha256 = ?", (sha,)
        ).fetchone()
        if existing_sha is not None:
            self.conn.execute(
                """
                INSERT INTO item_duplicates (kept_item_id, duplicate_url, duplicate_sha256, detected_at)
                VALUES (?, ?, ?, ?)
                """,
                (int(existing_sha["id"]), url, sha, utc_now()),
            )
            self.conn.commit()
            self._count_skip("duplicate_sha256")
            return None
        doc.source_url = url
        doc.source_title = title
        doc.body = body
        return doc

    def _save_draft(self, run_id: int, doc: FetchedDocument) -> str | None:
        sha = sha256_text(doc.body)
        existing_url = self.conn.execute(
            "SELECT id FROM items WHERE source_url = ?", (doc.source_url,)
        ).fetchone()
        if existing_url is not None:
            self._count_skip("duplicate_url")
            return "duplicate_url"
        existing_sha = self.conn.execute(
            "SELECT id FROM items WHERE content_sha256 = ?", (sha,)
        ).fetchone()
        if existing_sha is not None:
            self.conn.execute(
                """
                INSERT INTO item_duplicates (kept_item_id, duplicate_url, duplicate_sha256, detected_at)
                VALUES (?, ?, ?, ?)
                """,
                (int(existing_sha["id"]), doc.source_url, sha, utc_now()),
            )
            self.conn.commit()
            self._count_skip("duplicate_sha256")
            return "duplicate_sha256"
        fetched_at = utc_now()
        draft_in = DraftInput(
            collection_domain=doc.collection_domain,
            source_code=doc.source_code,
            source_url=doc.source_url,
            source_title=doc.source_title,
            body=doc.body,
            fetched_at=fetched_at,
            published_at=doc.published_at,
            content_sha256=sha,
            doi=doc.doi,
            language=doc.language if doc.language in ("ja", "en", "und") else "und",
        )
        result = self.draft_provider.draft(draft_in)
        if result.skip_reason:
            if result.skip_reason == "skipped_llm_stopped":
                self._record_error(run_id, doc.source_code, None, "LLM 連続失敗のため停止")
            self._count_skip(result.skip_reason)
            return result.skip_reason

        if not isinstance(self.draft_provider, NoneDraftProvider):
            tmp = {
                "goal": result.goal,
                "improve": result.improve,
                "worsens": result.worsens,
                "ideal": result.ideal,
                "constraints": result.constraints,
                "resources": result.resources,
                "evidence": result.evidence,
                "domain": result.domain,
                "source_url": doc.source_url,
                "fetched_at": fetched_at,
                "content_sha256": sha,
                "source_code": doc.source_code,
            }
            if not required_fields_filled(tmp) or not evidence_complete(tmp):
                self._count_skip("skipped_not_contradiction")
                return "skipped_not_contradiction"

        too_long = field_lengths_ok(
            {
                "goal": result.goal,
                "improve": result.improve,
                "worsens": result.worsens,
                "ideal": result.ideal,
                "physical": result.physical,
                "constraints": result.constraints,
                "resources": result.resources,
                "evidence": result.evidence,
            }
        )
        if too_long:
            self._count_skip("skipped_too_long")
            return "skipped_too_long"

        created = utc_now()
        try:
            cur = self.conn.execute(
                """
                INSERT INTO items (
                    collection_domain, domain, status,
                    goal, improve, worsens, ideal, physical, constraints, resources, evidence,
                    source_code, source_url, source_title, fetched_at, published_at,
                    content_sha256, language, doi, truncated, created_at
                ) VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc.collection_domain,
                    result.domain,
                    result.goal,
                    result.improve,
                    result.worsens,
                    result.ideal,
                    result.physical,
                    result.constraints,
                    result.resources,
                    result.evidence,
                    doc.source_code,
                    doc.source_url,
                    doc.source_title,
                    fetched_at,
                    doc.published_at,
                    sha,
                    draft_in.language,
                    doc.doi,
                    int(result.truncated),
                    created,
                ),
            )
            item_id = int(cur.lastrowid)
            self.conn.execute(
                "INSERT INTO item_bodies (item_id, body, stored_at) VALUES (?, ?, ?)",
                (item_id, doc.body, created),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            self.conn.rollback()
            self._count_skip("duplicate_race")
            return "duplicate_race"
        return None

    def _count_skip(self, reason: str) -> None:
        self._skip_counts[reason] = self._skip_counts.get(reason, 0) + 1

    def _record_error(
        self, run_id: int, source_code: str, http_status: int | None, message: str
    ) -> None:
        safe = _redact_secrets(message, self.config)
        self.conn.execute(
            """
            INSERT INTO collection_errors (run_id, source_code, occurred_at, http_status, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, source_code, utc_now(), http_status, safe),
        )
        self.conn.commit()

    def _purge_expired_bodies(self) -> None:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(
            days=self.config.get("BODY_RETENTION_DAYS", 7)
        )
        cutoff = cutoff_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        self.conn.execute(
            """
            DELETE FROM item_bodies
            WHERE item_id IN (
                SELECT id FROM items
                WHERE status IN ('approved', 'rejected')
                  AND (
                    (status = 'approved' AND approved_at IS NOT NULL AND approved_at <= ?)
                    OR (status = 'rejected' AND created_at <= ?)
                  )
            )
            """,
            (cutoff, cutoff),
        )
        self.conn.commit()


def format_skip_counts(counts: dict[str, int]) -> str:
    if not counts:
        return ""
    parts = [f"{k}={v}" for k, v in sorted(counts.items())]
    return "skip:" + " ".join(parts)


def parse_skip_counts(note: str | None) -> dict[str, int]:
    if not note:
        return {}
    text = note.strip()
    if text.startswith("skip:"):
        text = text[5:]
    out: dict[str, int] = {}
    for part in text.split():
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        try:
            out[key] = int(value)
        except ValueError:
            continue
    return out


def _redact_secrets(message: str, config: dict) -> str:
    text = message
    for key in ("GITHUB_TOKEN", "CONTACT_EMAIL"):
        secret = config.get(key) or ""
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def next_scheduled_utc(now: datetime | None = None, hour: int = 4, minute: int = 0) -> datetime:
    now = now or datetime.now(timezone.utc)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    return candidate
