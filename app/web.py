"""閲覧・承認 UI と API。認証なし。待ち受けは既定 127.0.0.1。"""

from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for

from app.collector import Collector, JobAlreadyRunning, next_scheduled_utc, parse_skip_counts
from app.config import FIELD_MAX, JOB_HISTORY_LIMIT, load_config
from app.db import ensure_db
from app.draft import create_draft_provider
from app.export import (
    dumps_export,
    evidence_complete,
    export_filename,
    exportable,
    required_fields_filled,
    row_to_problem,
    wrap_export,
)
from app.fetch import FetchContext, HttpClient, HttpxClient, RobotsCache
from app.sebas import push_triz_items
from app.queries import (
    counts,
    get_body,
    get_item,
    latest_run,
    list_items,
    recent_errors,
    recent_runs,
    row_to_dict,
)
from app.textutil import (
    build_evidence,
    evidence_license,
    field_lengths_ok,
    nfc,
    normalize_physical,
    split_evidence,
    utc_now,
)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


def create_app(
    config: dict | None = None,
    conn: sqlite3.Connection | None = None,
    http_client: HttpClient | None = None,
    collector: Collector | None = None,
    start_scheduler: bool = False,
    sleeper=None,
) -> Flask:
    cfg = config or load_config()
    database_path = cfg["DATABASE_PATH"]
    if conn is None:
        conn = ensure_db(database_path)
    if http_client is None:
        http_client = HttpxClient(
            user_agent=cfg["USER_AGENT"],
            timeout=float(cfg["HTTP_TIMEOUT_SEC"]),
            token=cfg.get("GITHUB_TOKEN") or "",
        )
    fetch_context = FetchContext(
        client=http_client,
        user_agent=cfg["USER_AGENT"],
        contact_email=cfg.get("CONTACT_EMAIL") or "",
        github_token=cfg.get("GITHUB_TOKEN") or "",
        robots=RobotsCache(),
        sleeper=sleeper,
    )
    if collector is None:
        collector = Collector(
            conn=conn,
            config=cfg,
            fetch_context=fetch_context,
            draft_provider=create_draft_provider(cfg),
        )

    app = Flask(__name__, template_folder=str(TEMPLATE_DIR))
    app.config["APP_CONFIG"] = cfg
    app.collector = collector
    app.db = conn
    stop_event = threading.Event()
    app.scheduler_stop = stop_event

    def _dashboard_payload() -> dict:
        stats = counts(conn)
        last = latest_run(conn)
        nxt = next_scheduled_utc(
            hour=int(cfg["SCHEDULE_HOUR_UTC"]),
            minute=int(cfg["SCHEDULE_MINUTE_UTC"]),
        )
        progress = collector.progress()
        last_finished = None
        last_status = None
        last_skipped = 0
        skip_counts: dict = {}
        if last is not None:
            last_finished = last["finished_at"]
            last_status = last["status"]
            last_skipped = last["skipped_count"] or 0
            skip_counts = parse_skip_counts(last["note"])
        return {
            "stats": stats,
            "last_finished_at": last_finished or "未実行",
            "last_status": last_status,
            "last_skipped": last_skipped,
            "skip_counts": skip_counts,
            "runs": [row_to_dict(r) for r in recent_runs(conn, JOB_HISTORY_LIMIT)],
            "errors": [row_to_dict(e) for e in recent_errors(conn, 20)],
            "progress": progress,
            "schedule": "1 日 1 回（仮: 04:00 UTC）",
            "next_scheduled_at": nxt.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "draft_provider": cfg.get("DRAFT_PROVIDER") or "none",
        }

    @app.get("/")
    def dashboard():
        return render_template("dashboard.html", **_dashboard_payload())

    @app.get("/items")
    def items_page():
        collection_domain = request.args.get("collection_domain") or None
        domain = request.args.get("domain") or None
        status = request.args.get("status") or None
        fetched_on = request.args.get("fetched_on") or None
        rows = list_items(
            conn,
            collection_domain=collection_domain,
            domain=domain,
            status=status,
            fetched_on=fetched_on,
        )
        return render_template(
            "items.html",
            items=[row_to_dict(r) for r in rows],
            collection_domain=collection_domain or "",
            domain=domain or "",
            status=status or "",
            fetched_on=fetched_on or "",
            message=request.args.get("message") or "",
        )

    @app.get("/items/<int:item_id>")
    def item_detail(item_id: int):
        row = get_item(conn, item_id)
        if row is None:
            abort(404)
        parsed = split_evidence(row["evidence"])
        reason = parsed[3] if parsed else ""
        body = get_body(conn, item_id)
        return render_template(
            "item_detail.html",
            item=row_to_dict(row),
            evidence_reason=reason,
            body=body,
            field_max=FIELD_MAX,
            error=request.args.get("error") or "",
            message=request.args.get("message") or "",
        )

    @app.post("/collect")
    def collect_post():
        if collector.is_running():
            return redirect(url_for("dashboard"))
        thread = threading.Thread(target=_run_job, args=(collector, "manual"), daemon=True)
        thread.start()
        return redirect(url_for("dashboard"))

    @app.post("/items/<int:item_id>/save")
    def save_item(item_id: int):
        error = _apply_edit(conn, item_id, request.form)
        if error:
            return redirect(url_for("item_detail", item_id=item_id, error=error))
        return redirect(url_for("item_detail", item_id=item_id, message="保存した"))

    @app.post("/items/<int:item_id>/approve")
    def approve_item(item_id: int):
        error = _apply_edit(conn, item_id, request.form, approve=True)
        if error:
            return redirect(url_for("item_detail", item_id=item_id, error=error))
        return redirect(url_for("item_detail", item_id=item_id, message="承認した"))

    @app.post("/items/bulk-approve")
    def bulk_approve_items():
        item_ids = list(dict.fromkeys(
            int(value) for value in request.form.getlist("item_ids") if value.isdigit()
        ))
        errors = Counter()
        approved = 0
        for item_id in item_ids:
            error = _apply_edit(conn, item_id, {}, approve=True)
            if error:
                errors[error] += 1
            else:
                approved += 1
        held = sum(errors.values())
        message = f"{approved}件承認、{held}件は保留"
        if errors:
            details = "、".join(f"{reason}: {count}件" for reason, count in errors.items())
            message += f"（{details}）"
        return redirect(url_for(
            "items_page",
            collection_domain=request.form.get("collection_domain") or None,
            domain=request.form.get("domain") or None,
            status=request.form.get("status") or None,
            fetched_on=request.form.get("fetched_on") or None,
            message=message,
        ))

    @app.post("/items/push-to-sebas")
    def push_to_sebas():
        filters = {
            "collection_domain": request.form.get("collection_domain") or None,
            "domain": request.form.get("domain") or None,
            "status": request.form.get("status") or None,
            "fetched_on": request.form.get("fetched_on") or None,
        }
        base_url = (cfg.get("SEBAS_BASE_URL") or "").strip()
        if not base_url:
            return redirect(url_for("items_page", **filters,
                                    message="セバスの接続先が設定されていません"))
        item_ids = list(dict.fromkeys(
            int(value) for value in request.form.getlist("item_ids") if value.isdigit()
        ))
        selected = list_items(conn, ids=item_ids) if item_ids else []
        skipped = _classify_sebas_skips(selected)
        targets = [
            row for row in selected
            if row["status"] == "approved" and row["sebas_pushed_at"] is None
        ]
        result = push_triz_items(
            conn, targets, base_url, cfg.get("SEBAS_PROJECT_ID") or "default",
            float(cfg.get("HTTP_TIMEOUT_SEC", 30)), sleeper=lambda seconds: sleeper(seconds) if sleeper else __import__("time").sleep(seconds),
        )
        message = _sebas_message(result, skipped)
        return redirect(url_for("items_page", **filters, message=message))

    @app.post("/items/<int:item_id>/reject")
    def reject_item(item_id: int):
        error = _reject(conn, item_id, request.form.get("reject_reason") or "")
        if error:
            return redirect(url_for("item_detail", item_id=item_id, error=error))
        return redirect(url_for("item_detail", item_id=item_id, message="却下した"))

    @app.post("/items/<int:item_id>/unapprove")
    def unapprove_item(item_id: int):
        error = _unapprove(conn, item_id)
        if error:
            return redirect(url_for("item_detail", item_id=item_id, error=error))
        return redirect(url_for("item_detail", item_id=item_id, message="下書きに戻した"))

    @app.post("/export")
    def export_post():
        raw_ids = request.form.getlist("ids") or request.form.getlist("item_ids")
        ids = [int(x) for x in raw_ids if str(x).isdigit()]
        collection_domain = request.form.get("collection_domain") or None
        domain = request.form.get("domain") or None
        status = request.form.get("status") or None
        fetched_on = request.form.get("fetched_on") or None
        if ids:
            rows = list_items(conn, ids=ids)
        else:
            rows = list_items(
                conn,
                collection_domain=collection_domain,
                domain=domain,
                status=status,
                fetched_on=fetched_on,
            )
        selected = [row_to_dict(r) for r in rows]
        exported = [row_to_problem(r) for r in selected if exportable(r)]
        skipped = len(selected) - len(exported)
        payload = wrap_export(exported)
        data = dumps_export(payload)
        filename = export_filename()
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Export-Count": str(len(exported)),
            "X-Export-Skipped": str(skipped),
        }
        return Response(data, mimetype="application/json; charset=utf-8", headers=headers)

    @app.get("/api/status")
    def api_status():
        return jsonify(_dashboard_payload())

    @app.get("/api/items")
    def api_items():
        rows = list_items(
            conn,
            collection_domain=request.args.get("collection_domain") or None,
            domain=request.args.get("domain") or None,
            status=request.args.get("status") or None,
            fetched_on=request.args.get("fetched_on") or None,
        )
        return jsonify({"items": [row_to_dict(r) for r in rows]})

    @app.get("/api/items/<int:item_id>")
    def api_item(item_id: int):
        row = get_item(conn, item_id)
        if row is None:
            return jsonify({"error": "not found"}), 404
        data = row_to_dict(row)
        data["body"] = get_body(conn, item_id)
        return jsonify(data)

    @app.post("/api/collect")
    def api_collect():
        if collector.is_running():
            return jsonify({"error": "running"}), 409
        try:
            run_id = collector.run("manual")
        except JobAlreadyRunning:
            return jsonify({"error": "running"}), 409
        return jsonify({"run_id": run_id}), 202

    @app.post("/api/items/<int:item_id>")
    def api_edit(item_id: int):
        payload = request.get_json(silent=True) or {}
        error = _apply_edit(conn, item_id, payload)
        if error:
            return jsonify({"error": error}), 400
        return jsonify(row_to_dict(get_item(conn, item_id)))

    @app.post("/api/items/<int:item_id>/approve")
    def api_approve(item_id: int):
        payload = request.get_json(silent=True) or {}
        error = _apply_edit(conn, item_id, payload, approve=True)
        if error:
            return jsonify({"error": error}), 400
        return jsonify(row_to_dict(get_item(conn, item_id)))

    @app.post("/api/items/<int:item_id>/reject")
    def api_reject(item_id: int):
        payload = request.get_json(silent=True) or {}
        error = _reject(conn, item_id, payload.get("reject_reason") or "")
        if error:
            return jsonify({"error": error}), 400
        return jsonify(row_to_dict(get_item(conn, item_id)))

    @app.get("/api/export")
    def api_export():
        raw = request.args.get("ids") or ""
        ids = [int(x) for x in raw.split(",") if x.strip().isdigit()]
        if ids:
            rows = list_items(conn, ids=ids)
        else:
            rows = list_items(
                conn,
                collection_domain=request.args.get("collection_domain") or None,
                domain=request.args.get("domain") or None,
                status=request.args.get("status") or None,
                fetched_on=request.args.get("fetched_on") or None,
            )
        selected = [row_to_dict(r) for r in rows]
        exported = [row_to_problem(r) for r in selected if exportable(r)]
        payload = wrap_export(exported)
        return jsonify(
            {
                "skipped_not_approved": len(selected) - len(exported),
                "export": payload,
            }
        )

    if start_scheduler:
        thread = threading.Thread(
            target=_scheduler_loop,
            args=(collector, stop_event, int(cfg["SCHEDULE_HOUR_UTC"]), int(cfg["SCHEDULE_MINUTE_UTC"])),
            daemon=True,
        )
        thread.start()

    return app


