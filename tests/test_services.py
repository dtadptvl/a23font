"""Service layer tests."""
import json

import pytest

from app import services
from app.security import SourceError

VALID_URL = "https://www.myfonts.com/collections/service-suite"


def test_create_job_returns_queued_row(db_conn, cfg):
    job = services.create_job(db_conn, VALID_URL, True, cfg)
    assert job["status"] == "QUEUED"
    assert job["normalized_url"] == "https://www.myfonts.com/collections/service-suite"
    options = json.loads(job["options_json"])
    assert options["vietnamese"] is True
    assert options["schema"] == cfg.pipeline_version


def test_create_job_invalid_url_raises(db_conn, cfg):
    with pytest.raises(SourceError):
        services.create_job(db_conn, "http://wrong.example/collections/x", False, cfg)
    assert services.get_job(db_conn, "anything") is None


def test_queue_full(db_conn, cfg):
    capacity = cfg.max_queue + cfg.max_active_collections
    for index in range(capacity):
        services.create_job(db_conn, f"https://www.myfonts.com/collections/set-{index}", False, cfg)
    with pytest.raises(services.QueueFullError):
        services.create_job(db_conn, "https://www.myfonts.com/collections/overflow", False, cfg)


def test_request_cancel_non_terminal(db_conn, cfg):
    job = services.create_job(db_conn, VALID_URL, False, cfg)
    assert services.request_cancel(db_conn, job["id"]) is True
    row = services.get_job(db_conn, job["id"])
    assert row["cancel_requested"] == 1


def test_request_cancel_terminal_returns_false(db_conn, cfg):
    job = services.create_job(db_conn, VALID_URL, False, cfg)
    services.finish_job(db_conn, job["id"], "FAILED", error_code="boom", error_message="exploded")
    assert services.request_cancel(db_conn, job["id"]) is False


def test_cancel_job_sets_cancelled(db_conn, cfg):
    job = services.create_job(db_conn, VALID_URL, False, cfg)
    assert services.cancel_job(db_conn, job["id"]) is True
    assert services.get_job(db_conn, job["id"])["status"] == "CANCELLED"


def test_finish_job_rejects_non_terminal_status(db_conn, cfg):
    job = services.create_job(db_conn, VALID_URL, False, cfg)
    with pytest.raises(services.InvalidTransition):
        services.finish_job(db_conn, job["id"], "QUEUED")


def test_set_stage_updates_fields(db_conn, cfg):
    job = services.create_job(db_conn, VALID_URL, False, cfg)
    services.set_stage(db_conn, job["id"], "DISCOVERING", styles_total=7)
    row = services.get_job(db_conn, job["id"])
    assert row["stage"] == "DISCOVERING"
    assert row["styles_total"] == 7


def test_add_event_rows(db_conn, cfg):
    job = services.create_job(db_conn, VALID_URL, False, cfg)
    services.add_event(db_conn, job["id"], "progress", {"step": 1})
    rows = db_conn.execute(
        "SELECT event, detail_json FROM job_events WHERE job_id = ? ORDER BY id", (job["id"],)
    ).fetchall()
    events = [row["event"] for row in rows]
    assert events[0] == "created"
    assert "progress" in events
    assert json.loads(rows[-1]["detail_json"]) == {"step": 1}


def test_list_recent_order(db_conn, cfg):
    first = services.create_job(db_conn, "https://www.myfonts.com/collections/first", False, cfg)
    second = services.create_job(db_conn, "https://www.myfonts.com/collections/second", False, cfg)
    recent = services.list_recent(db_conn, limit=10)
    assert [row["id"] for row in recent][:2] == [second["id"], first["id"]]
