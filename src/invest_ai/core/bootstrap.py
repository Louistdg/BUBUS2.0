"""Explicit runtime bootstrap; importing the package never starts processes."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from invest_ai.core.config import AppConfig, load_config
from invest_ai.core.health import HealthRegistry, HealthStatus
from invest_ai.core.jobs import RuntimeStore
from invest_ai.core.logging import configure_logging
from invest_ai.core.paths import RuntimePaths
from invest_ai.core.single_instance import InstanceGuard


@dataclass(slots=True)
class RuntimeContext:
    config: AppConfig
    paths: RuntimePaths
    store: RuntimeStore
    health: HealthRegistry
    logger: logging.Logger
    instance_guard: InstanceGuard
    recovered_jobs: int

    def close(self) -> None:
        self.instance_guard.release()
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)
            handler.close()

    def __enter__(self) -> "RuntimeContext":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def bootstrap_runtime(root: str | Path, config_path: str | Path | None = None) -> RuntimeContext:
    """Initialize local runtime facilities and recover expired work; no worker is started."""
    paths = RuntimePaths.from_root(root)
    config = load_config(config_path, paths.root)
    paths.ensure_runtime_directories()
    guard = InstanceGuard(paths.root).acquire()
    try:
        logger = configure_logging(
            paths.logs_dir,
            config.log_level,
            max_bytes=config.log_max_bytes,
            backup_count=config.log_backup_count,
        )
        store = RuntimeStore(paths.runtime_db)
        recovered = store.recover_expired()
        health = HealthRegistry()
        health.set("runtime", HealthStatus.HEALTHY, f"mode={config.mode.value}")
        health.set("runtime_store", HealthStatus.HEALTHY, f"recovered_jobs={recovered}")
        logger.info("runtime_bootstrapped", extra={"event": "runtime_bootstrapped", "component": "core", "status": config.mode.value})
        return RuntimeContext(config, paths, store, health, logger, guard, recovered)
    except Exception:
        guard.release()
        for handler in list(logging.getLogger("invest_ai").handlers):
            logging.getLogger("invest_ai").removeHandler(handler)
            handler.close()
        raise
