"""Application services: uploads, jobs, publication and worker loop."""

from __future__ import annotations

import logging
import sqlite3
import threading
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import db
from .compute import (
    Candidate,
    ReleaseComputation,
    SpectrumKey,
    canonical_threshold,
    compute_fdr,
    decimal_text,
    validate,
)
from .csvio import ParsedSource, parse_source

logger = logging.getLogger("release.worker")

KIND_LABELS = {"manifest": "谱图清单", "candidates": "候选"}


class ServiceError(Exception):
    """Base class for errors that are safe to show to the operator."""


class ConflictError(ServiceError):
    """The upload conflicts with the current or historical source."""


class NotFoundError(ServiceError):
    pass


class UploadResult:
    def __init__(
        self,
        status: str,
        kind: str,
        revision: int,
        message: str,
        job_id: int | None = None,
    ) -> None:
        self.status = status
        self.kind = kind
        self.revision = revision
        self.message = message
        self.job_id = job_id


def _source_row(conn: sqlite3.Connection, kind: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT s.* FROM current_sources c JOIN sources s ON s.id = c.source_id "
        "WHERE c.kind = ?",
        (kind,),
    ).fetchone()


def current_sources(conn: sqlite3.Connection) -> dict[str, sqlite3.Row | None]:
    return {kind: _source_row(conn, kind) for kind in ("manifest", "candidates")}


def get_threshold(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'fdr_threshold'"
    ).fetchone()
    return row["value"] if row else db.DEFAULT_THRESHOLD


def parse_threshold(raw: str) -> str:
    text = (raw or "").strip()
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ServiceError(f"FDR 阈值不是有效数字：{raw!r}")
    if not value.is_finite():
        raise ServiceError("FDR 阈值必须是有限数字")
    if value < 0 or value > 1:
        raise ServiceError("FDR 阈值必须介于 0 和 1 之间")
    return canonical_threshold(value)


def _ensure_job(
    conn: sqlite3.Connection,
    manifest_id: int,
    candidate_id: int,
    threshold: str,
) -> int:
    """Reuse an open/failed job for the same sources, else create one."""
    row = conn.execute(
        "SELECT id FROM jobs WHERE manifest_source_id = ? "
        "AND candidate_source_id = ? AND threshold = ? "
        "AND status IN ('pending', 'running') ORDER BY id DESC LIMIT 1",
        (manifest_id, candidate_id, threshold),
    ).fetchone()
    if row:
        return int(row["id"])
    row = conn.execute(
        "SELECT id FROM jobs WHERE manifest_source_id = ? "
        "AND candidate_source_id = ? AND threshold = ? "
        "AND status = 'failed' ORDER BY id DESC LIMIT 1",
        (manifest_id, candidate_id, threshold),
    ).fetchone()
    if row:
        job_id = int(row["id"])
        conn.execute(
            "UPDATE jobs SET status = 'pending', error = NULL, finished_at = NULL, "
            "started_at = NULL WHERE id = ?",
            (job_id,),
        )
        return job_id
    cur = conn.execute(
        "INSERT INTO jobs (manifest_source_id, candidate_source_id, threshold, "
        "status, attempts, created_at) VALUES (?, ?, ?, 'pending', 0, ?)",
        (manifest_id, candidate_id, threshold, db.now_iso()),
    )
    return int(cur.lastrowid)


def enqueue_current(
    conn: sqlite3.Connection, reason: str
) -> int | None:
    """Queue a full recompute for the two current sources and threshold.

    An already queued or running job for exactly the same triple is reused so
    that repeated operator actions do not churn the queue.  Any other open job
    is superseded because it was computed from a source set that is no longer
    current.
    """
    manifest = _source_row(conn, "manifest")
    candidates = _source_row(conn, "candidates")
    if manifest is None or candidates is None:
        db.supersede_open_jobs(conn, reason)
        return None
    threshold = get_threshold(conn)
    same = conn.execute(
        "SELECT id FROM jobs WHERE manifest_source_id = ? "
        "AND candidate_source_id = ? AND threshold = ? "
        "AND status IN ('pending', 'running') ORDER BY id DESC LIMIT 1",
        (int(manifest["id"]), int(candidates["id"]), threshold),
    ).fetchone()
    if same is not None:
        return int(same["id"])
    db.supersede_open_jobs(conn, reason)
    return _ensure_job(
        conn, int(manifest["id"]), int(candidates["id"]), threshold
    )


