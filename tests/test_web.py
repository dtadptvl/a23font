"""HTTP layer tests."""
import json

from app import services
from app.db import open_db

VALID_URL = "https://www.myfonts.com/collections/web-suite"


def test_health_live(app_client):
    response = app_client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "a23font"}


def test_health_ready(app_client):
    response = app_client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_home_renders_form(app_client):
    response = app_client.get("/")
    assert response.status_code == 200
    assert "<form" in response.text
    assert 'name="url"' in response.text


def test_post_jobs_invalid_url(app_client):
    response = app_client.post("/jobs", data={"url": "http://fonts.google.com/collections/x"})
    assert response.status_code == 400
    assert 'class="error"' in response.text


def test_post_jobs_valid_redirects_and_persists(app_client, cfg):
    response = app_client.post(
        "/jobs",
        data={"url": VALID_URL, "vietnamese": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/jobs/")
    job_id = location.rsplit("/", 1)[1]

    conn = open_db(cfg.data_root / "db" / "a23font.db")
    try:
        job = services.get_job(conn, job_id)
    finally:
        conn.close()
    assert job is not None
    assert job["status"] == "QUEUED"
    options = json.loads(job["options_json"])
    assert options["vietnamese"] is True

    page = app_client.get(f"/jobs/{job_id}")
    assert page.status_code == 200

    status = app_client.get(f"/jobs/{job_id}/status")
    assert status.status_code == 200
    payload = status.json()
    for key in (
        "id", "status", "stage", "styles_total", "styles_done", "styles_failed",
        "cancel_requested", "error_code", "error_message", "styles", "terminal",
    ):
        assert key in payload
    assert payload["terminal"] is False
    assert payload["styles"] == []


def test_unknown_job_page_404(app_client):
    response = app_client.get("/jobs/J-doesnotexist")
    assert response.status_code == 404


def test_unknown_job_status_404(app_client):
    response = app_client.get("/jobs/J-doesnotexist/status")
    assert response.status_code == 404


def test_download_conflict_before_done(app_client):
    response = app_client.post("/jobs", data={"url": VALID_URL}, follow_redirects=False)
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[1]
    download = app_client.get(f"/jobs/{job_id}/download")
    assert download.status_code == 409
    assert download.json()["error"] == "job_not_finished"


def _finish_job_with_zip(cfg, job_id: str, status: str, write_file: bool = True):
    """Forge a terminal job row (+ artifact) for download-route coverage."""
    zip_path = cfg.data_root / "outputs" / f"{job_id}.zip"
    payload = b"PK\x03\x04a23font-test-zip"
    if write_file:
        zip_path.write_bytes(payload)
    conn = open_db(cfg.data_root / "db" / "a23font.db")
    try:
        conn.execute(
            "UPDATE jobs SET status = ?, zip_name = ?, zip_size = ? WHERE id = ?",
            (status, zip_path.name, len(payload), job_id),
        )
        conn.commit()
    finally:
        conn.close()
    return payload


def test_download_serves_zip_for_done_and_done_with_errors(app_client, cfg):
    for status in ("DONE", "DONE_WITH_ERRORS"):
        response = app_client.post(
            "/jobs", data={"url": VALID_URL}, follow_redirects=False
        )
        assert response.status_code == 303
        job_id = response.headers["location"].rsplit("/", 1)[1]
        payload = _finish_job_with_zip(cfg, job_id, status)
        download = app_client.get(f"/jobs/{job_id}/download")
        assert download.status_code == 200, (status, download.text)
        assert download.headers["content-type"] == "application/zip"
        assert download.headers["content-disposition"] == (
            f'attachment; filename="{job_id}.zip"'
        )
        assert download.content == payload


def test_download_no_artifact_is_409(app_client, cfg):
    # downloadable status but no zip recorded (e.g. packaging failed)
    response = app_client.post("/jobs", data={"url": VALID_URL}, follow_redirects=False)
    job_id = response.headers["location"].rsplit("/", 1)[1]
    conn = open_db(cfg.data_root / "db" / "a23font.db")
    try:
        conn.execute(
            "UPDATE jobs SET status = 'DONE_WITH_ERRORS', zip_name = NULL WHERE id = ?",
            (job_id,),
        )
        conn.commit()
    finally:
        conn.close()
    download = app_client.get(f"/jobs/{job_id}/download")
    assert download.status_code == 409
    assert download.json()["error"] == "no_artifact"


def test_download_failed_job_is_409_not_finished(app_client, cfg):
    response = app_client.post("/jobs", data={"url": VALID_URL}, follow_redirects=False)
    job_id = response.headers["location"].rsplit("/", 1)[1]
    conn = open_db(cfg.data_root / "db" / "a23font.db")
    try:
        conn.execute("UPDATE jobs SET status = 'FAILED' WHERE id = ?", (job_id,))
        conn.commit()
    finally:
        conn.close()
    download = app_client.get(f"/jobs/{job_id}/download")
    assert download.status_code == 409
    assert download.json()["error"] == "job_not_finished"


def test_download_missing_file_is_404(app_client, cfg):
    response = app_client.post("/jobs", data={"url": VALID_URL}, follow_redirects=False)
    job_id = response.headers["location"].rsplit("/", 1)[1]
    _finish_job_with_zip(cfg, job_id, "DONE", write_file=False)
    download = app_client.get(f"/jobs/{job_id}/download")
    assert download.status_code == 404
    assert download.json()["error"] == "artifact_missing"


def test_cancel_redirect(app_client):
    response = app_client.post("/jobs", data={"url": VALID_URL}, follow_redirects=False)
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[1]
    cancel = app_client.post(f"/jobs/{job_id}/cancel", follow_redirects=False)
    assert cancel.status_code == 303
    assert cancel.headers["location"] == f"/jobs/{job_id}"


def test_ops_page(app_client):
    response = app_client.get("/ops")
    assert response.status_code == 200
    assert "Ops" in response.text
