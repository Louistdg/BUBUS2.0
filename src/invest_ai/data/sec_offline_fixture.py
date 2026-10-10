"""Offline-only SEC filing fixture contracts.

This module deliberately contains no transport, filesystem, clock, or financial
execution integration. Callers must supply synthetic request/response values.
It is a testable prototype, not a SEC collector.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from invest_ai.data.models import DataContractError, as_utc


class SecFixtureError(ValueError):
    """A synthetic SEC fixture failed a fail-closed validation rule."""


class PitStatus(StrEnum):
    FIRST_OBSERVED_CONSERVATIVE = "FIRST_OBSERVED_CONSERVATIVE"
    PIT_UNVERIFIED = "PIT_UNVERIFIED"


class ResponseDisposition(StrEnum):
    ACCEPTED = "ACCEPTED"
    RATE_LIMITED = "RATE_LIMITED"
    REDIRECT_REJECTED = "REDIRECT_REJECTED"
    REJECTED = "REJECTED"


class LicenseStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"


_CIK = re.compile(r"^[0-9]{10}$")
_ACCESSION = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_DOC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A"})
_DATASETS = frozenset({"submissions", "companyfacts", "filing_document", "xbrl_instance"})
_MIME = {
    "filing_document": frozenset({"text/html", "application/xhtml+xml", "text/plain", "application/xml"}),
    "xbrl_instance": frozenset({"application/xml", "text/xml"}),
    "submissions": frozenset({"application/json"}),
    "companyfacts": frozenset({"application/json"}),
}

# Canonical capture identity payload, in semantic schema order. The serialized
# object uses sorted JSON keys; capture_id is present as the empty string while
# deriving itself. manifest_sha256 is intentionally excluded from this payload.
CAPTURE_ID_FIELDS = (
    "schema_version", "capture_id", "source_id", "dataset", "cik", "accession",
    "form", "document_name", "filing_date", "accepted_at", "public_available_at",
    "first_observed_available_at", "collected_at", "available_at", "pit_status",
    "source_version", "supersedes_id", "license_id", "license_status", "raw_sha256",
    "raw_size", "response_status", "response_mime", "retry_after_seconds",
    "response_disposition", "untrusted_content", "content_trust",
)


def _canonical(value: object) -> bytes:
    """UTF-8 JSON, Unicode preserved, keys sorted, compact separators, no NaN."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _derive_capture_id(fields: Mapping[str, object]) -> str:
    """Derive an ID from every declared identity field, independent of manifest hash."""
    if any(name not in fields for name in CAPTURE_ID_FIELDS):
        raise SecFixtureError("capture identity fields are incomplete")
    identity = {name: fields[name] for name in CAPTURE_ID_FIELDS}
    identity["capture_id"] = ""
    return "sec-capture-" + hashlib.sha256(_canonical(identity)).hexdigest()


def _timestamp(value: datetime | str, name: str) -> datetime:
    try:
        return as_utc(value, name)
    except DataContractError as exc:
        raise SecFixtureError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class SecFixtureRequest:
    """An allowlisted synthetic request descriptor; it never performs I/O."""

    dataset: str
    cik: str
    form: str | None = None
    accession: str | None = None
    document_name: str | None = None

    def __post_init__(self) -> None:
        if self.dataset not in _DATASETS:
            raise SecFixtureError("dataset is not allowlisted")
        if not isinstance(self.cik, str) or not _CIK.fullmatch(self.cik):
            raise SecFixtureError("CIK must be exactly ten digits")
        if self.form is not None and self.form not in _FORMS:
            raise SecFixtureError("form is not allowlisted")
        if self.dataset in {"filing_document", "xbrl_instance"}:
            if self.form is None or self.accession is None or self.document_name is None:
                raise SecFixtureError("document requests require form, accession and document name")
            if not _ACCESSION.fullmatch(self.accession):
                raise SecFixtureError("accession is malformed")
            if self.accession[:10] != self.cik:
                raise SecFixtureError("accession CIK must match the canonical request CIK")
            if not _DOC_NAME.fullmatch(self.document_name) or ".." in self.document_name:
                raise SecFixtureError("document name is not a safe allowlisted component")
        elif self.accession is not None or self.document_name is not None:
            raise SecFixtureError("accession/document name are forbidden for this dataset")


