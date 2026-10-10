"""Paper-only deterministic execution simulation contracts."""
from invest_ai.execution.models import (
    ExecutionContractError,
    ExecutionMode,
    ExecutionStatus,
    OrderType,
    PaperExecutionPolicy,
    PaperExecutionRequest,
    PaperExecutionResult,
    PaperFill,
    PaperMarketSnapshot,
)
from invest_ai.execution.simulator import compute_execution_input_hash, simulate_paper_execution

__all__ = [
    "ExecutionContractError", "ExecutionMode", "ExecutionStatus", "OrderType",
    "PaperExecutionPolicy", "PaperExecutionRequest", "PaperExecutionResult",
    "PaperFill", "PaperMarketSnapshot", "compute_execution_input_hash",
    "simulate_paper_execution",
]