def upload_source(
    db_path: str | Path,
    kind: str,
    revision_raw: str,
    data: bytes,
    filename: str,
) -> UploadResult:
    try:
        revision = int(str(revision_raw).strip())
    except (TypeError, ValueError):
        raise ServiceError(f"修订号必须是整数：{revision_raw!r}")
    if revision < 1:
        raise ServiceError("修订号必须从 1 开始递增")

    parsed: ParsedSource = parse_source(kind, data, filename)

    with db.write_tx(db_path) as conn:
        existing = conn.execute(
            "SELECT * FROM sources WHERE kind = ? AND revision = ?",
            (kind, revision),
        ).fetchone()
        if existing is not None:
            if existing["content_sha256"] == parsed.content_sha256:
                current_now = _source_row(conn, kind)
                if current_now is not None and int(current_now["id"]) == int(
                    existing["id"]
                ):
                    message = (
                        f"{KIND_LABELS[kind]}修订号 {revision} 与内容均未变化，"
                        "未撤销放行，也未触发重算。"
                    )
                else:
                    message = (
                        f"{KIND_LABELS[kind]}修订号 {revision} 是历史版本，"
                        "内容相同；未改变现行来源，也未撤销放行。"
                    )
                return UploadResult("unchanged", kind, revision, message)
            raise ConflictError(
                f"{KIND_LABELS[kind]}修订号 {revision} 已存在但文件内容不同，"
                "拒绝覆盖；请使用更大的修订号。"
            )

        current = _source_row(conn, kind)
        latest_revision = current["revision"] if current else 0
        if revision <= latest_revision:
            raise ConflictError(
                f"{KIND_LABELS[kind]}修订号必须递增：当前修订号为 "
                f"{latest_revision}，收到 {revision}。"
            )

        cur = conn.execute(
            "INSERT INTO sources (kind, revision, content_sha256, filename, "
            "row_count, uploaded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                kind,
                revision,
                parsed.content_sha256,
                filename,
                parsed.row_count,
                db.now_iso(),
            ),
        )
        source_id = int(cur.lastrowid)
        if kind == "manifest":
            conn.executemany(
                "INSERT INTO manifest_rows (source_id, ord, line_no, sample, "
                "spectrum) VALUES (?, ?, ?, ?, ?)",
                [
                    (source_id, index, 0, key.sample, key.spectrum)
                    for index, key in enumerate(parsed.spectra)
                ],
            )
        else:
            conn.executemany(
                "INSERT INTO candidate_rows (source_id, ord, line_no, sample, "
                "spectrum, peptide, label, score) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        source_id,
                        index,
                        candidate.line_no,
                        candidate.key.sample,
                        candidate.key.spectrum,
                        candidate.peptide,
                        candidate.label,
                        decimal_text(candidate.score),
                    )
                    for index, candidate in enumerate(parsed.candidates)
                ],
            )
        conn.execute(
            "INSERT INTO current_sources (kind, source_id, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(kind) DO UPDATE SET "
            "source_id = excluded.source_id, updated_at = excluded.updated_at",
            (kind, source_id, db.now_iso()),
        )
        revoked = db.revoke_active_release(
            conn,
            f"{KIND_LABELS[kind]}换版：修订号 {revision}",
        )
        job_id = enqueue_current(
            conn, f"{KIND_LABELS[kind]}换版：修订号 {revision}"
        )

    message = (
        f"{KIND_LABELS[kind]}修订号 {revision} 已接收（{parsed.row_count} 行）。"
    )
    if revoked:
        message += " 原放行表已立即撤销。"
    if job_id is not None:
        message += " 已排队整份重算。"
    else:
        message += " 等待两份现行来源齐备后再重算。"
    return UploadResult("accepted", kind, revision, message, job_id)


def set_threshold(db_path: str | Path, raw: str) -> tuple[str, int | None]:
    threshold = parse_threshold(raw)
    with db.write_tx(db_path) as conn:
        old = get_threshold(conn)
        if old == threshold:
            return threshold, None
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = 'fdr_threshold'",
            (threshold,),
        )
        db.revoke_active_release(conn, f"FDR 阈值变更：{old} → {threshold}")
        job_id = enqueue_current(conn, f"FDR 阈值变更：{old} → {threshold}")
    return threshold, job_id


