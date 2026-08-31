"""FastAPI application factory and HTTP routes."""
from __future__ import annotations

import logging
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import services
from ..config import Config
from ..db import open_db
from ..logging_conf import log_event
from ..security import SourceError

BASE_DIR = Path(__file__).resolve().parents[2]
DOWNLOAD_STATUSES = ("DONE", "DONE_WITH_ERRORS")

logger = logging.getLogger("a23font.web")


def _form_flag(value: Optional[str]) -> bool:
    if value is None:
        return False
    return value.strip().lower() not in ("", "0", "false", "no", "off")


def create_app(cfg: Config) -> FastAPI:
    """Build the FastAPI application for the given configuration."""
    app = FastAPI(title="A23Font", openapi_url=None, docs_url=None, redoc_url=None)
    app.state.cfg = cfg
    app.state.db_path = cfg.data_root / "db" / "a23font.db"

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    def db_session() -> Iterator[sqlite3.Connection]:
        conn = open_db(app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    db_session = contextmanager(db_session)

    def get_db():
        """FastAPI dependency: one fresh SQLite connection per request."""
        with db_session() as conn:
            yield conn

    def render(request: Request, name: str, context: dict, status_code: int = 200):
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    async def request_timing(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = int((time.perf_counter() - started) * 1000)
        log_event(
            logger,
            "http_request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
        )
        return response

    app.middleware("http")(request_timing)

    def health_live():
        return {"status": "ok", "service": "a23font"}

    app.get("/health/live")(health_live)

    def health_ready():
        try:
            with db_session() as conn:
                conn.execute("SELECT 1").fetchone()
            outputs = cfg.data_root / "outputs"
            outputs.mkdir(parents=True, exist_ok=True)
            probe = outputs / ".a23font_ready_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except Exception as exc:  # noqa: BLE001 - any failure means not ready
            return JSONResponse(status_code=503, content={"status": "not_ready", "reason": str(exc)})
        return {"status": "ready"}

    app.get("/health/ready")(health_ready)

    def home(request: Request):
        context = {
            "error": None,
            "url": "",
            "vietnamese": False,
            "base_url": cfg.public_base_url,
        }
        return render(request, "home.html", context)

    app.get("/")(home)

    def create_job_route(
        request: Request,
        url: str = Form(...),
        vietnamese: Optional[str] = Form(default=None),
        conn: sqlite3.Connection = Depends(get_db),
    ):
        flag = _form_flag(vietnamese)
        context = {
            "url": url,
            "vietnamese": flag,
            "base_url": cfg.public_base_url,
        }
        try:
            job = services.create_job(conn, url, flag, cfg)
        except SourceError as exc:
            context["error"] = f"{exc.code}: {exc}"
            return render(request, "home.html", context, status_code=400)
        except services.QueueFullError as exc:
            context["error"] = f"queue full: {exc}"
            return render(request, "home.html", context, status_code=429)
        return RedirectResponse(url=f"/jobs/{job['id']}", status_code=303)

    app.post("/jobs")(create_job_route)

    def job_page(request: Request, job_id: str, conn: sqlite3.Connection = Depends(get_db)):
        job = services.get_job(conn, job_id)
        if job is None:
            return render(
                request,
                "404.html",
                {"message": f"job {job_id} not found", "base_url": cfg.public_base_url},
                status_code=404,
            )
        style_rows = conn.execute(
            "SELECT * FROM style_jobs WHERE job_id = ? ORDER BY position", (job_id,)
        ).fetchall()
        job["terminal"] = job["status"] in services.TERMINAL
        download_ready = False
        if job["status"] in DOWNLOAD_STATUSES and job.get("zip_name"):
            download_ready = (cfg.data_root / "outputs" / Path(job["zip_name"]).name).is_file()
        context = {
            "job": job,
            "styles": [dict(row) for row in style_rows],
            "download_ready": download_ready,
            "base_url": cfg.public_base_url,
        }
        return render(request, "job.html", context)

    app.get("/jobs/{job_id}")(job_page)

    def job_status(job_id: str, conn: sqlite3.Connection = Depends(get_db)):
        job = services.get_job(conn, job_id)
        if job is None:
            return JSONResponse(status_code=404, content={"error": "not_found", "job_id": job_id})
        rows = conn.execute(
            "SELECT name, status, error_message, cache_hit FROM style_jobs"
            " WHERE job_id = ? ORDER BY position",
            (job_id,),
        ).fetchall()
        styles = [
            {
                "name": row["name"],
                "status": row["status"],
                "error_message": row["error_message"],
                "cache_hit": bool(row["cache_hit"]),
            }
            for row in rows
        ]
        return {
            "id": job["id"],
            "status": job["status"],
            "stage": job["stage"],
            "styles_total": job["styles_total"],
            "styles_done": job["styles_done"],
            "styles_failed": job["styles_failed"],
            "cancel_requested": bool(job["cancel_requested"]),
            "error_code": job["error_code"],
            "error_message": job["error_message"],
            "styles": styles,
            "terminal": job["status"] in services.TERMINAL,
        }

    app.get("/jobs/{job_id}/status")(job_status)

    def cancel_job_route(job_id: str, conn: sqlite3.Connection = Depends(get_db)):
        try:
            services.request_cancel(conn, job_id)
        except services.NotFoundError:
            return JSONResponse(status_code=404, content={"error": "not_found", "job_id": job_id})
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    app.post("/jobs/{job_id}/cancel")(cancel_job_route)

    def download(job_id: str, conn: sqlite3.Connection = Depends(get_db)):
        job = services.get_job(conn, job_id)
        if job is None:
            return JSONResponse(status_code=404, content={"error": "not_found", "job_id": job_id})
        if job["status"] not in DOWNLOAD_STATUSES:
            return JSONResponse(
                status_code=409,
                content={"error": "job_not_finished", "status": job["status"]},
            )
        zip_name = job.get("zip_name")
        if not zip_name:
            return JSONResponse(status_code=409, content={"error": "no_artifact"})
        safe_name = Path(zip_name).name
        if not safe_name:
            return JSONResponse(status_code=409, content={"error": "no_artifact"})
        artifact = cfg.data_root / "outputs" / safe_name
        if not artifact.is_file():
            return JSONResponse(status_code=404, content={"error": "artifact_missing"})
        return FileResponse(str(artifact), media_type="application/zip", filename=safe_name)

    app.get("/jobs/{job_id}/download")(download)

    def _dir_size(path: Path) -> int:
        total = 0
        if not path.is_dir():
            return 0
        for entry in path.rglob("*"):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
        return total

    def ops(request: Request, conn: sqlite3.Connection = Depends(get_db)):
        try:
            disk = shutil.disk_usage(str(cfg.data_root))
        except OSError:
            disk = shutil.disk_usage(str(BASE_DIR))
        sizes = {name: _dir_size(cfg.data_root / name) for name in ("cache", "outputs", "jobs")}
        count_rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
        ).fetchall()
        context = {
            "data_root": str(cfg.data_root),
            "disk": disk,
            "sizes": sizes,
            "counts": {row["status"]: row["n"] for row in count_rows},
            "statuses": services.JOB_STATUSES,
            "recent": services.list_recent(conn, limit=10),
            "base_url": cfg.public_base_url,
        }
        return render(request, "ops.html", context)

    app.get("/ops")(ops)

    return app
