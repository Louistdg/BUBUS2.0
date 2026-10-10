"""Small, read-only adapters for approved official data sources.

Network access is opt-in through an injected transport. Tests and callers can
use an in-memory transport; this module never connects to a financial account
or performs financial actions.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol, Sequence

from invest_ai.data.archive import AppendResult, NormalizedArchive, RawArchive
from invest_ai.data.models import (
    DataContractError, DataObservation, DataProvenance, Filing, FundamentalFact,
    MacroObservation, ProviderMetadata, as_utc, observation_to_dict,
)
from invest_ai.data.providers import ArchivedRaw, ProviderAdapter, ProviderRequest, RawPayload


UTC = timezone.utc
SEC_EST = timezone(-timedelta(hours=5), name="EST")
SEC_BASE = "https://data.sec.gov"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
GLEIF_BASE = "https://api.gleif.org/api/v1"
ECB_BASE = "https://data-api.ecb.europa.eu/service"


class OfficialAdapterError(RuntimeError):
    """An official source response, configuration, or license check failed."""


class LicenseBlocked(OfficialAdapterError):
    """The source registry does not authorize the requested archive/use."""


@dataclass(frozen=True, slots=True)
class SourceLicense:
    source_id: str
    status: str
    terms_url: str
    terms_id: str
    raw_archive_allowed: bool
    commercial_use: bool
    attribution_required: bool
    third_party_exclusions: tuple[str, ...]
    notes: str
    approved_series: tuple[str, ...] = ()


class SourceRegistry:
    """Machine-readable license gate. Missing or malformed entries fail closed."""

    def __init__(self, sources: Mapping[str, SourceLicense]) -> None:
        self._sources = dict(sources)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "SourceRegistry":
        registry_path = Path(path) if path else (
            Path(__file__).resolve().with_name("source_registry.json")
        )
        try:
            document = json.loads(registry_path.read_text(encoding="utf-8"))
            raw_sources = document["sources"]
            sources: dict[str, SourceLicense] = {}
            for key, item in raw_sources.items():
                if item.get("source_id") != key:
                    raise ValueError("source_id does not match registry key")
                sources[key] = SourceLicense(
                    source_id=key,
                    status=str(item["status"]),
                    terms_url=str(item["terms_url"]),
                    terms_id=str(item["terms_id"]),
                    raw_archive_allowed=item["raw_archive_allowed"] is True,
                    commercial_use=item["commercial_use"] is True,
                    attribution_required=item["attribution_required"] is True,
                    third_party_exclusions=tuple(item["third_party_exclusions"]),
                    notes=str(item["notes"]),
                    approved_series=tuple(item.get("approved_series", ())),
                )
            return cls(sources)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LicenseBlocked("source license registry is missing or invalid") from exc

    def require(self, source_id: str, *, archive_raw: bool = True,
                commercial_use: bool = True, third_party_data: bool = False) -> SourceLicense:
        license_item = self.entry(source_id)
        if license_item.status != "GREEN":
            raise LicenseBlocked(f"source status does not authorize ingestion: {source_id}")
        if archive_raw and not license_item.raw_archive_allowed:
            raise LicenseBlocked(f"raw archive is not authorized: {source_id}")
        if commercial_use and not license_item.commercial_use:
            raise LicenseBlocked(f"commercial use is not authorized: {source_id}")
        if third_party_data and license_item.third_party_exclusions:
            raise LicenseBlocked(f"third-party data is excluded for source: {source_id}")
        return license_item

    def entry(self, source_id: str) -> SourceLicense:
        license_item = self._sources.get(source_id)
        if license_item is None:
            raise LicenseBlocked(f"source is not present in license registry: {source_id}")
        return license_item


@dataclass(frozen=True, slots=True)
class HttpResponse:
    body: bytes
    headers: Mapping[str, str]
    status: int = 200


class HttpTransport(Protocol):
    def get(self, url: str, *, params: Mapping[str, str],
            headers: Mapping[str, str]) -> HttpResponse: ...


class UrllibTransport:
    """Minimal GET-only transport. No retries, cookies, credentials, or writes."""

    def get(self, url: str, *, params: Mapping[str, str],
            headers: Mapping[str, str]) -> HttpResponse:
        query = urllib.parse.urlencode(params)
        full_url = f"{url}{'&' if '?' in url else '?'}{query}" if query else url
        request = urllib.request.Request(full_url, headers=dict(headers), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return HttpResponse(
                    response.read(),
                    {key: value for key, value in response.headers.items()},
                    response.status,
                )
        except urllib.error.HTTPError as exc:
            # Do not include request headers or query values in error text.
            raise OfficialAdapterError(f"official source returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OfficialAdapterError("official source request failed") from exc


@dataclass(frozen=True, slots=True)
class OfficialRawEnvelope:
    payload: RawPayload
    request_url: str
    request_parameters: Mapping[str, str]
    request_headers: Mapping[str, str]
    response_headers: Mapping[str, str]
    source_schema_version: str
    terms_id: str


def _source_headers(headers: Mapping[str, str]) -> dict[str, str]:
    allowed = {"date", "content-type", "content-length", "etag", "last-modified", "content-encoding"}
    return {key: value for key, value in headers.items() if key.lower() in allowed}


def _response_envelope(*, source_id: str, dataset: str, record_id: str,
                       response: HttpResponse, url: str, params: Mapping[str, str],
                       request_headers: Mapping[str, str], received_at: datetime,
                       schema_version: str, license_item: SourceLicense,
                       observed_at: datetime | None = None,
                       published_at: datetime | None = None,
                       revision: int = 1, media_type: str | None = None) -> OfficialRawEnvelope:
    received = as_utc(received_at, "received_at")
    payload = RawPayload(
        provider=source_id,
        dataset=dataset,
        source=url,
        record_id=record_id,
        body=response.body,
        observed_at=as_utc(observed_at or received, "observed_at"),
        published_at=as_utc(published_at, "published_at") if published_at else None,
        received_at=received,
        available_at=received,
        revision=revision,
        media_type=media_type or next(
            (value.split(";", 1)[0].strip() for key, value in response.headers.items()
             if key.lower() == "content-type"),
            "application/octet-stream",
        ),
    )
    return OfficialRawEnvelope(
        payload=payload,
        request_url=url,
        request_parameters=dict(params),
        request_headers=dict(request_headers),
        response_headers=_source_headers(response.headers),
        source_schema_version=schema_version,
        terms_id=license_item.terms_id,
    )


class OfficialAdapterBase(ProviderAdapter):
    requires_official_ingestor = True
    source_id: str
    supported: tuple[str, ...]
    terms_url: str
    schema_version: str

    def __init__(self, *, transport: HttpTransport | None = None,
                 registry: SourceRegistry | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._transport = transport or UrllibTransport()
        self._registry = registry or SourceRegistry.load()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._metadata = ProviderMetadata(
            provider=self.source_id,
            adapter_version="0.1.0",
            supported_datasets=self.supported,
            read_only=True,
            supports_realtime=False,
            supports_websocket=False,
            terms_url=self.terms_url,
            notes="Official-source GET only; no trading, accounts, or credentials.",
        )

    @property
    def metadata(self) -> ProviderMetadata:
        return self._metadata

    def fetch(self, request: ProviderRequest) -> Iterable[RawPayload]:
        return (envelope.payload for envelope in self.fetch_envelopes(request))

    def _request(self, url: str, params: Mapping[str, str],
                 request_headers: Mapping[str, str], *, dataset: str,
                 record_id: str, observed_at: datetime | None = None,
                 revision: int = 1, media_type: str | None = None) -> OfficialRawEnvelope:
        response = self._transport.get(url, params=params, headers=request_headers)
        if response.status < 200 or response.status >= 300:
            raise OfficialAdapterError(f"official source returned HTTP {response.status}")
        if not response.body:
            raise OfficialAdapterError("official source returned an empty body")
        license_item = self._registry.require(self.source_id)
        received_at = self._clock()
        available_at = self._clock()
        if as_utc(available_at, "available_at") < as_utc(received_at, "received_at"):
            raise OfficialAdapterError("injected clock moved backwards during response handling")
        envelope = _response_envelope(
            source_id=self.source_id, dataset=dataset, record_id=record_id,
            response=response, url=url, params=params, request_headers=request_headers,
            received_at=received_at, schema_version=self.schema_version,
            license_item=license_item, observed_at=observed_at, revision=revision,
            media_type=media_type,
        )
        if available_at != received_at:
            payload = RawPayload(
                provider=envelope.payload.provider, dataset=envelope.payload.dataset,
                source=envelope.payload.source, record_id=envelope.payload.record_id,
                body=envelope.payload.body, observed_at=envelope.payload.observed_at,
                published_at=envelope.payload.published_at, received_at=envelope.payload.received_at,
                available_at=as_utc(available_at, "available_at"), revision=envelope.payload.revision,
                instrument_id=envelope.payload.instrument_id, currency=envelope.payload.currency,
                unit=envelope.payload.unit, media_type=envelope.payload.media_type,
            )
            envelope = OfficialRawEnvelope(
                payload=payload, request_url=envelope.request_url,
                request_parameters=envelope.request_parameters,
                request_headers=envelope.request_headers,
                response_headers=envelope.response_headers,
                source_schema_version=envelope.source_schema_version,
                terms_id=envelope.terms_id,
            )
        return envelope

    def fetch_envelopes(self, request: ProviderRequest) -> Iterable[OfficialRawEnvelope]:
        raise NotImplementedError


class GleifAdapter(OfficialAdapterBase):
    source_id = "gleif"
    supported = ("entity_reference",)
    terms_url = "https://www.gleif.org/en/meta/lei-data-terms-of-use/"
    schema_version = "GLEIF_LEI_API_V1_JSONAPI"

    def fetch_envelopes(self, request: ProviderRequest) -> Iterable[OfficialRawEnvelope]:
        self._registry.require(self.source_id)
        if set(request.datasets) != {"entity_reference"}:
            raise DataContractError("GLEIF adapter only accepts entity_reference")
        for lei in request.instrument_ids:
            if not re.fullmatch(r"[0-9A-Z]{20}", lei.strip().upper()):
                raise DataContractError("GLEIF request requires a 20-character LEI")
            normalized_lei = lei.strip().upper()
            url = f"{GLEIF_BASE}/lei-records/{normalized_lei}"
            headers = {"Accept": "application/vnd.api+json"}
            yield self._request(
                url, {}, headers, dataset="entity_reference", record_id=normalized_lei,
                media_type="application/vnd.api+json",
            )


class SecAdapter(OfficialAdapterBase):
    source_id = "sec"
    supported = ("submissions", "companyfacts", "filing_document")
    terms_url = "https://www.sec.gov/about/webmaster-frequently-asked-questions"
    schema_version = "SEC_DATA_API_JSON_V1"

    def __init__(self, *, contact: str | None, transport: HttpTransport | None = None,
                 registry: SourceRegistry | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        super().__init__(transport=transport, registry=registry, clock=clock)
        self._contact = contact.strip() if isinstance(contact, str) else ""

    def _user_agent(self) -> str:
        # Contact is supplied by the owner/operator, never inferred or invented.
        email_contact = bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[A-Za-z]{2,}", self._contact))
        parsed_url = urllib.parse.urlparse(self._contact)
        url_contact = parsed_url.scheme == "https" and bool(parsed_url.netloc)
        if (not self._contact or any(char in self._contact for char in "\r\n")
                or not (email_contact or url_contact)):
            raise OfficialAdapterError("SEC live access requires an explicitly configured contact")
        return f"INVEST-AI/0.1 ({self._contact})"

    def fetch_envelopes(self, request: ProviderRequest) -> Iterable[OfficialRawEnvelope]:
        self._registry.require(self.source_id)
        if not set(request.datasets).issubset(self.supported):
            raise DataContractError("SEC adapter received an unsupported dataset")
        user_agent = self._user_agent()  # refuse before any transport call
        headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate", "Accept": "application/json"}
        cik_values = request.instrument_ids
        if not cik_values:
            raise DataContractError("SEC requests require one or more CIK identifiers")
        for raw_cik in cik_values:
            for dataset in request.datasets:
                if dataset == "filing_document":
                    parts = raw_cik.split(":", 2)
                    if len(parts) != 3:
                        raise DataContractError(
                            "filing_document identifier must be CIK:accession:document"
                        )
                    doc_cik, accession, document = parts
                    doc_cik_digits = doc_cik.removeprefix("CIK").lstrip("0") or "0"
                    if not doc_cik_digits.isdigit() or len(doc_cik_digits) > 10:
                        raise DataContractError("SEC document CIK is invalid")
                    acc_digits = accession.replace("-", "")
                    if not acc_digits.isdigit() or len(acc_digits) != 18:
                        raise DataContractError("SEC accession number is invalid")
                    if not acc_digits.startswith(doc_cik_digits.zfill(10)):
                        raise DataContractError("SEC accession CIK does not match the requested CIK")
                    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", document) or ".." in document:
                        raise DataContractError("SEC primary document name is invalid")
                    url = f"{SEC_ARCHIVES}/{int(doc_cik_digits)}/{acc_digits}/{document}"
                    params: dict[str, str] = {}
                    record_id = f"{acc_digits}:{document}"
                    yield self._request(
                        url, params, headers, dataset=dataset, record_id=record_id,
                        media_type="application/octet-stream",
                    )
                    continue
                digits = raw_cik.strip().removeprefix("CIK").lstrip("0") or "0"
                if not digits.isdigit() or len(digits) > 10:
                    raise DataContractError("SEC CIK must contain up to 10 digits")
                cik = digits.zfill(10)
                if dataset == "submissions":
                    url = f"{SEC_BASE}/submissions/CIK{cik}.json"
                    params: dict[str, str] = {}
                    record_id = f"submissions:{cik}"
                elif dataset == "companyfacts":
                    url = f"{SEC_BASE}/api/xbrl/companyfacts/CIK{cik}.json"
                    params = {}
                    record_id = f"companyfacts:{cik}"
                yield self._request(
                    url, params, headers, dataset=dataset, record_id=record_id,
                    media_type="application/json" if dataset != "filing_document" else "application/octet-stream",
                )


class EcbAdapter(OfficialAdapterBase):
    source_id = "ecb"
    supported = ("macro_observation",)
    terms_url = "https://www.ecb.europa.eu/stats/ecb_statistics/governance_and_quality_framework/html/usage_policy.en.html"
    schema_version = "ECB_SDMX_2_1_CSV"

    def __init__(self, *, approved_series: Iterable[str] | None = None, transport: HttpTransport | None = None,
                 include_history: bool = False,
                 registry: SourceRegistry | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        super().__init__(transport=transport, registry=registry, clock=clock)
        registry_series = frozenset(self._registry.entry("ecb").approved_series)
        configured_series = registry_series if approved_series is None else frozenset(
            item.strip() for item in approved_series if item.strip()
        )
        self._approved_series = registry_series.intersection(configured_series)
        self._include_history = include_history

    def fetch_envelopes(self, request: ProviderRequest) -> Iterable[OfficialRawEnvelope]:
        self._registry.require(self.source_id, third_party_data=False)
        if not self._approved_series:
            raise LicenseBlocked("ECB ingestion requires an explicit approved-series allowlist")
        if not request.datasets:
            raise DataContractError("ECB request must include a flow and series key")
        headers = {"Accept": "text/csv"}
        for spec in request.datasets:
            # Request form is FLOW_ID:SDMX_SERIES_KEY, e.g. EXR:D.USD.EUR.SP00.A.
            if ":" not in spec:
                raise DataContractError("ECB dataset must be FLOW_ID:SERIES_KEY")
            flow, key = spec.split(":", 1)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", flow) or not re.fullmatch(r"[A-Za-z0-9.+_-]{1,300}", key):
                raise DataContractError("ECB flow or SDMX series key is invalid")
            if spec not in self._approved_series:
                raise LicenseBlocked("ECB series is not on the approved-series allowlist")
            params = {"format": "csvdata"}
            if self._include_history:
                params["includeHistory"] = "true"
            if request.start_at:
                params["startPeriod"] = request.start_at.date().isoformat()
            if request.end_at:
                params["endPeriod"] = request.end_at.date().isoformat()
            url = f"{ECB_BASE}/data/{flow}/{key}"
            yield self._request(
                url, params, headers, dataset="macro_observation",
                record_id=f"{flow}:{key}", media_type="text/csv",
            )


@dataclass(frozen=True, slots=True)
class OfficialEntityReference:
    provenance: DataProvenance
    lei: str
    legal_name: str
    entity_status: str
    registration_status: str | None
    creation_date: str | None
    last_update_date: str | None
    country: str | None
    legal_form: str | None
    entity_category: str | None

    @property
    def observation_id(self) -> str:
        return self.provenance.observation_id


def _provenance(envelope: OfficialRawEnvelope, archived: ArchivedRaw, *,
                dataset: str | None = None, record_id: str | None = None,
                observed_at: datetime | None = None, published_at: datetime | None = None,
                revision: int | None = None, unit: str | None = None) -> DataProvenance:
    raw = envelope.payload
    return DataProvenance(
        provider=raw.provider, source=raw.source, dataset=dataset or raw.dataset,
        instrument_id=None, observed_at=observed_at or raw.observed_at,
        published_at=published_at, received_at=raw.received_at,
        available_at=raw.available_at, revision=revision or raw.revision,
        currency=None, unit=unit, raw_reference=archived.reference,
        raw_sha256=archived.sha256, record_id=record_id or raw.record_id,
    )


def normalize_gleif(envelope: OfficialRawEnvelope, archived: ArchivedRaw) -> OfficialEntityReference:
    if envelope.payload.provider != "gleif" or envelope.payload.dataset != "entity_reference":
        raise DataContractError("GLEIF normalizer received a different source/dataset")
    try:
        root = json.loads(envelope.payload.body.decode("utf-8"))
        attrs = root["data"]["attributes"]
        entity = attrs["entity"]
        legal_name = entity["legalName"]["name"]
        lei = root["data"]["id"]
        if lei != envelope.payload.record_id:
            raise ValueError("LEI does not match request")
        registration = attrs.get("registration", {})
        provenance = _provenance(envelope, archived, record_id=lei)
        return OfficialEntityReference(
            provenance=provenance, lei=lei, legal_name=legal_name,
            entity_status=str(entity.get("status", "UNKNOWN")),
            registration_status=registration.get("status"),
            creation_date=attrs.get("creationDate"),
            last_update_date=attrs.get("lastUpdateDate"),
            country=entity.get("legalAddress", {}).get("country"),
            legal_form=entity.get("legalForm", {}).get("id"),
            entity_category=entity.get("category"),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise DataContractError("GLEIF payload does not match the expected LEI response") from exc


def _sec_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
        # SEC documents acceptance time in EST. Do not reinterpret its clock time
        # as UTC or apply a machine-local timezone.
    raw = value.strip()
    if raw.endswith("Z") or re.search(r"[+-]\d\d:\d\d$", raw):
        return as_utc(raw, "SEC acceptanceDateTime")
    for fmt in ("%Y%m%d%H%M%S.%f", "%Y%m%d%H%M%S"):
        try:
            parsed = datetime.strptime(raw, fmt).replace(tzinfo=SEC_EST)
            return parsed.astimezone(UTC)
        except ValueError:
            continue
    raise DataContractError("SEC acceptance timestamp is invalid")


def normalize_sec_submissions(envelope: OfficialRawEnvelope, archived: ArchivedRaw) -> tuple[Filing, ...]:
    if envelope.payload.provider != "sec" or envelope.payload.dataset != "submissions":
        raise DataContractError("SEC submissions normalizer received a different source/dataset")
    try:
        body = json.loads(envelope.payload.body.decode("utf-8"))
        recent = body["filings"]["recent"]
        result: list[Filing] = []
        for index, accession in enumerate(recent["accessionNumber"]):
            form = recent["form"][index]
            filed_text = recent["filingDate"][index]
            primary = recent.get("primaryDocument", [""] * len(recent["accessionNumber"]))[index]
            accepted = _sec_datetime(recent.get("acceptanceDateTime", [None] * len(recent["accessionNumber"]))[index])
            if not primary or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", primary) or ".." in primary:
                document_url = ""
            else:
                cik = body["cik"]
                doc_cik = int(cik)
                accession_path = accession.replace("-", "")
                document_url = f"{SEC_ARCHIVES}/{doc_cik}/{accession_path}/{primary}"
            filed_day = date.fromisoformat(filed_text)
            event_time = accepted or datetime.combine(filed_day, time.min, tzinfo=UTC)
            provenance = _provenance(
                envelope, archived, dataset="submissions", record_id=accession,
                observed_at=event_time,
            )
            result.append(Filing(
                provenance=provenance, accession=accession, form=form,
                filed_at=event_time, accepted_at=accepted,
                title=f"{body.get('name', 'SEC filer')} {form}",
                document_url=document_url or f"https://www.sec.gov/Archives/edgar/data/{int(body['cik'])}/{accession.replace('-', '')}/",
            ))
        return tuple(result)
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, IndexError, ValueError) as exc:
        raise DataContractError("SEC submissions payload is incomplete or invalid") from exc


def normalize_sec_companyfacts(envelope: OfficialRawEnvelope, archived: ArchivedRaw) -> tuple[FundamentalFact, ...]:
    if envelope.payload.provider != "sec" or envelope.payload.dataset != "companyfacts":
        raise DataContractError("SEC companyfacts normalizer received a different source/dataset")
    try:
        body = json.loads(envelope.payload.body.decode("utf-8"), parse_float=Decimal)
        result: list[FundamentalFact] = []
        for taxonomy, concepts in body["facts"].items():
            for tag, concept in concepts.items():
                for unit, entries in concept.get("units", {}).items():
                    for entry in entries:
                        accession = entry.get("accn")
                        filed = date.fromisoformat(entry["filed"]) if entry.get("filed") else None
                        period_end = date.fromisoformat(entry["end"]) if entry.get("end") else None
                        period_start = date.fromisoformat(entry["start"]) if entry.get("start") else None
                        form_base = str(entry.get("form") or "").removesuffix("/A")
                        record = "|".join(str(v or "") for v in (
                            taxonomy, tag, unit, entry.get("start"), entry.get("end"),
                            entry.get("fy"), entry.get("fp"), entry.get("frame"), form_base,
                        ))
                        provenance = _provenance(
                            envelope, archived, dataset="companyfacts", record_id=record,
                            unit=unit,
                        )
                        result.append(FundamentalFact(
                            provenance=provenance,
                            metric=f"{taxonomy}:{tag}:{unit}",
                            value=entry["val"],
                            period_start=period_start,
                            period_end=period_end or filed,
                            fiscal_period=entry.get("fp"),
                            accession=accession,
                            xbrl_tag=tag,
                        ))
        return tuple(result)
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise DataContractError("SEC companyfacts payload is incomplete or invalid") from exc


def normalize_ecb_csv(envelope: OfficialRawEnvelope, archived: ArchivedRaw) -> tuple[MacroObservation, ...]:
    if envelope.payload.provider != "ecb" or envelope.payload.dataset != "macro_observation":
        raise DataContractError("ECB normalizer received a different source/dataset")
    try:
        reader = csv.DictReader(io.StringIO(envelope.payload.body.decode("utf-8-sig")))
        if not reader.fieldnames or "TIME_PERIOD" not in reader.fieldnames:
            raise ValueError("TIME_PERIOD column missing")
        result: list[MacroObservation] = []
        for row in reader:
            period = (row.get("TIME_PERIOD") or "").strip()
            if not period:
                continue
            raw_value = (row.get("OBS_VALUE") or "").strip()
            value = Decimal(raw_value) if raw_value not in ("", ".") else None
            series = (row.get("KEY") or envelope.payload.record_id).strip()
            status = (row.get("OBS_STATUS") or "").strip()
            record_id = f"{series}|{period}"
            unit = (row.get("UNIT") or row.get("UNIT_MEASURE") or "").strip()
            if not unit and row.get("CURRENCY") and row.get("DENOM"):
                unit = f"{row['CURRENCY'].strip()}/{row['DENOM'].strip()}"
            provenance = _provenance(
                envelope, archived, dataset="macro_observation", record_id=record_id,
                unit=unit or None,
            )
            result.append(MacroObservation(
                provenance=provenance,
                series_id=series,
                period=period,
                frequency=(row.get("FREQ") or "UNKNOWN").strip(),
                value=value,
                missing_reason=f"ECB observation missing; status={status or 'unspecified'}" if value is None else None,
            ))
        return tuple(result)
    except (UnicodeDecodeError, csv.Error, ValueError, ArithmeticError) as exc:
        raise DataContractError("ECB CSV payload is invalid") from exc


Normalizer = Callable[[OfficialRawEnvelope, ArchivedRaw], Sequence[DataObservation | OfficialEntityReference]]


class OfficialRawArchive:
    """Writes body bytes through RawArchive and an immutable request/response manifest."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.raw = RawArchive(self.project_root)

    def archive(self, envelope: OfficialRawEnvelope) -> tuple[ArchivedRaw, str, str]:
        archived = self.raw.archive(envelope.payload)
        manifest = {
            "source": envelope.payload.provider,
            "dataset": envelope.payload.dataset,
            "record_id": envelope.payload.record_id,
            "request_url": envelope.request_url,
            "request_parameters": dict(envelope.request_parameters),
            "request_headers": dict(envelope.request_headers),
            "response_headers": dict(envelope.response_headers),
            "received_at": envelope.payload.received_at.isoformat().replace("+00:00", "Z"),
            "available_at": envelope.payload.available_at.isoformat().replace("+00:00", "Z"),
            "body_sha256": archived.sha256,
            "body_size_bytes": archived.size_bytes,
            "source_schema_version": envelope.source_schema_version,
            "terms_id": envelope.terms_id,
        }
        data = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        relative = Path("data") / "raw_manifests" / envelope.payload.provider / archived.sha256 / f"{digest}.json"
        target = self.project_root / relative
        self._write_immutable(target, data)
        return archived, relative.as_posix(), digest

    def _write_immutable(self, target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.resolve().relative_to(self.project_root)
        except ValueError as exc:
            raise OfficialAdapterError("manifest path escapes project root") from exc
        if target.exists():
            if target.read_bytes() != data:
                raise OfficialAdapterError("immutable manifest path collision")
            return
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, prefix=".tmp-", delete=False) as handle:
                temp_name = handle.name
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            # Do not overwrite another manifest in the unlikely event of a race.
            if target.exists():
                if target.read_bytes() != data:
                    raise OfficialAdapterError("immutable manifest path collision")
                os.unlink(temp_name)
                return
            os.rename(temp_name, target)
        except OSError as exc:
            if temp_name and os.path.exists(temp_name):
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
            raise OfficialAdapterError("immutable manifest write failed") from exc


