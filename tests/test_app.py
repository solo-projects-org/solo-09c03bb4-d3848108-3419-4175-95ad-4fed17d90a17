import csv
import io

import pytest
from fastapi.testclient import TestClient

from app import db, service
from app.main import create_app


MANIFEST_HEADER = "sample,spectrum\n"
CAND_HEADER = "sample,spectrum,peptide,target_decoy,score\n"


def manifest_csv(*rows):
    buf = io.StringIO()
    buf.write(MANIFEST_HEADER)
    for sample, spectrum in rows:
        buf.write(f"{sample},{spectrum}\n")
    return buf.getvalue().encode("utf-8")


def candidates_csv(*rows):
    buf = io.StringIO()
    buf.write(CAND_HEADER)
    for sample, spectrum, peptide, label, score in rows:
        buf.write(f"{sample},{spectrum},{peptide},{label},{score}\n")
    return buf.getvalue().encode("utf-8")


@pytest.fixture()
def harness(tmp_path):
    db_path = tmp_path / "release.db"
    app = create_app(db_path, start_worker=False)
    with TestClient(app) as client:
        client.db_path = db_path
        yield client


def upload(client, kind, revision, payload, filename="input.csv"):
    return client.post(
        f"/upload/{kind}",
        data={"revision": str(revision)},
        files={"file": (filename, payload, "text/csv")},
        follow_redirects=False,
    )


def process_all(client):
    results = []
    while True:
        result = service.process_next_job(client.db_path)
        if result is None:
            return results
        results.append(result)


def job_row(client, job_id):
    conn = db.connect(client.db_path)
    try:
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()


def active_release(client):
    conn = db.connect(client.db_path)
    try:
        return conn.execute(
            "SELECT * FROM releases WHERE status = 'active'"
        ).fetchone()
    finally:
        conn.close()


def latest_job(client):
    conn = db.connect(client.db_path)
    try:
        return conn.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()


def issue_types(client):
    conn = db.connect(client.db_path)
    try:
        job = latest_job(client)
        rows = conn.execute(
            "SELECT issue_type, count FROM job_issues WHERE job_id = ? "
            "ORDER BY idx",
            (job["id"],),
        ).fetchall()
        return [(row["issue_type"], row["count"]) for row in rows]
    finally:
        conn.close()


TARGET_COUNT = 200


def standard_manifest():
    return manifest_csv(
        *(("S1", str(index)) for index in range(1, TARGET_COUNT + 2))
    )


def standard_candidates():
    # One decoy at the top and 200 targets beneath it.  Final FDR is
    # (0 + 1 + 1) / 200 = 0.01, so every target passes the default threshold.
    rows = [("S1", "1", "PEP_DECOY", "decoy", "1000")]
    rows += [
        ("S1", str(index), f"PEP_{index}", "target", str(1000 - index))
        for index in range(2, TARGET_COUNT + 2)
    ]
    return candidates_csv(*rows)


def test_full_flow_publishes_and_downloads(harness):
    client = harness
    r1 = upload(client, "manifest", 1, standard_manifest())
    assert r1.status_code == 303
    r2 = upload(client, "candidates", 1, standard_candidates())
    assert r2.status_code == 303

    results = process_all(client)
    assert results == ["done"]

    release = active_release(client)
    assert release is not None
    assert release["threshold"] == "0.01"
    assert release["status"] == "active"

    page = client.get("/")
    assert page.status_code == 200
    assert "放行有效" in page.text
    assert "PEP_2" in page.text

    table = client.get("/release")
    assert table.status_code == 200
    assert "PEP_3" in table.text

    csv_response = client.get("/release.csv")
    assert csv_response.status_code == 200
    assert "attachment" in csv_response.headers["content-disposition"]
    text = csv_response.content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == [
        "样本号",
        "谱图号",
        "肽段",
        "靶标/诱饵",
        "分数",
        "q值",
        "全实验排名",
        "同分组",
    ]
    # header + 200 targets; the decoy never appears in the target CSV
    assert len(rows) == TARGET_COUNT + 1
    assert rows[1][2] == "PEP_2"
    assert rows[-1][2] == f"PEP_{TARGET_COUNT + 1}"
    assert all(row[3] == "靶标" for row in rows[1:])
    assert all(abs(float(row[5]) - 0.01) < 1e-9 for row in rows[1:])