def _classify_sebas_skips(rows) -> dict[str, int]:
    return {
        "pushed": sum(row["sebas_pushed_at"] is not None for row in rows),
        "rejected": sum(
            row["sebas_pushed_at"] is None and row["status"] == "rejected" for row in rows
        ),
        "draft": sum(
            row["sebas_pushed_at"] is None and row["status"] == "draft" for row in rows
        ),
    }


def _sebas_message(result: dict, skipped: dict[str, int]) -> str:
    message = (f"{result['sent']}件送信、{result['duplicate']}件は重複、"
               f"{result['failed']}件失敗")
    if result["reasons"]:
        details = "、".join(f"{reason}: {count}件" for reason, count in result["reasons"].items())
        message += f"（失敗理由: {details}）"
    message += (f"（送信対象外: 送信済み{skipped['pushed']}件、"
                f"却下済み{skipped['rejected']}件、"
                f"未承認（下書き）{skipped['draft']}件はスキップ）")
    return message


def _run_job(collector: Collector, trigger: str) -> None:
    try:
        collector.run(trigger)
    except JobAlreadyRunning:
        return


def _scheduler_loop(collector: Collector, stop_event: threading.Event, hour: int, minute: int) -> None:
    while not stop_event.is_set():
        nxt = next_scheduled_utc(hour=hour, minute=minute)
        remaining = (nxt - datetime.now(timezone.utc)).total_seconds()
        wait_for = 60 if remaining > 60 else max(remaining, 0.5)
        if stop_event.wait(wait_for):
            return
        if datetime.now(timezone.utc) >= nxt:
            try:
                collector.run("scheduled")
            except JobAlreadyRunning:
                pass
            stop_event.wait(61)


