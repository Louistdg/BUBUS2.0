"""Compatibility launcher for the former demo entry point.

The demo UI and simulation routes have been retired. Running this module now
starts the read-only market dashboard.
"""
from invest_ai.assistant.market_dashboard import main

if __name__ == "__main__":
    raise SystemExit(main())
