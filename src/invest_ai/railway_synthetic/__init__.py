"""Isolated, synthetic-only coordination projection prototype.

This package deliberately does not import the operational Coordination Core.
"""

from .contract import ContractError, build_snapshot, validate_snapshot
from .postgres import PostgresProjectionStore
from .store import IngestConflict, SyntheticProjectionStore

__all__ = [
    "ContractError",
    "IngestConflict",
    "PostgresProjectionStore",
    "SyntheticProjectionStore",
    "build_snapshot",
    "validate_snapshot",
]
