"""Point-in-time selection and freshness helpers."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from invest_ai.data.models import (
    DataContractError, DataObservation, as_utc, canonical_json_bytes,
)


def select_as_of(observations: Iterable[DataObservation],
                 as_of: datetime | str) -> list[DataObservation]:
    """Select the latest eligible revision per logical record at the requested instant."""
    cutoff = as_utc(as_of, "as_of")
    latest: dict[str, DataObservation] = {}
    for observation in observations:
        provenance = observation.provenance
        if provenance.available_at > cutoff:
            continue
        previous = latest.get(provenance.logical_key)
        if previous is None:
            latest[provenance.logical_key] = observation
            continue
        prior_provenance = previous.provenance
        if (provenance.revision, provenance.available_at, provenance.received_at) > (
            prior_provenance.revision, prior_provenance.available_at, prior_provenance.received_at
        ):
            latest[provenance.logical_key] = observation
        elif provenance.revision == prior_provenance.revision:
            if (provenance.observation_id != prior_provenance.observation_id
                    or canonical_json_bytes(observation) != canonical_json_bytes(previous)):
                raise DataContractError("conflicting observations share provider revision and logical key")
    return sorted(latest.values(), key=lambda item: item.provenance.observation_id)


def is_stale(observation: DataObservation, at: datetime | str,
             max_age: timedelta) -> bool:
    if max_age < timedelta(0):
        raise DataContractError("max_age cannot be negative")
    cutoff = as_utc(at, "at")
    return observation.provenance.observed_at < cutoff - max_age
