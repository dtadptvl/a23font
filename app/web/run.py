"""Server entry point: python -m app.web.run"""
from __future__ import annotations

import uvicorn

from ..config import Config
from ..logging_conf import setup_logging
from .main import create_app


def main() -> None:
    cfg = Config.from_env()
    dirs = cfg.ensure_dirs()
    setup_logging(cfg.log_level, logfile=dirs["logs"] / "a23font.log")
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.http_host, port=cfg.http_port)


if __name__ == "__main__":
    main()
