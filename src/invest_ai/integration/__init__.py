"""Canonical adapters between INVEST-AI domain contracts."""

from invest_ai.integration.gateway import (
    CurrencyMismatchError,
    IntegrationContractError,
    MarketRiskMapping,
    PolicyEvaluationInput,
    RiskInputBundle,
    TrustedRiskAuthorityRequired,
    TrustedPortfolioSnapshot,
    build_policy_evaluation_input,
    build_risk_input_bundle,
    build_risk_portfolio_state,
    canonical_policy_input_bytes,
    compute_policy_input_hash,
    compute_proposal_hash,
    map_market_snapshot_to_risk,
)

__all__ = [
    "CurrencyMismatchError",
    "IntegrationContractError",
    "MarketRiskMapping",
    "PolicyEvaluationInput",
    "RiskInputBundle",
    "TrustedRiskAuthorityRequired",
    "TrustedPortfolioSnapshot",
    "build_policy_evaluation_input",
    "build_risk_input_bundle",
    "build_risk_portfolio_state",
    "canonical_policy_input_bytes",
    "compute_policy_input_hash",
    "compute_proposal_hash",
    "map_market_snapshot_to_risk",
]
