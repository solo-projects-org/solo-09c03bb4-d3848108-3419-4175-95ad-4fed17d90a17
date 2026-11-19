"""FastAPI application for the spectrum release console."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from urllib.parse import urlencode

from . import db, service
from .compute import display_q
from .csvio import ParseError

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

MAX_UPLOAD_BYTES = 64 * 1024 * 1024
KIND_LABELS = service.KIND_LABELS

STATUS_LABELS = {
    "pending": "待计算",
    "running": "计算中",
    "done": "已完成",
    "blocked": "已暂停（有问题）",
    "failed": "失败",
    "superseded": "已作废",
    "active": "现行",
    "revoked": "已撤销",
    "unchanged": "未变化",
}

ISSUE_HINTS = {
    "duplicate_spectrum": "请修正清单中的重复行后以更高修订号重新上传。",
    "candidate_not_in_list": "候选中的谱图不在待交付清单内，请核对清单或候选文件。",
    "missing_candidate": "清单中的谱图没有候选，请补充候选或从清单移除。",
    "top_score_tie": "同一谱图最高分并列，无法确定唯一候选；请复核打分或候选去重。",
}


def default_db_path() -> str:
    return os.environ.get(
        "RELEASE_DB", str(BASE_DIR.parent / "data" / "release.db")
    )


def _q_for_csv(value: str) -> str:
    return format(
        Decimal(value).quantize(Decimal("0.0000000001"), rounding=ROUND_HALF_UP),
        "f",
    )


def _json_loads(text: str | None, fallback: Any = None) -> Any:
    if not text:
        return fallback
    try:
        return json.loads(text)
    except ValueError:
        return fallback


def _issues_for(conn: sqlite3.Connection, job_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT issue_type, title, count, details_json, truncated "
        "FROM job_issues WHERE job_id = ? ORDER BY idx",
        (job_id,),
    ).fetchall()
    issues = []
    for row in rows:
        issues.append(
            {
                "type": row["issue_type"],
                "title": row["title"],
                "count": row["count"],
                "details": _json_loads(row["details_json"], []),
                "truncated": bool(row["truncated"]),
                "hint": ISSUE_HINTS.get(row["issue_type"], ""),
            }
        )
    return issues


def gather_dashboard(conn: sqlite3.Connection) -> dict[str, Any]:
    sources = service.current_sources(conn)
    threshold = service.get_threshold(conn)

    release = conn.execute(
        "SELECT * FROM releases WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    release_info = None
    accepted_count = 0
    if release is not None:
        accepted_count = conn.execute(
            "SELECT COUNT(*) AS n FROM release_rows "
            "WHERE release_id = ? AND accepted = 1",
            (release["id"],),
        ).fetchone()["n"]
        release_info = {
            "id": release["id"],
            "threshold": release["threshold"],
            "summary": _json_loads(release["summary_json"], {}),
            "created_at": release["created_at"],
            "manifest_revision": conn.execute(
                "SELECT revision FROM sources WHERE id = ?",
                (release["manifest_source_id"],),
            ).fetchone()["revision"],
            "candidate_revision": conn.execute(
                "SELECT revision FROM sources WHERE id = ?",
                (release["candidate_source_id"],),
            ).fetchone()["revision"],
            "accepted_count": accepted_count,
        }

    jobs = conn.execute(
        "SELECT j.*, m.revision AS manifest_revision, "
        "c.revision AS candidate_revision "
        "FROM jobs j "
        "JOIN sources m ON m.id = j.manifest_source_id "
        "JOIN sources c ON c.id = j.candidate_source_id "
        "ORDER BY j.id DESC LIMIT 20"
    ).fetchall()
    job_list = []
    latest_job = None
    for row in jobs:
        item = {
            "id": row["id"],
            "status": row["status"],
            "status_label": STATUS_LABELS.get(row["status"], row["status"]),
            "threshold": row["threshold"],
            "manifest_revision": row["manifest_revision"],
            "candidate_revision": row["candidate_revision"],
            "attempts": row["attempts"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "error": row["error"],
            "summary": _json_loads(row["summary_json"], {}),
        }
        job_list.append(item)
        if latest_job is None:
            latest_job = item

    last_revoked = conn.execute(
        "SELECT r.*, m.revision AS manifest_revision, "
        "c.revision AS candidate_revision "
        "FROM releases r "
        "JOIN sources m ON m.id = r.manifest_source_id "
        "JOIN sources c ON c.id = r.candidate_source_id "
        "WHERE r.status = 'revoked' ORDER BY r.id DESC LIMIT 1"
    ).fetchone()
    revoked_info = None
    if last_revoked is not None:
        revoked_info = {
            "id": last_revoked["id"],
            "revoked_at": last_revoked["revoked_at"],
            "reason": last_revoked["revoke_reason"],
            "manifest_revision": last_revoked["manifest_revision"],
            "candidate_revision": last_revoked["candidate_revision"],
        }

    issues: list[dict[str, Any]] = []
    if latest_job is not None and latest_job["status"] == "blocked":
        issues = _issues_for(conn, latest_job["id"])

    preview = []
    if release_info is not None:
        rows = conn.execute(
            "SELECT sample, spectrum, peptide, score, q_value, rank "
            "FROM release_rows WHERE release_id = ? AND accepted = 1 "
            "ORDER BY rank LIMIT 200",
            (release["id"],),
        ).fetchall()
        preview = [
            {
                "sample": row["sample"],
                "spectrum": row["spectrum"],
                "peptide": row["peptide"],
                "score": row["score"],
                "q_value": display_q(row["q_value"]),
                "rank": row["rank"],
            }
            for row in rows
        ]

    def source_info(kind: str) -> dict[str, Any] | None:
        row = sources[kind]
        if row is None:
            return None
        return {
            "revision": row["revision"],
            "filename": row["filename"],
            "row_count": row["row_count"],
            "uploaded_at": row["uploaded_at"],
        }

    return {
        "sources": {
            "manifest": source_info("manifest"),
            "candidates": source_info("candidates"),
        },
        "threshold": threshold,
        "release": release_info,
        "jobs": job_list,
        "latest_job": latest_job,
        "revoked": revoked_info,
        "issues": issues,
        "preview": preview,
        "preview_limit": 200,
        "kind_labels": KIND_LABELS,
        "status_labels": STATUS_LABELS,
    }


def create_app(
    db_path: str | Path | None = None,
    *,
    start_worker: bool = True,
    poll_seconds: float = 1.0,
) -> FastAPI:
    path = str(db_path or default_db_path())
    db.init_db(path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        recovered = service.recover_interrupted_jobs(path)
        worker = None
        if start_worker:
            worker = service.Worker(path, poll_seconds=poll_seconds)
            worker.start()
        app.state.worker = worker
        if recovered:
            logging.getLogger("release").warning(
                "recovered %s interrupted job(s)", recovered
            )
        try:
            yield
        finally:
            if worker is not None:
                worker.stop()
                worker.join(timeout=10)

    app = FastAPI(title="谱图匹配放行台", lifespan=lifespan)
    app.state.db_path = path
    app.state.templates = TEMPLATES
    app.mount(
        "/static",
        StaticFiles(directory=str(BASE_DIR / "static")),
        name="static",
    )

    def render(
        request: Request,
        name: str,
        context: dict[str, Any],
        status_code: int = 200,
    ) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request, name, context, status_code=status_code
        )

    def with_conn(fn):
        conn = db.connect(path)
        try:
            return fn(conn)
        finally:
            conn.close()

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, msg: str | None = None, level: str = "info"):
        data = with_conn(gather_dashboard)
        return render(
            request,
            "index.html",
            {
                "msg": msg,
                "level": level,
                "q_display": display_q,
                **data,
            },
        )

    def _upload(
        request: Request,
        kind: str,
        revision: str,
        file: UploadFile,
    ):
        if kind not in KIND_LABELS:
            return render(
                request,
                "message.html",
                {
                    "title": "未知来源类型",
                    "message": f"无法识别的来源类型：{kind}",
                    "level": "error",
                    "back_url": "/",
                },
                status_code=404,
            )
        data = file.file.read()
        if len(data) > MAX_UPLOAD_BYTES:
            return render(
                request,
                "message.html",
                {
                    "title": "上传失败",
                    "message": "文件超过 64 MiB 上限。",
                    "level": "error",
                    "back_url": "/",
                },
                status_code=413,
            )
        try:
            result = service.upload_source(
                path, kind, revision, data, file.filename or f"{kind}.csv"
            )
        except (service.ServiceError, ParseError) as exc:
            return render(
                request,
                "message.html",
                {
                    "title": f"{KIND_LABELS[kind]}上传被拒绝",
                    "message": str(exc),
                    "level": "error",
                    "back_url": "/",
                },
                status_code=400,
            )
        worker = getattr(request.app.state, "worker", None)
        if worker is not None and result.job_id is not None:
            worker.wake()
        query = urlencode({"msg": result.message, "level": "ok"})
        return RedirectResponse(f"/?{query}", status_code=303)

    @app.post("/upload/manifest")
    def upload_manifest(
        request: Request,
        revision: str = Form(...),
        file: UploadFile = File(...),
    ):
        return _upload(request, "manifest", revision, file)

    @app.post("/upload/candidates")
    def upload_candidates(
        request: Request,
        revision: str = Form(...),
        file: UploadFile = File(...),
    ):
        return _upload(request, "candidates", revision, file)

    @app.post("/threshold")
    def update_threshold(request: Request, threshold: str = Form(...)):
        try:
            new_value, job_id = service.set_threshold(path, threshold)
        except (service.ServiceError, ParseError) as exc:
            return render(
                request,
                "message.html",
                {
                    "title": "阈值更新失败",
                    "message": str(exc),
                    "level": "error",
                    "back_url": "/",
                },
                status_code=400,
            )
        if job_id is None:
            message = f"FDR 阈值未变化（{new_value}）。"
        else:
            message = (
                f"FDR 阈值已设为 {new_value}；原放行表已撤销，整份重算已排队。"
            )
            worker = getattr(request.app.state, "worker", None)
            if worker is not None:
                worker.wake()
        query = urlencode({"msg": message, "level": "ok"})
        return RedirectResponse(f"/?{query}", status_code=303)

    @app.post("/recompute")
    def recompute(request: Request):
        try:
            job_id = service.retrigger_current(path)
        except service.ServiceError as exc:
            query = urlencode({"msg": str(exc), "level": "error"})
            return RedirectResponse(f"/?{query}", status_code=303)
        if job_id is None:
            query = urlencode(
                {"msg": "两份现行来源尚未齐备，无法重算。", "level": "error"}
            )
            return RedirectResponse(f"/?{query}", status_code=303)
        worker = getattr(request.app.state, "worker", None)
        if worker is not None:
            worker.wake()
        query = urlencode(
            {"msg": f"已排队整份重算（任务 #{job_id}）。", "level": "ok"}
        )
        return RedirectResponse(f"/?{query}", status_code=303)

    @app.post("/jobs/{job_id}/retry")
    def retry(request: Request, job_id: int):
        try:
            service.retry_job(path, job_id)
        except (service.ServiceError, service.NotFoundError) as exc:
            query = urlencode({"msg": str(exc), "level": "error"})
            return RedirectResponse(f"/?{query}", status_code=303)
        worker = getattr(request.app.state, "worker", None)
        if worker is not None:
            worker.wake()
        query = urlencode(
            {"msg": f"任务 #{job_id} 已重新排队。", "level": "ok"}
        )
        return RedirectResponse(f"/?{query}", status_code=303)

    @app.get("/release")
    def release_page(request: Request, limit: int = 200, offset: int = 0):
        limit = max(1, min(limit, 1000))
        offset = max(0, offset)

        def load(conn: sqlite3.Connection):
            release = conn.execute(
                "SELECT * FROM releases WHERE status = 'active' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if release is None:
                return None, [], 0
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM release_rows "
                "WHERE release_id = ? AND accepted = 1",
                (release["id"],),
            ).fetchone()["n"]
            rows = conn.execute(
                "SELECT sample, spectrum, peptide, score, q_value, rank "
                "FROM release_rows WHERE release_id = ? AND accepted = 1 "
                "ORDER BY rank LIMIT ? OFFSET ?",
                (release["id"], limit, offset),
            ).fetchall()
            return release, rows, total

        release, rows, total = with_conn(load)
        return render(
            request,
            "release.html",
            {
                "release": release,
                "summary": _json_loads(release["summary_json"], {})
                if release
                else {},
                "rows": rows,
                "total": total,
                "limit": limit,
                "offset": offset,
                "q_display": display_q,
            },
        )

    @app.get("/release.csv")
    def download_release():
        conn = db.connect(path)
        try:
            release = conn.execute(
                "SELECT * FROM releases WHERE status = 'active' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if release is None:
                return HTMLResponse(
                    "<h1>当前没有已放行的结果</h1>", status_code=404
                )
            rows = conn.execute(
                "SELECT sample, spectrum, peptide, score, q_value, rank, "
                "score_group FROM release_rows "
                "WHERE release_id = ? AND accepted = 1 ORDER BY rank",
                (release["id"],),
            ).fetchall()
        finally:
            conn.close()

        buffer = io.StringIO()
        buffer.write("﻿")
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(
            [
                "样本号",
                "谱图号",
                "肽段",
                "靶标/诱饵",
                "分数",
                "q值",
                "全实验排名",
                "同分组",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["sample"],
                    row["spectrum"],
                    row["peptide"],
                    "靶标",
                    row["score"],
                    _q_for_csv(row["q_value"]),
                    row["rank"],
                    row["score_group"],
                ]
            )
        filename = (
            f"targets_m{release['manifest_source_id']}"
            f"_c{release['candidate_source_id']}"
            f"_fdr{release['threshold']}.csv"
        )
        return StreamingResponse(
            iter([buffer.getvalue()]),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            },
        )

    @app.get("/api/status")
    def api_status():
        def load(conn: sqlite3.Connection):
            return gather_dashboard(conn)

        data = with_conn(load)
        return JSONResponse(
            {
                "threshold": data["threshold"],
                "sources": data["sources"],
                "latest_job": data["latest_job"],
                "release": data["release"],
                "issues": data["issues"],
            }
        )

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