def _load_candidates(
    conn: sqlite3.Connection, source_id: int
) -> list[Candidate]:
    rows = conn.execute(
        "SELECT sample, spectrum, peptide, label, score, line_no "
        "FROM candidate_rows WHERE source_id = ? ORDER BY ord",
        (source_id,),
    ).fetchall()
    return [
        Candidate(
            key=SpectrumKey(row["sample"], row["spectrum"]),
            peptide=row["peptide"],
            label=row["label"],
            score=Decimal(row["score"]),
            line_no=row["line_no"],
        )
        for row in rows
    ]


def _load_spectra(
    conn: sqlite3.Connection, source_id: int
) -> list[SpectrumKey]:
    rows = conn.execute(
        "SELECT sample, spectrum FROM manifest_rows "
        "WHERE source_id = ? ORDER BY ord",
        (source_id,),
    ).fetchall()
    return [SpectrumKey(row["sample"], row["spectrum"]) for row in rows]


def load_and_compute(
    db_path: str | Path, job: sqlite3.Row
) -> tuple[ReleaseComputation, list[dict[str, object]]]:
    conn = db.connect(db_path)
    try:
        # A job stores immutable source ids, so reading each table separately
        # is safe: rows for a given source id never change after upload.  The
        # read transaction only guarantees a consistent view of the database.
        conn.execute("BEGIN")
        spectra = _load_spectra(conn, int(job["manifest_source_id"]))
        candidates = _load_candidates(conn, int(job["candidate_source_id"]))
        conn.execute("COMMIT")
    finally:
        conn.close()
    issues = validate(spectra, candidates)
    threshold = Decimal(job["threshold"])
    computation = compute_fdr(candidates, threshold)
    return computation, issues


def _claim_next_job(db_path: str | Path) -> sqlite3.Row | None:
    with db.write_tx(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, "
            "started_at = ?, finished_at = NULL, error = NULL WHERE id = ?",
            (db.now_iso(), row["id"]),
        )
        return conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (row["id"],)
        ).fetchone()


def _fail_job(db_path: str | Path, job_id: int, error: str) -> None:
    with db.write_tx(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET status = 'failed', error = ?, finished_at = ? "
            "WHERE id = ? AND status = 'running'",
            (error[:4000], db.now_iso(), job_id),
        )


def _save_issues(
    conn: sqlite3.Connection, job_id: int, issues: list[dict[str, object]]
) -> None:
    for index, issue in enumerate(issues):
        conn.execute(
            "INSERT INTO job_issues (job_id, idx, issue_type, title, count, "
            "details_json, truncated) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                index,
                issue["type"],
                issue["title"],
                issue["count"],
                db.json_dumps(issue["details"]),
                1 if issue["truncated"] else 0,
            ),
        )


