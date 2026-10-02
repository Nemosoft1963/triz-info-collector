import json
import sqlite3
import unittest
from unittest.mock import patch
from urllib.parse import unquote
from pathlib import Path
import sys

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))
from app.config import load_config
from app.db import init_db
from app.web import create_app

class DummyCollector:
    def is_running(self): return False
    def progress(self): return {"running": False}
class Response:
    def __init__(self, data): self.data=data
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def read(self): return json.dumps(self.data).encode()

class SebasPushTest(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(":memory:",check_same_thread=False); self.conn.row_factory=sqlite3.Row
        init_db(self.conn)
        cfg=load_config({"DATABASE_PATH":":memory:","SEBAS_BASE_URL":"https://sebas.invalid","SEBAS_PROJECT_ID":"project"})
        self.app=create_app(cfg,self.conn,collector=DummyCollector(),sleeper=lambda _:None); self.app.testing=True
        self.client=self.app.test_client()
    def add(self,status,pushed=False):
        n=self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]+1
        evidence = (f"出典URL: https://source.invalid/{n}\n"
                    "取得日時: 2026-01-01T00:00:00Z\n"
                    "根拠引用: 架空データによる根拠")
        self.conn.execute("""INSERT INTO items (collection_domain,domain,status,goal,improve,worsens,ideal,physical,constraints,resources,evidence,source_code,source_url,source_title,fetched_at,content_sha256,language,created_at) VALUES ('software_dev','software',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (status,"goal","improve","worsens","ideal",None,"constraints","resources",evidence,"source",f"https://source.invalid/{n}","title","2026-01-01T00:00:00Z",f"{n:064x}","ja","2026-01-01T00:00:00Z"))
        item_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        if pushed:
            self.conn.execute(
                "UPDATE items SET sebas_pushed_at='2026-01-02T00:00:00Z' WHERE id=?",
                (item_id,),
            )
        self.conn.commit(); return item_id
    def test_success_skips_unapproved_and_updates_timestamp(self):
        approved,draft=self.add("approved"),self.add("draft"); calls=[]
        def fake(req,timeout=0):
            calls.append((req.method,req.full_url,None if req.data is None else json.loads(req.data)))
            if req.full_url.endswith("/agent-examples"): return Response({"id":"e1"})
            if req.method=="GET": return Response({"revision":3,"sessions":[]})
            return Response({"ok":True})
        with patch("app.sebas.request.urlopen",side_effect=fake):
            response=self.client.post("/items/push-to-sebas",data={"item_ids":[approved,draft]})
        self.assertEqual(calls[0][2]["agent"],"triz-info-collector-push")
        self.assertEqual(calls[2][2]["revision"],3)
        values = calls[2][2]["values"]
        self.assertEqual(values["domain"], "software")
        self.assertTrue(values["human_checks"])
        self.assertLessEqual(len(values["human_checks"]), 3000)
        expected_evidence = ("出典URL: https://source.invalid/1\n"
                             "取得日時: 2026-01-01T00:00:00Z\n"
                             "根拠引用: 架空データによる根拠")
        self.assertEqual(values["evidence"], expected_evidence)
        self.assertNotEqual(values["evidence"], "https://source.invalid/1")
        self.assertIn("未承認（下書き）1件はスキップ",unquote(response.location))
        self.assertIsNotNone(self.conn.execute("SELECT sebas_pushed_at FROM items WHERE id=?",(approved,)).fetchone()[0])
    def test_only_unpushed_approved_items_are_sent_and_skip_reasons_are_split(self):
        target = self.add("approved")
        pushed = self.add("approved", pushed=True)
        rejected = self.add("rejected")
        draft = self.add("draft")
        calls = []
        def fake(req,timeout=0):
            calls.append((req.method, req.full_url,
                          None if req.data is None else json.loads(req.data)))
            if req.full_url.endswith("/agent-examples"):
                return Response({"id":"e1"})
            if req.method == "GET":
                return Response({"revision":1,"sessions":[]})
            return Response({"ok":True})
        with patch("app.sebas.request.urlopen", side_effect=fake):
            response = self.client.post(
                "/items/push-to-sebas",
                data={"item_ids":[target, pushed, rejected, draft]},
            )
        source_items = json.loads(calls[0][2]["source"])
        self.assertEqual(len(source_items), 1)
        self.assertIn("https://source.invalid/1", source_items[0]["evidence"])
        post_values = [call[2]["values"] for call in calls
                       if call[0] == "POST" and call[2] and "values" in call[2]]
        self.assertEqual(len(post_values), 1)
        message = unquote(response.location)
        self.assertIn("送信済み1件", message)
        self.assertIn("却下済み1件", message)
        self.assertIn("未承認（下書き）1件はスキップ", message)

    def test_only_excluded_items_do_not_call_api(self):
        ids = [self.add("approved", pushed=True), self.add("rejected"), self.add("draft")]
        with patch("app.sebas.request.urlopen") as send:
            response = self.client.post("/items/push-to-sebas", data={"item_ids":ids})
        send.assert_not_called()
        message = unquote(response.location)
        self.assertIn("送信済み1件", message)
        self.assertIn("却下済み1件", message)
        self.assertIn("未承認（下書き）1件", message)

    def test_duplicate_is_reported(self):
        item=self.add("approved")
        existing_evidence = ("出典URL: https://source.invalid/1\n"
                             "取得日時: 2025-12-31T00:00:00Z\n"
                             "根拠引用: 既存の架空データ")
        replies=[Response({"id":"e1"}),Response({"revision":1,"sessions":[{"problem":{"evidence":existing_evidence}}]})]
        with patch("app.sebas.request.urlopen",side_effect=replies):
            response=self.client.post("/items/push-to-sebas",data={"item_ids":[item]})
        self.assertIn("1件は重複",unquote(response.location))
    def test_unconfigured_does_not_send(self):
        self.app.config["APP_CONFIG"]["SEBAS_BASE_URL"]=""
        with patch("app.sebas.request.urlopen") as send: response=self.client.post("/items/push-to-sebas")
        send.assert_not_called(); self.assertIn("接続先が設定されていません",unquote(response.location))

if __name__=="__main__": unittest.main()
