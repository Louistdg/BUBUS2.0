"""Experimental, non-authoritative research strategies."""

from .breakout import (
    BreakoutPolicy,
    BreakoutSignalObservation,
    CandidateProposal,
    SignalEvaluation,
    SignalStatus,
    StrategyContractError,
    evaluate_breakout,
    evaluate_breakout_signal,
)

__all__ = [
    "BreakoutPolicy", "BreakoutSignalObservation", "CandidateProposal", "SignalEvaluation", "SignalStatus",
    "StrategyContractError", "evaluate_breakout", "evaluate_breakout_signal",
]
