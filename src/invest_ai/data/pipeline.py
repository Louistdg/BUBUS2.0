"""Raw-first, read-only provider ingestion pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from invest_ai.data.archive import NormalizedArchive, RawArchive
from invest_ai.data.models import (
    DataContractError, DataObservation, Filing, FundamentalFact, MacroObservation,
    MarketBar, NewsItem, Quote,
)
from invest_ai.data.providers import ArchivedRaw, ProviderAdapter, ProviderRequest, RawPayload


Normalizer = Callable[[RawPayload, ArchivedRaw], DataObservation]


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    status: str
    observation_id: str
    raw_reference: str
    raw_sha256: str
    normalized_reference: str


class DataIngestor:
    def __init__(self, project_root: str | Path) -> None:
        self.raw_archive = RawArchive(project_root)
        self.normalized_archive = NormalizedArchive(project_root)

    def ingest(self, adapter: ProviderAdapter, request: ProviderRequest,
               normalizers: Mapping[str, Normalizer]) -> list[IngestOutcome]:
        metadata = adapter.metadata
        if getattr(adapter, "requires_official_ingestor", False):
            raise DataContractError(
                "official adapters require OfficialDataIngestor to preserve request and license metadata"
            )
        if not metadata.read_only:
            raise DataContractError("only read-only adapters may be ingested")
        outcomes: list[IngestOutcome] = []
        for payload in adapter.fetch(request):
            if payload.provider != metadata.provider:
                raise DataContractError("payload provider differs from adapter metadata")
            if payload.dataset not in metadata.supported_datasets or payload.dataset not in request.datasets:
                raise DataContractError("adapter returned an undeclared or unrequested dataset")
            if payload.dataset not in normalizers:
                raise DataContractError(f"no normalizer registered for {payload.dataset}")
            archived = self.raw_archive.archive(payload)
            # The raw file is durable before the normalizer is called.
            observation = normalizers[payload.dataset](payload, archived)
            self._verify_binding(payload, archived.reference, archived.sha256, observation)
            normalized = self.normalized_archive.append(observation)
            outcomes.append(IngestOutcome(
                status=normalized.status,
                observation_id=observation.provenance.observation_id,
                raw_reference=archived.reference,
                raw_sha256=archived.sha256,
                normalized_reference=normalized.reference,
            ))
        return outcomes

    @staticmethod
    def _verify_binding(payload: RawPayload, reference: str, digest: str,
                        observation: DataObservation) -> None:
        provenance = observation.provenance
        expected = (
            payload.provider, payload.source, payload.dataset, payload.instrument_id,
            payload.observed_at, payload.published_at, payload.received_at,
            payload.available_at, payload.revision, payload.record_id,
        )
        actual = (
            provenance.provider, provenance.source, provenance.dataset, provenance.instrument_id,
            provenance.observed_at, provenance.published_at, provenance.received_at,
            provenance.available_at, provenance.revision, provenance.record_id,
        )
        if expected != actual:
            raise DataContractError("normalized provenance does not match provider payload")
        expected_values = (payload.currency.upper() if payload.currency else None, payload.unit)
        actual_values = (provenance.currency, provenance.unit)
        if expected_values != actual_values:
            raise DataContractError("normalized currency/unit do not match provider payload")
        if provenance.raw_reference != reference or provenance.raw_sha256 != digest:
            raise DataContractError("normalized observation is not bound to archived raw payload")
        expected_types = {
            "market_bar": MarketBar,
            "quote": Quote,
            "fundamental_fact": FundamentalFact,
            "macro_observation": MacroObservation,
            "news_item": NewsItem,
            "filing": Filing,
        }
        expected_type = expected_types.get(payload.dataset)
        if expected_type is not None and not isinstance(observation, expected_type):
            raise DataContractError(
                f"dataset {payload.dataset} requires {expected_type.__name__}"
            )
