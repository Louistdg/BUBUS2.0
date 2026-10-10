"""Small, deterministic normalizers for provider payloads."""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from invest_ai.data.models import DataContractError, DataProvenance, MarketBar, as_utc
from invest_ai.data.providers import ArchivedRaw, RawPayload


def normalize_market_bar(payload: RawPayload, archived: ArchivedRaw) -> MarketBar:
    """Normalize the documented offline bar fixture shape into the canonical contract."""
    if payload.dataset != "market_bar":
        raise DataContractError("normalize_market_bar only accepts market_bar payloads")
    try:
        body: dict[str, Any] = json.loads(payload.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataContractError("market bar payload is not valid UTF-8 JSON") from exc
    provenance = DataProvenance(
        provider=payload.provider,
        source=payload.source,
        dataset=payload.dataset,
        instrument_id=payload.instrument_id,
        observed_at=payload.observed_at,
        published_at=payload.published_at,
        received_at=payload.received_at,
        available_at=payload.available_at,
        revision=payload.revision,
        currency=payload.currency,
        unit=payload.unit,
        raw_reference=archived.reference,
        raw_sha256=archived.sha256,
        record_id=payload.record_id,
    )

    def decimal_field(name: str) -> Decimal | None:
        value = body.get(name)
        if value is None:
            return None
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise DataContractError(f"{name} must be a decimal string or integer")
        try:
            return Decimal(value)
        except Exception as exc:
            raise DataContractError(f"{name} is not a valid decimal") from exc

    adjusted = body.get("adjusted")
    if adjusted is not None and not isinstance(adjusted, bool):
        raise DataContractError("adjusted must be a JSON boolean or null")

    return MarketBar(
        provenance=provenance,
        interval=str(body["interval"]),
        bar_start=as_utc(body["bar_start"], "bar_start"),
        bar_end=as_utc(body["bar_end"], "bar_end"),
        open=decimal_field("open"),
        high=decimal_field("high"),
        low=decimal_field("low"),
        close=decimal_field("close"),
        volume=decimal_field("volume"),
        adjusted=adjusted,
    )
