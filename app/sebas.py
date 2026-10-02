"""承認済みTRIZ課題をセバスへ送信する。"""
from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime, timezone
from urllib import error, parse, request


def _json_request(url: str, method: str = "GET", payload: dict | None = None, timeout: float = 30) -> dict:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, data=body, headers={"Content-Type": "application/json"}, method=method)
    with request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def push_triz_items(conn, rows, base_url: str, project_id: str, timeout: float = 30, sleeper=time.sleep) -> dict:
    result = {"sent": 0, "duplicate": 0, "failed": 0, "reasons": Counter()}
    root = base_url.rstrip("/") + "/api/projects/" + parse.quote(project_id, safe="")
    if not rows:
        return result
    source_items = [_values(row) for row in rows]
    filename = "triz-info-collector-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json"
    try:
        created = _json_request(root + "/agent-examples", "POST", {
            "filename": filename, "source": json.dumps(source_items, ensure_ascii=False),
            "agent": "triz-info-collector-push"}, timeout)
        eid = str(created["id"])
    except Exception as exc:
        result["failed"] = len(rows)
        result["reasons"][_error_text(exc)] += len(rows)
        return result
    endpoint = root + "/agent-examples/" + parse.quote(eid, safe="") + "/learning/general"
    for position, row in enumerate(rows):
        try:
            state = _json_request(endpoint, timeout=timeout)
            if _has_source(state, row["source_url"]):
                result["duplicate"] += 1
                continue
            for attempt in range(20):
                try:
                    _json_request(endpoint, "POST", {"revision": state.get("revision"),
                                                      "values": _values(row)}, timeout)
                    result["sent"] += 1
                    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                    conn.execute("UPDATE items SET sebas_pushed_at = ? WHERE id = ?", (now, row["id"]))
                    conn.commit()
                    break
                except error.HTTPError as exc:
                    if exc.code == 409 and attempt < 19:
                        sleeper(0.1)
                        state = _json_request(endpoint, timeout=timeout)
                        if _has_source(state, row["source_url"]):
                            result["duplicate"] += 1
                            break
                        continue
                    raise
            else:
                raise RuntimeError("HTTP 409 再試行上限")
        except Exception as exc:
            remaining = len(rows) - position
            if _is_transport_or_server(exc):
                result["failed"] += remaining
                result["reasons"][_error_text(exc)] += remaining
                break
            result["failed"] += 1
            result["reasons"][_error_text(exc)] += 1
    return result


HUMAN_CHECKS = (
    "【自動生成】この文は収集エージェントが機械的に付与した定型の確認項目であり、"
    "人が内容を確認済みであることを意味しません。採用の前に、出典URLおよび根拠引用が"
    "原本と一致すること、未実施の効果を成功と述べていないことを、人が別途確認してください。"
)


def _values(row) -> dict:
    return {key: (row[key] or "") for key in
            ("goal", "improve", "worsens", "ideal", "physical", "constraints", "resources")} | {
            "domain": row["domain"],
            "human_checks": HUMAN_CHECKS,
            "evidence": row["evidence"],
    }


def _source_url_from_evidence(evidence) -> str | None:
    if not isinstance(evidence, str):
        return None
    for line in evidence.splitlines():
        if line.startswith("出典URL:"):
            return line.partition(":")[2].strip() or None
    return None


def _has_source(state: dict, source_url: str) -> bool:
    for session in state.get("sessions") or []:
        evidence = ((session.get("problem") or {}).get("evidence"))
        if _source_url_from_evidence(evidence) == source_url:
            return True
    return False


def _is_transport_or_server(exc: Exception) -> bool:
    return isinstance(exc, error.URLError) or (isinstance(exc, error.HTTPError) and exc.code >= 500)


def _error_text(exc: Exception) -> str:
    if isinstance(exc, error.HTTPError):
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            detail = ""
        return f"HTTP {exc.code}" + (f": {detail}" if detail else "")
    if isinstance(exc, error.URLError):
        return str(exc.reason)
    return str(exc) or exc.__class__.__name__
