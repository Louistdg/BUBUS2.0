"""Read-only provider adapter contracts and offline fixtures."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Protocol, runtime_checkable

from invest_ai.data.models import DataContractError, ProviderMetadata, as_utc


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    datasets: tuple[str, ...]
    instrument_ids: tuple[str, ...] = ()
    start_at: datetime | None = None
    end_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.datasets or any(not item.strip() for item in self.datasets):
            raise DataContractError("at least one non-empty dataset is required")
        object.__setattr__(self, "datasets", tuple(item.strip() for item in self.datasets))
        object.__setattr__(self, "instrument_ids", tuple(self.instrument_ids))
        for name in ("start_at", "end_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, as_utc(value, name))
        if self.start_at and self.end_at and self.end_at < self.start_at:
            raise DataContractError("end_at cannot precede start_at")


@dataclass(frozen=True, slots=True)
class RawPayload:
    provider: str
    dataset: str
    source: str
    record_id: str
    body: bytes
    observed_at: datetime
    received_at: datetime
    available_at: datetime
    revision: int = 1
    instrument_id: str | None = None
    published_at: datetime | None = None
    currency: str | None = None
    unit: str | None = None
    media_type: str = "application/octet-stream"

    def __post_init__(self) -> None:
        for name in ("provider", "dataset", "source", "record_id", "media_type"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DataContractError(f"{name} is required")
            object.__setattr__(self, name, value.strip())
        if not isinstance(self.body, bytes) or not self.body:
            raise DataContractError("raw payload body must be non-empty bytes")
        for name in ("observed_at", "received_at", "available_at"):
            object.__setattr__(self, name, as_utc(getattr(self, name), name))
        if self.published_at is not None:
            object.__setattr__(self, "published_at", as_utc(self.published_at, "published_at"))
        if self.available_at < self.received_at:
            raise DataContractError("available_at cannot precede received_at")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise DataContractError("revision must be a positive integer")


@dataclass(frozen=True, slots=True)
class ArchivedRaw:
    reference: str
    sha256: str
    size_bytes: int


@runtime_checkable
class ProviderAdapter(Protocol):
    """Read-only surface: adapters can fetch data but expose no financial actions."""

    @property
    def metadata(self) -> ProviderMetadata:
        ...

    def fetch(self, request: ProviderRequest) -> Iterable[RawPayload]:
        ...


class OfflineFixtureProvider:
    """Deterministic local adapter for fixtures; it never opens a network connection."""

    def __init__(self, metadata: ProviderMetadata, payloads: Iterable[RawPayload]) -> None:
        if not metadata.read_only:
            raise DataContractError("provider metadata must declare read_only")
        self._metadata = metadata
        self._payloads = tuple(payloads)
        allowed = set(metadata.supported_datasets)
        if any(payload.provider != metadata.provider for payload in self._payloads):
            raise DataContractError("fixture provider does not match payload provider")
        if any(payload.dataset not in allowed for payload in self._payloads):
            raise DataContractError("fixture dataset is not declared in provider metadata")

    @property
    def metadata(self) -> ProviderMetadata:
        return self._metadata

    def fetch(self, request: ProviderRequest) -> Iterable[RawPayload]:
        if not set(request.datasets).issubset(self._metadata.supported_datasets):
            raise DataContractError("requested dataset is not supported by this provider")
        instruments = set(request.instrument_ids)
        for payload in self._payloads:
            if payload.dataset not in request.datasets:
                continue
            if instruments and payload.instrument_id not in instruments:
                continue
            if request.start_at and payload.observed_at < request.start_at:
                continue
            if request.end_at and payload.observed_at > request.end_at:
                continue
            yield payload