@dataclass(frozen=True, slots=True)
class SyntheticHttpResponse:
    """In-memory fixture response; body bytes are treated as untrusted source data."""

    status: int
    headers: Mapping[str, str]
    body: bytes
    completed_at: datetime

    def __post_init__(self) -> None:
        if isinstance(self.status, bool) or not isinstance(self.status, int) or not 100 <= self.status <= 599:
            raise SecFixtureError("invalid HTTP status")
        if not isinstance(self.body, bytes):
            raise SecFixtureError("response body must be exact bytes")
        if not isinstance(self.headers, Mapping):
            raise SecFixtureError("response headers must be a mapping")
        object.__setattr__(self, "headers", MappingProxyType({str(k).lower(): str(v) for k, v in self.headers.items()}))
        object.__setattr__(self, "completed_at", _timestamp(self.completed_at, "completed_at"))


@dataclass(frozen=True, slots=True)
class SecDocumentEvidence:
    """Immutable PIT/provenance sidecar compatible with DATA and Research concepts."""

    schema_version: str
    capture_id: str
    source_id: str
    dataset: str
    cik: str
    accession: str
    form: str
    document_name: str
    filing_date: date
    accepted_at: datetime | None
    public_available_at: datetime | None
    first_observed_available_at: datetime | None
    collected_at: datetime
    available_at: datetime | None
    pit_status: PitStatus
    source_version: str
    supersedes_id: str | None
    license_id: str
    license_status: LicenseStatus
    raw_sha256: str | None
    raw_size: int | None
    response_status: int | None
    response_mime: str | None
    retry_after_seconds: int | None
    response_disposition: ResponseDisposition
    untrusted_content: bool
    manifest_sha256: str

    def manifest(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "capture_id": self.capture_id,
            "source_id": self.source_id,
            "dataset": self.dataset,
            "cik": self.cik,
            "accession": self.accession,
            "form": self.form,
            "document_name": self.document_name,
            "filing_date": self.filing_date.isoformat(),
            "accepted_at": _iso(self.accepted_at),
            "public_available_at": _iso(self.public_available_at),
            "first_observed_available_at": _iso(self.first_observed_available_at),
            "collected_at": _iso(self.collected_at),
            "available_at": _iso(self.available_at),
            "pit_status": self.pit_status.value,
            "source_version": self.source_version,
            "supersedes_id": self.supersedes_id,
            "license_id": self.license_id,
            "license_status": self.license_status.value,
            "raw_sha256": self.raw_sha256,
            "raw_size": self.raw_size,
            "response_status": self.response_status,
            "response_mime": self.response_mime,
            "retry_after_seconds": self.retry_after_seconds,
            "response_disposition": self.response_disposition.value,
            "untrusted_content": self.untrusted_content,
            "content_trust": ("UNTRUSTED_SOURCE_DATA" if self.untrusted_content is True
                              else "TRUST_MARKER_INVALID"),
            "manifest_sha256": self.manifest_sha256,
        }

    def verify(self) -> bool:
        try:
            if self.untrusted_content is not True:
                return False
            body = self.manifest()
            if self.capture_id != _derive_capture_id(body):
                return False
            expected = body.pop("manifest_sha256")
            return hashlib.sha256(_canonical(body)).hexdigest() == expected
        except (SecFixtureError, TypeError, ValueError):
            return False


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def _build_evidence(*, request: SecFixtureRequest, filing_date: date, accepted_at: datetime | None,
                    response: SyntheticHttpResponse | None, license_id: str,
                    license_status: LicenseStatus = LicenseStatus.UNKNOWN,
                    supersedes_id: str | None = None) -> SecDocumentEvidence:
    if not isinstance(filing_date, date) or isinstance(filing_date, datetime):
        raise SecFixtureError("filing_date must be a date without invented time")
    accepted = _timestamp(accepted_at, "accepted_at") if accepted_at else None
    if not license_id or not license_id.strip():
        raise SecFixtureError("license_id must be explicit; unknown rights fail closed")
    collected = response.completed_at if response else None
    if collected and filing_date > collected.date():
        raise SecFixtureError("filing_date cannot be after the observed collection date")
    if accepted and collected and accepted > collected:
        raise SecFixtureError("accepted_at cannot be later than completed collection")
    disposition = ResponseDisposition.REJECTED
    mime: str | None = None
    retry_after: int | None = None
    raw_hash: str | None = None
    raw_size: int | None = None
    pit_status = PitStatus.PIT_UNVERIFIED
    first_observed = available = None
    status = response.status if response else None
    if response:
        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower() or None
        if response.status == 429:
            raw_retry_after = response.headers.get("retry-after", "").strip()
            if raw_retry_after.isascii() and raw_retry_after.isdigit():
                retry_after = int(raw_retry_after)
        if 300 <= response.status < 400:
            disposition = ResponseDisposition.REDIRECT_REJECTED
        elif response.status == 429:
            disposition = ResponseDisposition.RATE_LIMITED
        elif response.status != 200:
            disposition = ResponseDisposition.REJECTED
        elif not response.body:
            disposition = ResponseDisposition.REJECTED
        elif mime not in _MIME[request.dataset]:
            disposition = ResponseDisposition.REJECTED
        elif "content-length" in response.headers:
            try:
                if int(response.headers["content-length"]) != len(response.body):
                    disposition = ResponseDisposition.REJECTED
                else:
                    disposition = ResponseDisposition.ACCEPTED
            except ValueError:
                disposition = ResponseDisposition.REJECTED
        else:
            disposition = ResponseDisposition.ACCEPTED
        if disposition == ResponseDisposition.ACCEPTED:
            raw_hash = hashlib.sha256(response.body).hexdigest()
            raw_size = len(response.body)
            first_observed = collected
            available = collected
            pit_status = PitStatus.FIRST_OBSERVED_CONSERVATIVE
    fields: dict[str, object] = {
        "schema_version": "sec-fixture-evidence-v1", "capture_id": "", "source_id": "SEC_EDGAR_FIXTURE",
        "dataset": request.dataset, "cik": request.cik, "accession": request.accession or "",
        "form": request.form or "", "document_name": request.document_name or "",
        "filing_date": filing_date.isoformat(), "accepted_at": _iso(accepted),
        "public_available_at": None, "first_observed_available_at": _iso(first_observed),
        "collected_at": _iso(collected), "available_at": _iso(available), "pit_status": pit_status.value,
        "source_version": f"{request.accession or request.dataset}:{raw_hash or 'unverified'}",
        "supersedes_id": supersedes_id, "license_id": license_id,
        "license_status": license_status.value,
        "raw_sha256": raw_hash, "raw_size": raw_size, "response_status": status,
        "response_mime": mime, "response_disposition": disposition.value,
        "retry_after_seconds": retry_after,
        "untrusted_content": True,
        "content_trust": "UNTRUSTED_SOURCE_DATA",
    }
    capture_id = _derive_capture_id(fields)
    fields["capture_id"] = capture_id
    manifest_sha = hashlib.sha256(_canonical(fields)).hexdigest()
    return SecDocumentEvidence(
        schema_version="sec-fixture-evidence-v1", capture_id=capture_id, source_id="SEC_EDGAR_FIXTURE",
        dataset=request.dataset, cik=request.cik, accession=request.accession or "", form=request.form or "",
        document_name=request.document_name or "", filing_date=filing_date, accepted_at=accepted,
        public_available_at=None, first_observed_available_at=first_observed, collected_at=collected,
        available_at=available, pit_status=pit_status, source_version=str(fields["source_version"]),
        supersedes_id=supersedes_id, license_id=license_id, raw_sha256=raw_hash, raw_size=raw_size,
        license_status=license_status,
        response_status=status, response_mime=mime, response_disposition=disposition,
        retry_after_seconds=retry_after,
        untrusted_content=True, manifest_sha256=manifest_sha,
    )


