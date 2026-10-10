"""Application errors with stable machine-readable codes."""
from __future__ import annotations


class InvestAIError(Exception):
    code = "INVEST_AI_ERROR"
    retryable = False

    def __init__(self, message: str, *, context: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.context = context or {}


class ConfigurationError(InvestAIError):
    code = "CONFIGURATION_ERROR"


class AlreadyRunningError(InvestAIError):
    code = "ALREADY_RUNNING"


class StorageError(InvestAIError):
    code = "STORAGE_ERROR"
    retryable = True


class BackupError(InvestAIError):
    code = "BACKUP_ERROR"


class JobStateError(InvestAIError):
    code = "JOB_STATE_ERROR"