def test_duplicate_manifest_blocks_release(harness):
    client = harness
    upload(client, "manifest", 1, manifest_csv(("S1", "1"), ("S1", "1")))
    upload(client, "candidates", 1, candidates_csv(("S1", "1", "P", "target", "10")))
    assert process_all(client) == ["blocked"]
    assert active_release(client) is None
    assert ("duplicate_spectrum", 1) in issue_types(client)

    page = client.get("/")
    assert "放行已暂停" in page.text
    assert "清单重复" in page.text


def test_candidate_not_in_manifest_blocks(harness):
    client = harness
    upload(client, "manifest", 1, manifest_csv(("S1", "1")))
    upload(
        client,
        "candidates",
        1,
        candidates_csv(
            ("S1", "1", "P", "target", "10"),
            ("S2", "7", "Q", "decoy", "9"),
        ),
    )
    assert process_all(client) == ["blocked"]
    assert issue_types(client) == [("candidate_not_in_list", 1)]
    assert active_release(client) is None


def test_missing_candidate_blocks(harness):
    client = harness
    upload(client, "manifest", 1, manifest_csv(("S1", "1"), ("S1", "2")))
    upload(client, "candidates", 1, candidates_csv(("S1", "1", "P", "target", "10")))
    assert process_all(client) == ["blocked"]
    assert issue_types(client) == [("missing_candidate", 1)]


def test_top_score_tie_blocks(harness):
    client = harness
    upload(client, "manifest", 1, manifest_csv(("S1", "1")))
    upload(
        client,
        "candidates",
        1,
        candidates_csv(
            ("S1", "1", "P", "target", "10"),
            ("S1", "1", "Q", "decoy", "10"),
            ("S1", "1", "R", "target", "1"),
        ),
    )
    assert process_all(client) == ["blocked"]
    assert issue_types(client) == [("top_score_tie", 1)]


def test_all_issues_reported_at_once(harness):
    client = harness
    upload(client, "manifest", 1, manifest_csv(("S1", "1"), ("S1", "1"), ("S1", "2")))
    upload(
        client,
        "candidates",
        1,
        candidates_csv(
            ("S1", "1", "P", "target", "10"),
            ("S1", "1", "Q", "decoy", "10"),
            ("S9", "9", "Z", "target", "5"),
        ),
    )
    assert process_all(client) == ["blocked"]
    assert [t for t, _ in issue_types(client)] == [
        "duplicate_spectrum",
        "candidate_not_in_list",
        "missing_candidate",
        "top_score_tie",
    ]


def test_resubmitting_same_revision_and_content_changes_nothing(harness):
    client = harness
    upload(client, "manifest", 1, standard_manifest())
    upload(client, "candidates", 1, standard_candidates())
    process_all(client)
    release_before = active_release(client)
    assert release_before is not None

    r = upload(client, "manifest", 1, standard_manifest())
    assert r.status_code == 303
    assert "未变化" in r.headers["location"] or "%E6%9C%AA%E5%8F%98%E5%8C%96" in r.headers["location"]
    assert active_release(client)["id"] == release_before["id"]
    assert service.process_next_job(client.db_path) is None
    assert job_row(client, latest_job(client)["id"])["status"] == "done"

    # Same bytes for the candidates too.
    r = upload(client, "candidates", 1, standard_candidates())
    assert r.status_code == 303
    assert active_release(client)["id"] == release_before["id"]


