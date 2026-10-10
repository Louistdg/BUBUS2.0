"""Stable and random identifiers for runtime records."""
from __future__ import annotations

import uuid

_NAMESPACE = uuid.UUID("4ed45fe1-8bcb-442d-a170-1ab0d19bd03a")


def stable_id(kind: str, *parts: object) -> str:
    """Return a deterministic UUID for the same kind and ordered values."""
    material = "\x1f".join(str(part) for part in parts)
    return str(uuid.uuid5(_NAMESPACE, f"{kind}\x1e{material}"))


def new_id() -> str:
    return str(uuid.uuid4())
