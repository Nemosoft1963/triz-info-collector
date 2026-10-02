"""UI / API 用の読み取り。"""

from __future__ import annotations

import sqlite3

from app.collector import parse_skip_counts
from app.config import JOB_HISTORY_LIMIT


def counts(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
    by_status = {row["status"]: row["n"] for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM items GROUP BY status"
    )}
    by_collection = {}
    for row in conn.execute(
        """
        SELECT collection_domain, status, COUNT(*) AS n
        FROM items GROUP BY collection_domain, status
        """
    ):
        by_collection.setdefault(row["collection_domain"], {})[row["status"]] = row["n"]
        by_collection[row["collection_domain"]]["total"] = (
            by_collection[row["collection_domain"]].get("total", 0) + row["n"]
        )
    approved_domain = {
        row["domain"]: row["n"]
        for row in conn.execute(
            """
            SELECT domain, COUNT(*) AS n FROM items
            WHERE status = 'approved' GROUP BY domain
            """
        )
    }
    return {
        "total": total,
        "by_status": {
            "draft": by_status.get("draft", 0),
            "approved": by_status.get("approved", 0),
            "rejected": by_status.get("rejected", 0),
        },
        "by_collection_domain": by_collection,
        "approved_by_domain": {
            "software": approved_domain.get("software", 0),
            "workflow": approved_domain.get("workflow", 0),
            "research": approved_domain.get("research", 0),
        },
    }


def latest_run(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT * FROM collection_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()


def recent_runs(conn: sqlite3.Connection, limit: int = JOB_HISTORY_LIMIT):
    return conn.execute(
        "SELECT * FROM collection_runs ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def recent_errors(conn: sqlite3.Connection, limit: int = 20):
    return conn.execute(
        """
        SELECT * FROM collection_errors
        ORDER BY occurred_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def list_items(
    conn: sqlite3.Connection,
    collection_domain: str | None = None,
    domain: str | None = None,
    status: str | None = None,
    fetched_on: str | None = None,
    ids: list[int] | None = None,
):
    sql = "SELECT * FROM items WHERE 1=1"
    args: list = []
    if collection_domain:
        sql += " AND collection_domain = ?"
        args.append(collection_domain)
    if domain:
        sql += " AND domain = ?"
        args.append(domain)
    if status:
        sql += " AND status = ?"
        args.append(status)
    if fetched_on:
        sql += " AND substr(fetched_at, 1, 10) = ?"
        args.append(fetched_on)
    if ids:
        placeholders = ",".join("?" * len(ids))
        sql += f" AND id IN ({placeholders})"
        args.extend(ids)
    sql += " ORDER BY fetched_at DESC, id DESC"
    return conn.execute(sql, args).fetchall()


def get_item(conn: sqlite3.Connection, item_id: int):
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return row


def get_body(conn: sqlite3.Connection, item_id: int) -> str | None:
    row = conn.execute(
        "SELECT body FROM item_bodies WHERE item_id = ?", (item_id,)
    ).fetchone()
    return row["body"] if row else None


def row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


def skip_breakdown(note: str | None) -> dict[str, int]:
    return parse_skip_counts(note)