def process_fixture(*, request: SecFixtureRequest, filing_date: date, accepted_at: datetime | None,
                    response: SyntheticHttpResponse | None, license_id: str,
                    license_status: LicenseStatus = LicenseStatus.UNKNOWN,
                    supersedes_id: str | None = None, as_of: datetime | None = None) -> SecDocumentEvidence:
    """Validate an in-memory synthetic response and emit a deterministic evidence sidecar.

    ``public_available_at`` is always unknown. Strict cutoff eligibility relies only
    on the first complete locally observed response and never on filing/acceptance.
    """
    evidence = _build_evidence(request=request, filing_date=filing_date, accepted_at=accepted_at,
                               response=response, license_id=license_id, license_status=license_status,
                               supersedes_id=supersedes_id)
    if as_of is not None:
        cutoff = _timestamp(as_of, "as_of")
        if evidence.available_at is None or evidence.available_at > cutoff:
            # Preserve evidence status; consumers must check cutoff eligibility explicitly.
            return evidence
    return evidence


def eligible_as_of(evidence: SecDocumentEvidence, as_of: datetime | str) -> bool:
    cutoff = _timestamp(as_of, "as_of")
    return (evidence.untrusted_content is True and evidence.verify()
            and evidence.pit_status == PitStatus.FIRST_OBSERVED_CONSERVATIVE
            and evidence.available_at is not None and evidence.available_at <= cutoff
            and evidence.raw_sha256 is not None and evidence.license_id.strip() != ""
            and evidence.license_status == LicenseStatus.ALLOWED)


