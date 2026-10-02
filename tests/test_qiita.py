import json
import sqlite3
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

from app.collector import Collector
from app.config import QIITA_SOURCE_CODES, QIITA_SOURCES, load_config
from app.db import init_db
from app.fetch import (
    FetchContext,
    QIITA_MIN_INTERVAL_SEC,
    RecordingClient,
    RobotsCache,
    parse_qiita_items,
    source_interval,
)


class DummyDraftProvider:
    pass


class QiitaTest(unittest.TestCase):
    def test_parse_qiita_items_ignores_incomplete_articles(self):
        payload = [
            {
                "url": "https://qiita.invalid/items/valid",
                "title": "架空の障害対応",
                "body": "性能と費用のトレードオフを調査した架空の記事本文",
                "created_at": "2026-01-01T12:34:56+09:00",
            },
            {"url": "", "title": "URLなし", "body": "本文"},
            {"url": "https://qiita.invalid/items/no-title", "title": "", "body": "本文"},
            {"url": "https://qiita.invalid/items/no-body", "title": "本文なし", "body": ""},
            "not-an-object",
        ]
        docs = parse_qiita_items(json.dumps(payload), "qiita_troubleshooting", "software_dev")
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].source_url, "https://qiita.invalid/items/valid")
        self.assertEqual(docs[0].source_title, "架空の障害対応")
        self.assertIn("トレードオフ", docs[0].body)
        self.assertEqual(docs[0].language, "ja")
        self.assertEqual(docs[0].published_at, "2026-01-01T03:34:56Z")

    def test_source_plan_contains_all_four_qiita_sources(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        context = FetchContext(
            client=RecordingClient(), user_agent="test", contact_email="",
            github_token="", robots=RobotsCache(), sleeper=lambda _seconds: None,
        )
        collector = Collector(conn, load_config({"DATABASE_PATH": ":memory:"}), context,
                              draft_provider=DummyDraftProvider())
        plan_codes = {code for code, _worker in collector._source_plan()}
        self.assertEqual(len(QIITA_SOURCES), 4)
        self.assertEqual(QIITA_SOURCE_CODES, {
            "qiita_troubleshooting", "qiita_performance_tuning",
            "qiita_architecture_design", "qiita_refactoring",
        })
        self.assertTrue(QIITA_SOURCE_CODES.issubset(plan_codes))
        conn.close()

    def test_qiita_sources_share_six_second_rate_limiter(self):
        for code in QIITA_SOURCE_CODES:
            self.assertEqual(source_interval(code, False), QIITA_MIN_INTERVAL_SEC)
            self.assertEqual(source_interval(code, True), QIITA_MIN_INTERVAL_SEC)
        context = FetchContext(
            client=RecordingClient(), user_agent="test", contact_email="",
            github_token="", robots=RobotsCache(), sleeper=lambda _seconds: None,
        )
        first = context.limiter_for("qiita_troubleshooting")
        second = context.limiter_for("qiita_refactoring")
        self.assertIs(first, second)
        self.assertEqual(first.min_interval_sec, 6.0)


if __name__ == "__main__":
    unittest.main()
