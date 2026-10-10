"""Core application boundaries; document content is never executable."""
"""Local runtime foundations for INVEST-AI."""

from invest_ai.core.clock import iso_utc, parse_utc, to_utc, utc_now
from invest_ai.core.bootstrap import RuntimeContext, bootstrap_runtime
from invest_ai.core.config import AppConfig, load_config
from invest_ai.core.modes import RuntimeMode
from invest_ai.core.paths import RuntimePaths

__all__ = ["AppConfig", "RuntimeContext", "RuntimeMode", "RuntimePaths", "bootstrap_runtime", "iso_utc", "load_config", "parse_utc", "to_utc", "utc_now"]
