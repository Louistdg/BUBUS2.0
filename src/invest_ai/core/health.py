"""In-process health registry; no network endpoint is started by this module."""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from enum import StrEnum

from invest_ai.core.clock import iso_utc, utc_now
from invest_ai.core.logging import redact


class HealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    UNKNOWN = "UNKNOWN"


_SEVERITY = {HealthStatus.HEALTHY: 0, HealthStatus.UNKNOWN: 1, HealthStatus.DEGRADED: 2, HealthStatus.UNHEALTHY: 3}


@dataclass(frozen=True, slots=True)
class HealthCheck:
    component: str
    status: HealthStatus
    checked_at: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class HealthReport:
    status: HealthStatus
    checked_at: str
    checks: tuple[HealthCheck, ...]

    def to_dict(self) -> dict:
        return {"status": self.status.value, "checked_at": self.checked_at, "checks": [
            {**asdict(check), "status": check.status.value} for check in self.checks
        ]}


class HealthRegistry:
    def __init__(self) -> None:
        self._checks: dict[str, HealthCheck] = {}
        self._lock = threading.Lock()

    def set(self, component: str, status: HealthStatus, detail: str | None = None) -> None:
        if not component.strip():
            raise ValueError("Le composant de santé doit être nommé.")
        if detail is not None and len(detail) > 240:
            detail = detail[:240]
        if detail is not None:
            detail = redact(detail)
        check = HealthCheck(component=component, status=status, checked_at=iso_utc(utc_now()), detail=detail)
        with self._lock:
            self._checks[component] = check

    def snapshot(self) -> HealthReport:
        with self._lock:
            checks = tuple(sorted(self._checks.values(), key=lambda item: item.component))
        status = max((check.status for check in checks), key=lambda item: _SEVERITY[item], default=HealthStatus.UNKNOWN)
        return HealthReport(status=status, checked_at=iso_utc(utc_now()), checks=checks)