@dataclass(frozen=True, slots=True)
class OfficialIngestOutcome:
    source: str
    dataset: str
    record_id: str
    raw_reference: str
    raw_sha256: str
    manifest_reference: str
    manifest_sha256: str
    normalized: tuple[AppendResult, ...]


class OfficialDataIngestor:
    """Raw-first ingestor for metadata-rich official-source envelopes."""

    def __init__(self, project_root: str | Path) -> None:
        self.archive = OfficialRawArchive(project_root)
        self.normalized = NormalizedArchive(project_root)

    def ingest(self, adapter: OfficialAdapterBase, request: ProviderRequest,
               normalizers: Mapping[str, Normalizer]) -> list[OfficialIngestOutcome]:
        outcomes: list[OfficialIngestOutcome] = []
        for envelope in adapter.fetch_envelopes(request):
            payload = envelope.payload
            if payload.provider != adapter.metadata.provider:
                raise DataContractError("official envelope source differs from adapter metadata")
            if payload.dataset not in request.datasets and not isinstance(adapter, EcbAdapter):
                raise DataContractError("official adapter returned an unrequested dataset")
            # Per-response gate is repeated immediately before any durable write.
            adapter._registry.require(payload.provider, third_party_data=False)
            archived, manifest_ref, manifest_sha = self.archive.archive(envelope)
            normalized_results: list[AppendResult] = []
            normalizer = normalizers.get(payload.dataset)
            if normalizer:
                observations = normalizer(envelope, archived)
                for observation in observations:
                    p = observation.provenance
                    if (p.provider != payload.provider or p.source != payload.source
                            or p.raw_reference != archived.reference or p.raw_sha256 != archived.sha256
                            or p.received_at != payload.received_at or p.available_at != payload.available_at):
                        raise DataContractError("normalized data is not bound to its immutable raw payload")
                    observation, duplicate = self._prepare_revision(observation)
                    if duplicate is not None:
                        normalized_results.append(duplicate)
                    else:
                        normalized_results.append(self.normalized.append(observation))  # type: ignore[arg-type]
            outcomes.append(OfficialIngestOutcome(
                source=payload.provider, dataset=payload.dataset, record_id=payload.record_id,
                raw_reference=archived.reference, raw_sha256=archived.sha256,
                manifest_reference=manifest_ref, manifest_sha256=manifest_sha,
                normalized=tuple(normalized_results),
            ))
        return outcomes

    def _prepare_revision(self, observation: DataObservation | OfficialEntityReference
                          ) -> tuple[DataObservation | OfficialEntityReference, AppendResult | None]:
        """Deduplicate unchanged records and version changed source snapshots."""
        provenance = observation.provenance
        directory = self.normalized.project_root / "data" / "normalized" / provenance.provider / provenance.dataset
        if not directory.exists():
            return observation, None
        logical_key = provenance.logical_key
        max_revision = 0
        latest_path: Path | None = None
        latest_bytes: bytes | None = None
        latest_content: dict[str, object] | None = None
        for path in directory.glob("*.json"):
            try:
                raw_stored = path.read_bytes()
                stored = json.loads(raw_stored.decode("utf-8"))
                prior = stored["provenance"]
                prior_key = "|".join((
                    prior["provider"], prior["dataset"], prior.get("instrument_id") or "",
                    prior["record_id"],
                ))
                if prior_key != logical_key:
                    continue
                prior_revision = int(prior["revision"])
                if prior_revision >= max_revision:
                    max_revision = prior_revision
                    latest_path = path
                    latest_bytes = raw_stored
                    latest_content = dict(stored)
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                # An unreadable normalized entry must not be overwritten. Archive.append
                # remains the final integrity gate and will fail closed if it conflicts.
                continue
        if not max_revision:
            return observation, None
        incoming = observation_to_dict(observation)
        incoming_content = dict(incoming)
        incoming_content.pop("provenance", None)
        if latest_content is not None:
            latest_content.pop("provenance", None)
            if latest_content == incoming_content and latest_path is not None and latest_bytes is not None:
                reference = latest_path.relative_to(self.normalized.project_root).as_posix()
                digest = hashlib.sha256(latest_bytes).hexdigest()
                return observation, AppendResult(reference, digest, "DUPLICATE")
        revision = max(max_revision + 1, provenance.revision)
        if revision == provenance.revision:
            return observation, None
        return replace(observation, provenance=replace(provenance, revision=revision)), None
