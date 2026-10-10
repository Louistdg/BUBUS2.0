"""Central UTC clock helpers."""
from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Un horodatage sans fuseau est interdit.")
    return value.astimezone(UTC)


def iso_utc(value: datetime | None = None) -> str:
    instant = to_utc(value or utc_now())
    return instant.isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return to_utc(parsed)