def test_resubmitting_superseded_identical_revision_changes_nothing(harness):
    client = harness
    upload(client, "manifest", 1, standard_manifest())
    upload(client, "candidates", 1, standard_candidates())
    upload(client, "manifest", 2, standard_manifest())
    process_all(client)
    release_before = active_release(client)
    assert release_before is not None

    # Revision 1 is now historical; identical bytes must neither change the
    # current revision nor revoke the release.
    r = upload(client, "manifest", 1, standard_manifest())
    assert r.status_code == 303
    assert "历史版本" in r.headers["location"] or "%E5%8E%86%E5%8F%B2" in r.headers["location"]
    assert active_release(client)["id"] == release_before["id"]
    assert service.process_next_job(client.db_path) is None


def test_same_revision_different_content_rejected(harness):
    client = harness
    upload(client, "manifest", 1, standard_manifest())
    r = upload(client, "manifest", 1, manifest_csv(("S1", "1")))
    assert r.status_code == 400
    assert "内容不同" in r.text
    conn = db.connect(client.db_path)
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]
        current = conn.execute(
            "SELECT s.revision FROM current_sources c JOIN sources s "
            "ON s.id = c.source_id WHERE c.kind = 'manifest'"
        ).fetchone()["revision"]
    finally:
        conn.close()
    assert count == 1
    assert current == 1


def test_non_increasing_revision_rejected(harness):
    client = harness
    upload(client, "manifest", 2, standard_manifest())
    r = upload(client, "manifest", 1, manifest_csv(("S1", "9")))
    assert r.status_code == 400
    assert "递增" in r.text
    r = upload(client, "manifest", 2, manifest_csv(("S1", "9")))
    assert r.status_code == 400


def test_revision_change_revokes_release_immediately(harness):
    client = harness
    upload(client, "manifest", 1, standard_manifest())
    upload(client, "candidates", 1, standard_candidates())
    process_all(client)
    assert active_release(client) is not None

    r = upload(client, "manifest", 2, standard_manifest())
    assert r.status_code == 303
    assert active_release(client) is None
    revoked = client.get("/release")
    assert revoked.status_code == 200
    assert "没有放行的结果" in revoked.text

    # The queued job for the new pair then republishes.
    assert process_all(client) == ["done"]
    release = active_release(client)
    conn = db.connect(client.db_path)
    try:
        manifest_revision = conn.execute(
            "SELECT revision FROM sources WHERE id = ?",
            (release["manifest_source_id"],),
        ).fetchone()["revision"]
        candidate_revision = conn.execute(
            "SELECT revision FROM sources WHERE id = ?",
            (release["candidate_source_id"],),
        ).fetchone()["revision"]
    finally:
        conn.close()
    assert (manifest_revision, candidate_revision) == (2, 1)


def test_interleaved_recompute_discards_stale_pair(harness):
    client = harness
    upload(client, "manifest", 1, standard_manifest())
    upload(client, "candidates", 1, standard_candidates())

    # Simulate a worker that claimed the (m1, c1) job and is computing it.
    stale_job = service._claim_next_job(client.db_path)
    assert stale_job is not None
    assert stale_job["status"] == "running"

    # While it is running, the spectrum list is replaced.
    r = upload(client, "manifest", 2, standard_manifest())
    assert r.status_code == 303

    # The stale computation finishing late must publish nothing.
    assert service.process_job(client.db_path, stale_job) == "discarded"
    assert active_release(client) is None

    # Only the job matching the two current sources may publish.
    results = process_all(client)
    assert results == ["done"]
    release = active_release(client)
    assert release["manifest_source_id"] == latest_job(client)["manifest_source_id"]
    conn = db.connect(client.db_path)
    try:
        current_manifest = conn.execute(
            "SELECT source_id FROM current_sources WHERE kind = 'manifest'"
        ).fetchone()["source_id"]
        current_candidates = conn.execute(
            "SELECT source_id FROM current_sources WHERE kind = 'candidates'"
        ).fetchone()["source_id"]
    finally:
        conn.close()
    assert release["manifest_source_id"] == current_manifest
    assert release["candidate_source_id"] == current_candidates


