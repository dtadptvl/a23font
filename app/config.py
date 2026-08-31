"""Configuration loading and data directory management."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional


class ConfigError(RuntimeError):
    """Raised when configuration is invalid or unusable."""


_SUBDIRS = ("db", "cache", "jobs", "outputs", "logs")
_TRUE_VALUES = {"true", "1", "yes", "on"}
_FALSE_VALUES = {"false", "0", "no", "off"}


def _parse_bool(name: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ConfigError(f"{name}: invalid boolean value {raw!r}")


def _parse_int(name: str, raw: str) -> int:
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name}: invalid integer value {raw!r}") from exc
    if value < 0:
        raise ConfigError(f"{name}: must be >= 0, got {value}")
    return value


class Config:
    """Runtime configuration mirrored from A23FONT_* environment variables."""

    data_root: Path
    http_host: str = "0.0.0.0"
    http_port: int = 8090
    public_base_url: str = "http://localhost:8090"
    http_concurrency: int = 8
    glyph_workers: int = 2
    browser_sessions: int = 1
    atlas_target_mb: int = 96
    atlas_max_mb: int = 128
    checkpoint_batch: int = 16
    execution_budget_minutes: int = 15
    browser_enabled: bool = True
    chromium_path: str = "chromium"
    max_queue: int = 3
    max_active_collections: int = 1
    log_level: str = "INFO"
    pipeline_version: str = "1"

    def __post_init__(self) -> None:
        self.data_root = Path(self.data_root)

    def ensure_dirs(self) -> Dict[str, Path]:
        """Create the data directory layout and verify it is writable."""
        dirs: Dict[str, Path] = {}
        try:
            self.data_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(f"data_root {self.data_root}: cannot create: {exc}") from exc
        for name in _SUBDIRS:
            path = self.data_root / name
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ConfigError(f"cannot create directory {path}: {exc}") from exc
            probe = path / ".a23font_write_probe"
            try:
                probe.write_text("probe", encoding="utf-8")
                probe.unlink()
            except OSError as exc:
                raise ConfigError(f"directory not writable: {path}: {exc}") from exc
            dirs[name] = path
        return dirs

    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Config":
        """Build a Config from A23FONT_* environment variables."""
        source = dict(os.environ) if env is None else dict(env)

        def text(key: str, default: str) -> str:
            value = source.get(key)
            return default if value is None or value == "" else value

        def number(key: str, default: int) -> int:
            value = source.get(key)
            return default if value is None or value == "" else _parse_int(key, value)

        def flag(key: str, default: bool) -> bool:
            value = source.get(key)
            return default if value is None or value == "" else _parse_bool(key, value)

        return cls(
            data_root=Path(text("A23FONT_DATA_ROOT", "./data")),
            http_host=text("A23FONT_HTTP_HOST", "0.0.0.0"),
            http_port=number("A23FONT_HTTP_PORT", 8090),
            public_base_url=text("A23FONT_PUBLIC_BASE_URL", "http://localhost:8090"),
            http_concurrency=number("A23FONT_HTTP_CONCURRENCY", 8),
            glyph_workers=number("A23FONT_GLYPH_WORKERS", 2),
            browser_sessions=number("A23FONT_BROWSER_SESSIONS", 1),
            atlas_target_mb=number("A23FONT_ATLAS_TARGET_MB", 96),
            atlas_max_mb=number("A23FONT_ATLAS_MAX_MB", 128),
            checkpoint_batch=number("A23FONT_CHECKPOINT_BATCH", 16),
            execution_budget_minutes=number("A23FONT_EXECUTION_BUDGET_MINUTES", 15),
            browser_enabled=flag("A23FONT_BROWSER_ENABLED", True),
            chromium_path=text("A23FONT_CHROMIUM_PATH", "chromium"),
            max_queue=number("A23FONT_MAX_QUEUE", 3),
            max_active_collections=number("A23FONT_MAX_ACTIVE_COLLECTIONS", 1),
            log_level=text("A23FONT_LOG_LEVEL", "INFO"),
            pipeline_version=text("A23FONT_PIPELINE_VERSION", "1"),
        )


Config = dataclass(Config)
Config.from_env = classmethod(Config.from_env)
