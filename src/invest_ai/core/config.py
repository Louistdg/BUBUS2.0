"""Small, strict TOML configuration with safe defaults and no secret fields."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from invest_ai.core.errors import ConfigurationError
from invest_ai.core.modes import RuntimeMode, parse_mode
from invest_ai.core.paths import RuntimePaths

_ALLOWED_TOP_LEVEL = {"runtime", "health", "jobs", "logging"}
_ALLOWED_KEYS = {
    "runtime": {"mode"},
    "health": {"stale_after_seconds"},
    "jobs": {"lease_seconds", "max_attempts", "retry_base_seconds", "retry_max_seconds"},
    "logging": {"level", "max_bytes", "backup_count"},
}
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


@dataclass(frozen=True, slots=True)
class AppConfig:
    paths: RuntimePaths
    mode: RuntimeMode = RuntimeMode.RESEARCH
    stale_after_seconds: int = 120
    job_lease_seconds: int = 300
    job_max_attempts: int = 5
    retry_base_seconds: int = 2
    retry_max_seconds: int = 300
    log_level: str = "INFO"
    log_max_bytes: int = 1_048_576
    log_backup_count: int = 5


def _positive_int(table: dict, key: str, default: int, *, allow_zero: bool = False) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{key} doit être un entier.")
    if value < (0 if allow_zero else 1):
        raise ConfigurationError(f"{key} est hors limites.")
    return value


def load_config(
    config_path: str | Path | None,
    root: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load strict TOML. Only mode and log level accept environment overrides."""
    env = os.environ if environ is None else environ
    raw: dict = {}
    if config_path is not None and Path(config_path).exists():
        try:
            with Path(config_path).open("rb") as stream:
                raw = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(f"Configuration illisible : {exc}") from exc

    if not isinstance(raw, dict) or set(raw) - _ALLOWED_TOP_LEVEL:
        unknown = sorted(set(raw) - _ALLOWED_TOP_LEVEL) if isinstance(raw, dict) else []
        raise ConfigurationError(f"Sections inconnues : {unknown}")
    tables: dict[str, dict] = {}
    for section in _ALLOWED_TOP_LEVEL:
        table = raw.get(section, {})
        if not isinstance(table, dict):
            raise ConfigurationError(f"La section [{section}] doit être une table TOML.")
        unknown = set(table) - _ALLOWED_KEYS[section]
        if unknown:
            raise ConfigurationError(f"Clés inconnues dans [{section}] : {sorted(unknown)}")
        tables[section] = table

    runtime, health, jobs, logging = (tables[name] for name in ("runtime", "health", "jobs", "logging"))
    try:
        mode = parse_mode(env.get("INVEST_AI_MODE", runtime.get("mode", "RESEARCH")))
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc
    log_level = str(env.get("INVEST_AI_LOG_LEVEL", logging.get("level", "INFO"))).upper()
    if log_level not in _LOG_LEVELS:
        raise ConfigurationError(f"Niveau de journalisation interdit : {log_level!r}")

    config = AppConfig(
        paths=RuntimePaths.from_root(root),
        mode=mode,
        stale_after_seconds=_positive_int(health, "stale_after_seconds", 120),
        job_lease_seconds=_positive_int(jobs, "lease_seconds", 300),
        job_max_attempts=_positive_int(jobs, "max_attempts", 5),
        retry_base_seconds=_positive_int(jobs, "retry_base_seconds", 2),
        retry_max_seconds=_positive_int(jobs, "retry_max_seconds", 300),
        log_level=log_level,
        log_max_bytes=_positive_int(logging, "max_bytes", 1_048_576),
        log_backup_count=_positive_int(logging, "backup_count", 5, allow_zero=True),
    )
    if config.retry_max_seconds < config.retry_base_seconds:
        raise ConfigurationError("retry_max_seconds doit être supérieur ou égal à retry_base_seconds.")
    return config
