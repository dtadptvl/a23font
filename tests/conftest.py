"""Shared pytest fixtures."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.db import open_db
from app.web.main import create_app


def pytest_configure(config):
    # Marker registration lives here (not pytest.ini) because pytest.ini is
    # outside the task scope for pipeline/test work; functionally equivalent.
    config.addinivalue_line(
        "markers",
        "live: tests that hit live external network endpoints (myfonts.com)",
    )


def tmp_data(tmp_path):
    data_root = tmp_path / "data"
    config = Config(data_root=data_root)
    config.ensure_dirs()
    return data_root


tmp_data = pytest.fixture(tmp_data)


def cfg(tmp_data):
    return Config(data_root=tmp_data, max_queue=2, max_active_collections=1)


cfg = pytest.fixture(cfg)


def db_conn(tmp_data):
    conn = open_db(tmp_data / "db" / "a23font.db")
    yield conn
    conn.rollback()
    conn.close()


db_conn = pytest.fixture(db_conn)


def app_client(cfg):
    cfg.ensure_dirs()
    app = create_app(cfg)
    with TestClient(app) as client:
        yield client


app_client = pytest.fixture(app_client)
