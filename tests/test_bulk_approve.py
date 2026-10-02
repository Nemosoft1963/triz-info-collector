import sqlite3
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
for name in list(sys.modules):
    if name == "app" or name.startswith("app."):
        del sys.modules[name]
sys.path.insert(0, str(APP_DIR))

from app.config import load_config
from app.db import init_db
from app.textutil import build_evidence
from app.web import create_app


class DummyCollector:
    def is_running(self):
        return False

    def progress(self):
        return {"running": False}


class BulkApproveTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        init_db(self.conn)
        config = load_config({"DATABASE_PATH": ":memory:"})
        self.app = create_app(config=config, conn=self.conn, collector=DummyCollector())
        self.app.testing = True
        self.client = self.app.test_client()
        self.serial = 0

    def tearDown(self):
        self.conn.close()

    @classmethod
    def tearDownClass(cls):
        if str(APP_DIR) in sys.path:
            sys.path.remove(str(APP_DIR))
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]

    def add_item(self, status="draft", valid=True):
        self.serial += 1
        url = f"https://example.invalid/problem-{self.serial}"
        fetched = "2026-01-01T00:00:00Z"
        sha = f"{self.serial:064x}"
        evidence = build_evidence(url, fetched, sha, "架空データによる根拠")
        goal = "架空システムを改善する" if valid else ""
        cur = self.conn.execute(
            """INSERT INTO items (
                collection_domain, domain, status, goal, improve, worsens, ideal,
                physical, constraints, resources, evidence, source_code, source_url,
                source_title, fetched_at, content_sha256, language, created_at, reject_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("software_dev", "software", status, goal, "速度を上げる",
             "費用が増える", "費用を増やさず高速化する", None, "架空の制約",
             "架空の計算資源", evidence, "k8s_blog_rss", url, "架空記事", fetched,
             sha, "ja", fetched, "既に却下" if status == "rejected" else None),
        )
        self.conn.commit()
        return cur.lastrowid

    def statuses(self, ids):
        return [self.conn.execute("SELECT status FROM items WHERE id = ?", (i,)).fetchone()[0] for i in ids]

    def post(self, ids):
        return self.client.post("/items/bulk-approve", data={"item_ids": [str(i) for i in ids]})

    def test_approves_multiple_valid_drafts(self):
        ids = [self.add_item(), self.add_item()]
        response = self.post(ids)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.statuses(ids), ["approved", "approved"])

    def test_invalid_draft_stays_draft_while_valid_item_is_approved(self):
        valid_id = self.add_item()
        invalid_id = self.add_item(valid=False)
        response = self.post([valid_id, invalid_id])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.statuses([valid_id, invalid_id]), ["approved", "draft"])
        self.assertIn("%E5%BF%85%E9%A0%88%E3%83%95%E3%82%A3%E3%83%BC%E3%83%AB%E3%83%89", response.location)

    def test_rejected_item_is_not_approved(self):
        valid_id = self.add_item()
        rejected_id = self.add_item(status="rejected")
        self.post([valid_id, rejected_id])
        self.assertEqual(self.statuses([valid_id, rejected_id]), ["approved", "rejected"])

    def test_empty_selection_changes_nothing(self):
        ids = [self.add_item(), self.add_item(status="rejected")]
        response = self.client.post("/items/bulk-approve", data={})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.statuses(ids), ["draft", "rejected"])
        self.assertIn("0%E4%BB%B6", response.location)


if __name__ == "__main__":
    unittest.main()
