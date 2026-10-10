"""Deterministic, local-only risk evaluation for INVEST-AI.

This package evaluates proposals. It has no broker, network, LLM, or order
submission capability.
"""

from .engine import ENGINE_VERSION, compute_input_hash, evaluate
from .input_gate import GateResult, GateStatus, parse_trade_proposal
from .models import (
    AssetType,
    MarketState,
    PendingExposure,
    PortfolioState,
    Position,
    RiskDecision,
    RiskPolicy,
    RiskResult,
    RiskRuntimeState,
    RiskViolation,
    Side,
    TradeProposal,
)

__all__ = [
    "ENGINE_VERSION",
    "GateResult",
    "GateStatus",
    "AssetType",
    "MarketState",
    "PendingExposure",
    "PortfolioState",
    "Position",
    "RiskDecision",
    "RiskPolicy",
    "RiskResult",
    "RiskRuntimeState",
    "RiskViolation",
    "Side",
    "TradeProposal",
    "compute_input_hash",
    "evaluate",
    "parse_trade_proposal",
]