def test_interleaved_candidate_change_mid_compute(harness):
    client = harness
    upload(client, "manifest", 1, standard_manifest())
    upload(client, "candidates", 1, standard_candidates())
    running = service._claim_next_job(client.db_path)

    changed = standard_candidates().replace(b"PEP_A", b"PEP_X")
    assert upload(client, "candidates", 2, changed).status_code == 303

    assert service.process_job(client.db_path, running) == "discarded"
    assert active_release(client) is None
    assert process_all(client) == ["done"]
    release = active_release(client)
    conn = db.connect(client.db_path)
    try:
        revision = conn.execute(
            "SELECT revision FROM sources WHERE id = ?",
            (release["candidate_source_id"],),
        ).fetchone()["revision"]
    finally:
        conn.close()
    assert revision == 2


def test_duplicate_worker_claim_publishes_once(harness):
    client = harness
    upload(client, "manifest", 1, standard_manifest())
    upload(client, "candidates", 1, standard_candidates())
    job = service._claim_next_job(client.db_path)
    assert service.process_job(client.db_path, job) == "done"
    # Re-processing the same job must not create a second active release.
    assert service.process_job(client.db_path, job) == "discarded"
    conn = db.connect(client.db_path)
    try:
        active = conn.execute(
            "SELECT COUNT(*) AS n FROM releases WHERE status = 'active'"
        ).fetchone()["n"]
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM release_rows"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert active == 1
    assert rows == TARGET_COUNT + 1


def test_failed_job_can_be_retried(harness, monkeypatch):
    client = harness
    upload(client, "manifest", 1, standard_manifest())
    upload(client, "candidates", 1, standard_candidates())

    real = service.load_and_compute
    calls = {"n": 0}

    def flaky(db_path, job):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash")
        return real(db_path, job)

    monkeypatch.setattr(service, "load_and_compute", flaky)
    assert process_all(client) == ["failed"]
    failed = latest_job(client)
    assert failed["status"] == "failed"
    assert "simulated crash" in failed["error"]
    assert active_release(client) is None

    response = client.post(f"/jobs/{failed['id']}/retry", follow_redirects=False)
    assert response.status_code == 303
    assert job_row(client, failed["id"])["status"] == "pending"

    assert process_all(client) == ["done"]
    assert active_release(client) is not None
    assert job_row(client, failed["id"])["attempts"] == 2


def test_failed_job_for_old_pair_cannot_be_retried(harness, monkeypatch):
    client = harness
    upload(client, "manifest", 1, standard_manifest())
    upload(client, "candidates", 1, standard_candidates())

    real = service.load_and_compute

    def boom(db_path, job):
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "load_and_compute", boom)
    assert process_all(client) == ["failed"]
    failed_id = latest_job(client)["id"]

    monkeypatch.setattr(service, "load_and_compute", real)
    upload(client, "manifest", 2, standard_manifest())
    with pytest.raises(service.ConflictError):
        service.retry_job(client.db_path, failed_id)


def test_restart_requeues_running_jobs(harness):
    client = harness
    upload(client, "manifest", 1, standard_manifest())
    upload(client, "candidates", 1, standard_candidates())
    running = service._claim_next_job(client.db_path)
    assert running["status"] == "running"

    recovered = service.recover_interrupted_jobs(client.db_path)
    assert recovered == 1
    job = job_row(client, running["id"])
    assert job["status"] == "pending"
    assert job["attempts"] == 1

    assert process_all(client) == ["done"]
    assert active_release(client) is not None
    assert job_row(client, running["id"])["attempts"] == 2


def test_threshold_change_revokes_and_recomputes(harness):
    client = harness
    upload(client, "manifest", 1, standard_manifest())
    upload(client, "candidates", 1, standard_candidates())
    process_all(client)
    assert active_release(client)["threshold"] == "0.01"

    r = client.post("/threshold", data={"threshold": "0.5"}, follow_redirects=False)
    assert r.status_code == 303
    assert active_release(client) is None
    assert process_all(client) == ["done"]
    release = active_release(client)
    assert release["threshold"] == "0.5"
    # q = 0.01 for all rows: only targets are released, decoy never is.
    csv_response = client.get("/release.csv")
    rows = list(csv.reader(io.StringIO(csv_response.content.decode("utf-8-sig"))))
    assert len(rows) == TARGET_COUNT + 1


