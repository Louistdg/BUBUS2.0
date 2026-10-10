"""Execution modes intentionally available in INVEST-AI V0."""
from __future__ import annotations

from enum import StrEnum


class RuntimeMode(StrEnum):
    RESEARCH = "RESEARCH"
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"


def parse_mode(value: str) -> RuntimeMode:
    """Parse a configured mode. LIVE and unknown modes are deliberately rejected."""
    try:
        return RuntimeMode(value.strip().upper())
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"Mode interdit ou inconnu : {value!r}") from exc