def _finalize_job(
    db_path: str | Path,
    job_id: int,
    computation: ReleaseComputation,
    issues: list[dict[str, object]],
) -> str:
    """Persist a job result, but only if it still matches current state."""
    with db.write_tx(db_path) as conn:
        job = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if job is None or job["status"] != "running":
            return "discarded"

        manifest = _source_row(conn, "manifest")
        candidate = _source_row(conn, "candidates")
        threshold = get_threshold(conn)
        current_ok = (
            manifest is not None
            and candidate is not None
            and int(manifest["id"]) == int(job["manifest_source_id"])
            and int(candidate["id"]) == int(job["candidate_source_id"])
            and threshold == job["threshold"]
        )
        if not current_ok:
            conn.execute(
                "UPDATE jobs SET status = 'superseded', finished_at = ?, "
                "error = COALESCE(error, '来源或阈值已变化，整份结果丢弃') "
                "WHERE id = ? AND status = 'running'",
                (db.now_iso(), job_id),
            )
            return "discarded"

        if issues:
            _save_issues(conn, job_id, issues)
            conn.execute(
                "UPDATE jobs SET status = 'blocked', finished_at = ?, "
                "summary_json = ? WHERE id = ?",
                (
                    db.now_iso(),
                    db.json_dumps({"issue_count": len(issues)}),
                    job_id,
                ),
            )
            return "blocked"

        # Defensive: a release that is still active cannot belong to the
        # current pair (uploads revoke it), but replacing is the safe order.
        db.revoke_active_release(conn, "发布新整份结果")
        cur = conn.execute(
            "INSERT INTO releases (job_id, manifest_source_id, "
            "candidate_source_id, threshold, status, summary_json, created_at) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?)",
            (
                job_id,
                job["manifest_source_id"],
                job["candidate_source_id"],
                job["threshold"],
                db.json_dumps(computation.summary),
                db.now_iso(),
            ),
        )
        release_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO release_rows (release_id, ord, sample, spectrum, "
            "peptide, label, score, q_value, accepted, rank, score_group) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    release_id,
                    index,
                    row.key.sample,
                    row.key.spectrum,
                    row.peptide,
                    row.label,
                    decimal_text(row.score),
                    decimal_text(row.q_value),
                    1 if row.accepted else 0,
                    row.rank,
                    row.score_group,
                )
                for index, row in enumerate(computation.rows)
            ],
        )
        conn.execute(
            "UPDATE jobs SET status = 'done', finished_at = ?, summary_json = ? "
            "WHERE id = ?",
            (db.now_iso(), db.json_dumps(computation.summary), job_id),
        )
        return "done"


def process_job(db_path: str | Path, job: sqlite3.Row) -> str:
    job_id = int(job["id"])
    try:
        computation, issues = load_and_compute(db_path, job)
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        logger.exception("job %s failed", job_id)
        _fail_job(db_path, job_id, f"{type(exc).__name__}: {exc}")
        return "failed"
    return _finalize_job(db_path, job_id, computation, issues)


def process_next_job(db_path: str | Path) -> str | None:
    job = _claim_next_job(db_path)
    if job is None:
        return None
    return process_job(db_path, job)


def recover_interrupted_jobs(db_path: str | Path) -> int:
    """Startup: jobs interrupted while running return to the pending queue."""
    with db.write_tx(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE status = 'running'"
        ).fetchall()
        if not rows:
            return 0
        conn.execute(
            "UPDATE jobs SET status = 'pending', started_at = NULL, "
            "error = '进程重启，任务重新排队' WHERE status = 'running'"
        )
        return len(rows)


def retry_job(db_path: str | Path, job_id: int) -> int:
    with db.write_tx(db_path) as conn:
        job = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if job is None:
            raise NotFoundError(f"任务 {job_id} 不存在")
        if job["status"] not in ("failed", "pending"):
            raise ConflictError(
                f"任务 {job_id} 当前状态为 {job['status']}，不能手动重试"
            )
        manifest = _source_row(conn, "manifest")
        candidate = _source_row(conn, "candidates")
        threshold = get_threshold(conn)
        if (
            manifest is None
            or candidate is None
            or int(manifest["id"]) != int(job["manifest_source_id"])
            or int(candidate["id"]) != int(job["candidate_source_id"])
            or threshold != job["threshold"]
        ):
            raise ConflictError(
                "该任务不对应当前两份来源与阈值，不能重试；"
                "请按现行来源触发重算。"
            )
        conn.execute(
            "UPDATE jobs SET status = 'pending', error = NULL, "
            "finished_at = NULL WHERE id = ?",
            (job_id,),
        )
        return job_id


def retrigger_current(db_path: str | Path) -> int | None:
    """Operator action: queue a full recompute for the current pair."""
    with db.write_tx(db_path) as conn:
        return enqueue_current(conn, "操作员触发重算")


class Worker(threading.Thread):
    def __init__(self, db_path: str | Path, poll_seconds: float = 1.0) -> None:
        super().__init__(name="release-worker", daemon=True)
        self.db_path = str(db_path)
        self.poll_seconds = poll_seconds
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()

    def wake(self) -> None:
        self.wake_event.set()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                result = process_next_job(self.db_path)
            except Exception:  # noqa: BLE001 - keep worker alive
                logger.exception("worker loop error")
                result = None
            if result is not None:
                continue
            self.wake_event.wait(self.poll_seconds)
            self.wake_event.clear()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