def test_threshold_validation(harness):
    client = harness
    for bad in ("abc", "-0.1", "1.5", "nan"):
        r = client.post("/threshold", data={"threshold": bad})
        assert r.status_code == 400, bad
        assert "阈值" in r.text


def test_uploads_require_revision(harness):
    client = harness
    r = client.post(
        "/upload/manifest",
        files={"file": ("m.csv", standard_manifest(), "text/csv")},
        follow_redirects=False,
    )
    assert r.status_code == 422


def test_bad_csv_is_rejected_without_state_change(harness):
    client = harness
    bad = b"sample,spectrum\nS1\n"  # short row -> empty spectrum
    r = upload(client, "manifest", 1, bad)
    assert r.status_code == 400
    conn = db.connect(client.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"] == 0
    finally:
        conn.close()

    bad_label = candidates_csv(("S1", "1", "P", "unknown", "1"))
    r = upload(client, "candidates", 1, bad_label)
    assert r.status_code == 400
    assert "靶标/诱饵" in r.text

    bad_score = candidates_csv(("S1", "1", "P", "target", "abc"))
    r = upload(client, "candidates", 1, bad_score)
    assert r.status_code == 400


def test_no_release_until_both_sources_present(harness):
    client = harness
    upload(client, "manifest", 1, standard_manifest())
    assert service.process_next_job(client.db_path) is None
    upload(client, "candidates", 1, standard_candidates())
    assert process_all(client) == ["done"]
    assert active_release(client) is not None


def test_api_status_reports_state(harness):
    client = harness
    upload(client, "manifest", 1, standard_manifest())
    upload(client, "candidates", 1, standard_candidates())
    process_all(client)
    payload = client.get("/api/status").json()
    assert payload["sources"]["manifest"]["revision"] == 1
    assert payload["latest_job"]["status"] == "done"
    assert payload["release"]["accepted_count"] == TARGET_COUNT


def test_worker_thread_computes_without_manual_processing(tmp_path):
    import time

    db_path = tmp_path / "worker.db"
    app = create_app(db_path, start_worker=True, poll_seconds=0.05)
    with TestClient(app) as client:
        client.db_path = db_path
        upload(client, "manifest", 1, standard_manifest())
        upload(client, "candidates", 1, standard_candidates())
        deadline = time.time() + 15
        release = None
        while time.time() < deadline:
            release = active_release(client)
            if release is not None:
                break
            time.sleep(0.05)
        assert release is not None
        assert release["threshold"] == "0.01"
        csv_response = client.get("/release.csv")
        rows = list(
            csv.reader(io.StringIO(csv_response.content.decode("utf-8-sig")))
        )
        assert len(rows) == TARGET_COUNT + 1


def test_threshold_uses_exact_decimal_comparison(harness):
    client = harness
    # Two decoys above 200 targets: every prefix FDR is at least the final
    # (2 + 1) / 200 = exactly 0.015, so q = 0.015 for every row.
    rows = [("S1", str(i), f"PEP_{i}", "target", str(1000 - i)) for i in range(1, 201)]
    rows.append(("S1", "201", "D1", "decoy", "2000"))
    rows.append(("S1", "202", "D2", "decoy", "1999"))
    manifest = manifest_csv(*(("S1", str(i)) for i in range(1, 203)))
    upload(client, "manifest", 1, manifest)
    upload(client, "candidates", 1, candidates_csv(*rows))
    assert process_all(client) == ["done"]
    # q value is exactly 0.015; a 0.0149 threshold must release nothing.
    client.post("/threshold", data={"threshold": "0.0149"}, follow_redirects=False)
    process_all(client)
    response = client.get("/release.csv")
    text = response.content.decode("utf-8-sig")
    assert len(list(csv.reader(io.StringIO(text)))) == 1
    client.post("/threshold", data={"threshold": "0.015"}, follow_redirects=False)
    process_all(client)
    response = client.get("/release.csv")
    text = response.content.decode("utf-8-sig")
    assert len(list(csv.reader(io.StringIO(text)))) == 201
