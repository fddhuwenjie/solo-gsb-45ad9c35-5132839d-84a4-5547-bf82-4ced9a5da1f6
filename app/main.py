"""Flask 应用：静态页面 + 计算/版本/导出 API，SQLite 持久化。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory

from .planner import build_plan
from .scheduling_utils import PlanError
from .verify import verify_exposure

DB_PATH = os.environ.get(
    "PLANNER_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "planner.db"),
)
_STATIC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")

app = Flask(__name__, static_folder=None)
_lock = threading.Lock()


# ------------------------------------------------------------------ database
def get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), 0o775, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS versions (
            id          TEXT PRIMARY KEY,
            label       TEXT NOT NULL,
            request     TEXT NOT NULL,
            result      TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


# --------------------------------------------------------------------- pages
@app.get("/")
def index():
    return send_from_directory(_STATIC, "index.html")


@app.get("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(_STATIC, filename)


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


# ------------------------------------------------------------------- compute
@app.post("/api/compute")
def api_compute():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):

        return jsonify(error="请求体必须是 JSON 对象"), 400
    try:
        result = build_plan(payload)
    except PlanError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(result)


# ----------------------------------------------------------------- versions
@app.get("/api/versions")
def list_versions():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, label, created_at FROM versions ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return jsonify(
        [
            {"id": r["id"], "label": r["label"], "created_at": r["created_at"]}
            for r in rows
        ]
    )


@app.post("/api/versions")
def save_version():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("request"), dict):
        return jsonify(error="需要包含 request（计算输入）的 JSON 对象"), 400
    label = str(body.get("label") or "").strip()
    try:
        result = build_plan(body["request"])
    except PlanError as exc:
        return jsonify(error=str(exc)), 400
    label = label or datetime.now(timezone.utc).strftime("版本 %Y-%m-%d %H:%M UTC")
    vid = uuid.uuid4().hex[:12]
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_db()
    try:
        with _lock:
            conn.execute(
                "INSERT INTO versions (id, label, request, result, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    vid,
                    label,
                    json.dumps(body["request"], ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                    created,
                ),
            )
            conn.commit()
    finally:
        conn.close()
    return jsonify({"id": vid, "label": label, "created_at": created, "result": result})


@app.get("/api/versions/<vid>")
def get_version(vid):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, label, request, result, created_at FROM versions WHERE id = ?",
            (vid,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return jsonify(error="版本不存在"), 404
    return jsonify(
        {
            "id": row["id"],
            "label": row["label"],
            "created_at": row["created_at"],
            "request": json.loads(row["request"]),
            "result": json.loads(row["result"]),
        }
    )


@app.delete("/api/versions/<vid>")
def delete_version(vid):
    conn = get_db()
    try:
        with _lock:
            cur = conn.execute("DELETE FROM versions WHERE id = ?", (vid,))
            conn.commit()
    finally:
        conn.close()
    if cur.rowcount == 0:
        return jsonify(error="版本不存在"), 404
    return jsonify(deleted=vid)


# -------------------------------------------------------------------- verify
@app.post("/api/versions/<vid>/verify")
def verify_version(vid):
    """实测曝光核验：以版本保存的排程为准核算实际观测时段，只读不改库。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="请求体必须是 JSON 对象"), 400
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, label, result FROM versions WHERE id = ?", (vid,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return jsonify(error="版本不存在"), 404
    try:
        outcome = verify_exposure(json.loads(row["result"]), body)
    except PlanError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(
        {
            "version_id": row["id"],
            "version_label": row["label"],
            **outcome,
        }
    )


# -------------------------------------------------------------------- export
@app.post("/api/export")
def api_export():
    """返回自描述的时间表 JSON（输入参数 + 排程结果）。"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="请求体必须是 JSON 对象"), 400
    try:
        result = build_plan(payload)
    except PlanError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(
        {
            "schema": "observation-schedule/v1",
            "generated_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "request": payload,
            "schedule": result,
        }
    )


if __name__ == "__main__":
    # 直接 python -m app.main 时使用；容器内由 gunicorn 启动
    get_db().close()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