class FixtureEvidenceLedger:
    """Append-only in-memory version ledger for synthetic duplicate/correction cases."""

    def __init__(self) -> None:
        self._by_capture: dict[str, SecDocumentEvidence] = {}
        self._latest_by_accession: dict[tuple[str, str], str] = {}

    @staticmethod
    def _identity(evidence: SecDocumentEvidence) -> tuple[object, ...]:
        """All identity and evidence metadata that must agree for deduplication."""
        return (
            evidence.schema_version, evidence.source_id, evidence.dataset, evidence.cik, evidence.accession,
            evidence.form, evidence.document_name, evidence.raw_sha256, evidence.raw_size,
            evidence.source_version, evidence.supersedes_id, evidence.license_id,
            evidence.license_status, evidence.filing_date, evidence.accepted_at,
            evidence.public_available_at, evidence.first_observed_available_at,
            evidence.collected_at, evidence.available_at, evidence.pit_status,
            evidence.response_status, evidence.response_mime, evidence.retry_after_seconds,
            evidence.response_disposition, evidence.untrusted_content,
        )

    def append(self, evidence: SecDocumentEvidence) -> SecDocumentEvidence:
        if not evidence.verify():
            raise SecFixtureError("manifest integrity check failed")
        if evidence.response_disposition != ResponseDisposition.ACCEPTED:
            raise SecFixtureError("rejected response cannot enter the evidence ledger")
        slot = ((evidence.cik, evidence.accession) if evidence.accession else
                (evidence.dataset, evidence.cik))
        previous_id = self._latest_by_accession.get(slot)
        if previous_id:
            previous = self._by_capture[previous_id]
            if self._identity(previous) == self._identity(evidence):
                return previous
            raise SecFixtureError("identity or provenance conflict for existing accession; rejected for quarantine")
        if evidence.supersedes_id is not None:
            if evidence.supersedes_id not in self._by_capture:
                raise SecFixtureError("supersedes_id must reference an existing immutable version")
            prior = self._by_capture[evidence.supersedes_id]
            if prior.cik != evidence.cik:
                raise SecFixtureError("correction cannot supersede another CIK")
        self._by_capture[evidence.capture_id] = evidence
        self._latest_by_accession[slot] = evidence.capture_id
        return evidence

    def get(self, capture_id: str) -> SecDocumentEvidence:
        try:
            return self._by_capture[capture_id]
        except KeyError as exc:
            raise SecFixtureError("unknown capture id") from exc

    def __len__(self) -> int:
        return len(self._by_capture)


@dataclass(frozen=True, slots=True)
class QuotaDecision:
    allowed: bool
    next_allowed_at: datetime


class SimulatedSharedQuota:
    """Deterministic process-local test double. It is not an interprocess limiter."""

    def __init__(self, interval: timedelta = timedelta(seconds=1)) -> None:
        if interval <= timedelta(0):
            raise SecFixtureError("quota interval must be positive")
        self._interval = interval
        self._next: datetime | None = None

    def request(self, at: datetime | str) -> QuotaDecision:
        now = _timestamp(at, "quota time")
        if self._next is not None and now < self._next:
            return QuotaDecision(False, self._next)
        self._next = now + self._interval
        return QuotaDecision(True, now)


def verify_response_fixture(response: SyntheticHttpResponse, dataset: str) -> ResponseDisposition:
    """Transport-independent response gate; redirects are rejected without following Location."""
    if dataset not in _DATASETS:
        raise SecFixtureError("dataset is not allowlisted")
    if 300 <= response.status < 400:
        return ResponseDisposition.REDIRECT_REJECTED
    if response.status == 429:
        return ResponseDisposition.RATE_LIMITED
    if response.status != 200 or not response.body:
        return ResponseDisposition.REJECTED
    mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if mime not in _MIME[dataset]:
        return ResponseDisposition.REJECTED
    length = response.headers.get("content-length")
    if length is not None:
        try:
            if int(length) != len(response.body):
                return ResponseDisposition.REJECTED
        except ValueError:
            return ResponseDisposition.REJECTED
    return ResponseDisposition.ACCEPTED