def _form_get(data, key: str, default: str = "") -> str:
    value = data.get(key) if hasattr(data, "get") else None
    if value is None:
        return default
    return nfc(str(value))


def _apply_edit(conn: sqlite3.Connection, item_id: int, data, approve: bool = False) -> str | None:
    row = get_item(conn, item_id)
    if row is None:
        return "not found"
    if row["status"] == "rejected" and approve:
        return "却下済みは承認できない"
    domain = _form_get(data, "domain", row["domain"])
    if domain not in ("software", "workflow", "research"):
        return "domain が不正"
    goal = _form_get(data, "goal", row["goal"])
    improve = _form_get(data, "improve", row["improve"])
    worsens = _form_get(data, "worsens", row["worsens"])
    ideal = _form_get(data, "ideal", row["ideal"])
    physical_raw = data.get("physical") if hasattr(data, "get") else None
    if physical_raw is None:
        physical = row["physical"]
    else:
        physical = normalize_physical(physical_raw)
        if physical is not None:
            physical = nfc(physical)
    constraints = _form_get(data, "constraints", row["constraints"])
    resources = _form_get(data, "resources", row["resources"])
    parsed = split_evidence(row["evidence"])
    current_reason = parsed[3] if parsed else ""
    reason = _form_get(data, "evidence_reason", current_reason)
    # 編集・承認で証拠欄を作り直しても、ライセンス行(CC BY-SA の出典表示義務)を落とさない。
    evidence = build_evidence(
        row["source_url"], row["fetched_at"], row["content_sha256"], reason,
        license_label=evidence_license(row["evidence"]),
    )

    fields = {
        "goal": goal,
        "improve": improve,
        "worsens": worsens,
        "ideal": ideal,
        "physical": physical,
        "constraints": constraints,
        "resources": resources,
        "evidence": evidence,
    }
    over = field_lengths_ok(fields)
    if over:
        return "字数が 2,500 を超えている: " + ",".join(over)

    require_complete = approve or row["status"] == "approved"
    if require_complete:
        candidate = {
            "domain": domain,
            "goal": goal,
            "improve": improve,
            "worsens": worsens,
            "ideal": ideal,
            "constraints": constraints,
            "resources": resources,
            "evidence": evidence,
            "source_url": row["source_url"],
            "fetched_at": row["fetched_at"],
            "content_sha256": row["content_sha256"],
            "status": "approved",
        }
        if not required_fields_filled(candidate) or not evidence_complete(candidate):
            return "必須フィールドまたは evidence メタが不足"

    status = "approved" if approve else row["status"]
    if status == "rejected" and not approve:
        status = "draft"
    approved_at = utc_now() if approve else row["approved_at"]
    conn.execute(
        """
        UPDATE items SET
            domain = ?, goal = ?, improve = ?, worsens = ?, ideal = ?,
            physical = ?, constraints = ?, resources = ?, evidence = ?,
            status = ?, approved_at = ?,
            reject_reason = CASE WHEN ? = 'approved' THEN NULL ELSE reject_reason END
        WHERE id = ?
        """,
        (
            domain,
            goal,
            improve,
            worsens,
            ideal,
            physical,
            constraints,
            resources,
            evidence,
            status,
            approved_at,
            status,
            item_id,
        ),
    )
    conn.commit()
    return None


def _reject(conn: sqlite3.Connection, item_id: int, reason: str) -> str | None:
    row = get_item(conn, item_id)
    if row is None:
        return "not found"
    conn.execute(
        """
        UPDATE items SET status = 'rejected', reject_reason = ?, approved_at = NULL
        WHERE id = ?
        """,
        (nfc(reason) or None, item_id),
    )
    conn.commit()
    return None


def _unapprove(conn: sqlite3.Connection, item_id: int) -> str | None:
    row = get_item(conn, item_id)
    if row is None:
        return "not found"
    if row["status"] != "approved":
        return "承認済みではない"
    conn.execute(
        "UPDATE items SET status = 'draft', approved_at = NULL WHERE id = ?",
        (item_id,),
    )
    conn.commit()
    return None


def main() -> None:
    cfg = load_config()
    app = create_app(cfg, start_scheduler=True)
    app.run(host=cfg["BIND_HOST"], port=int(cfg["PORT"]), debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
