"""Offline, deterministic document and evidence identities for Research.

This module intentionally has no filesystem, network, database, broker, or
execution integration. SHA-256 establishes byte consistency only; it does not
authenticate a source, establish data rights, or prove a document's meaning.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Mapping, Sequence


SCHEMA_VERSION = "research-evidence-v1"
MANIFEST_VERSION = "research-snapshot-v1"
SELECTION_POLICY_VERSION = "pit-conservative-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ResearchEvidenceError(ValueError):
    """Research evidence input is malformed or cannot be verified."""


class LicenseStatus(StrEnum):
    ALLOWED = "ALLOWED"
    UNKNOWN = "UNKNOWN"
    DENIED = "DENIED"


class SourceTrust(StrEnum):
    VERIFIED_OBSERVATION = "VERIFIED_OBSERVATION"
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"
    UNVERIFIED = "UNVERIFIED"


class AvailabilityBasis(StrEnum):
    OBSERVED = "OBSERVED"
    SYNTHETIC_CLOCK = "SYNTHETIC_CLOCK"
    UNKNOWN = "UNKNOWN"


class CitationCheck(StrEnum):
    PASS_TECHNICAL = "PASS_TECHNICAL"
    REJECT = "REJECT"


def _utc(value: datetime | None, field_name: str, *, required: bool = False) -> datetime | None:
    if value is None:
        if required:
            raise ResearchEvidenceError(f"{field_name} is required")
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ResearchEvidenceError(f"{field_name} must include a timezone")
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z") if value else None


def _normalise(value: object) -> object:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list | tuple):
        return [_normalise(item) for item in value]
    if isinstance(value, Mapping):
        output: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ResearchEvidenceError("canonical object keys must be strings")
            normal_key = unicodedata.normalize("NFC", key)
            if normal_key in output:
                raise ResearchEvidenceError("canonical object has duplicate keys after Unicode normalization")
            output[normal_key] = _normalise(item)
        return output
    if value is None or isinstance(value, (bool, int)):
        return value
    raise ResearchEvidenceError(f"unsupported canonical value type: {type(value).__name__}")


def canonical_json(value: object) -> bytes:
    """Versioned UTF-8 JSON encoding: NFC strings, sorted keys, no floats/NaN."""
    try:
        return json.dumps(_normalise(value), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ResearchEvidenceError("value cannot be canonically serialized") from exc


def _stable_id(prefix: str, identity: Mapping[str, object]) -> str:
    return f"{prefix}-{hashlib.sha256(canonical_json(identity)).hexdigest()}"


def _text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchEvidenceError(f"{field_name} is required")
    return unicodedata.normalize("NFC", value.strip())


@dataclass(frozen=True, slots=True)
class ResearchDocumentVersion:
    """A caller-supplied immutable document version; no acquisition is performed."""

    source_id: str
    source_native_id: str
    source_version: str
    issuer_id: str
    document_type: str
    original_filename: str
    mime_type: str
    original_bytes: bytes = field(repr=False)
    published_at: datetime | None = None
    available_at: datetime | None = None
    availability_basis: AvailabilityBasis = AvailabilityBasis.UNKNOWN
    license_decision_id: str = ""
    license_status: LicenseStatus = LicenseStatus.UNKNOWN
    source_trust: SourceTrust = SourceTrust.UNVERIFIED
    schema_version: str = SCHEMA_VERSION
    document_id: str = field(init=False)
    document_version_id: str = field(init=False)
    raw_sha256: str = field(init=False)
    byte_length: int = field(init=False)

    def __post_init__(self) -> None:
        for name in ("source_id", "source_native_id", "source_version", "issuer_id", "document_type",
                     "mime_type", "license_decision_id", "schema_version"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.schema_version != SCHEMA_VERSION:
            raise ResearchEvidenceError("schema_version is not supported by this implementation")
        if not isinstance(self.original_filename, str) or not _SAFE_COMPONENT.fullmatch(self.original_filename):
            raise ResearchEvidenceError("original_filename must be a safe filename component")
        if not isinstance(self.original_bytes, bytes) or not self.original_bytes:
            raise ResearchEvidenceError("original_bytes must contain the exact non-empty source bytes")
        if not isinstance(self.license_status, LicenseStatus):
            raise ResearchEvidenceError("license_status must use the canonical enum")
        if not isinstance(self.source_trust, SourceTrust):
            raise ResearchEvidenceError("source_trust must use the canonical enum")
        if not isinstance(self.availability_basis, AvailabilityBasis):
            raise ResearchEvidenceError("availability_basis must use the canonical enum")
        if self.source_trust is SourceTrust.VERIFIED_OBSERVATION:
            raise ResearchEvidenceError("authoritative acquisition receipts are not implemented; observations cannot be self-certified")
        if (self.license_status is LicenseStatus.ALLOWED
                and (self.source_trust is not SourceTrust.SYNTHETIC_FIXTURE
                     or self.license_decision_id != "LICENSE-SYNTHETIC-INTERNAL-ONLY")):
            raise ResearchEvidenceError("only internal synthetic fixtures have an implemented license decision")
        published = _utc(self.published_at, "published_at")
        available = _utc(self.available_at, "available_at")
        if self.availability_basis is AvailabilityBasis.UNKNOWN and available is not None:
            raise ResearchEvidenceError("unknown availability basis cannot carry available_at")
        if self.availability_basis is not AvailabilityBasis.UNKNOWN and available is None:
            raise ResearchEvidenceError("a known availability basis requires available_at")
        if self.availability_basis is AvailabilityBasis.OBSERVED:
            raise ResearchEvidenceError("observed availability requires authoritative acquisition receipts, not implemented")
        if self.availability_basis is AvailabilityBasis.SYNTHETIC_CLOCK and self.source_trust is not SourceTrust.SYNTHETIC_FIXTURE:
            raise ResearchEvidenceError("synthetic availability requires synthetic-fixture trust status")
        if published is not None and available is not None and published > available:
            raise ResearchEvidenceError("published_at cannot be later than available_at")
        object.__setattr__(self, "published_at", published)
        object.__setattr__(self, "available_at", available)
        raw_hash = hashlib.sha256(self.original_bytes).hexdigest()
        document_id = _stable_id("rd-doc-v1", {"schema_version": self.schema_version,
                                                "source_id": self.source_id,
                                                "source_native_id": self.source_native_id})
        version_id = _stable_id("rd-docver-v1", {"document_id": document_id,
                                                  "source_version": self.source_version,
                                                  "raw_sha256": raw_hash,
                                                  "schema_version": self.schema_version})
        object.__setattr__(self, "raw_sha256", raw_hash)
        object.__setattr__(self, "byte_length", len(self.original_bytes))
        object.__setattr__(self, "document_id", document_id)
        object.__setattr__(self, "document_version_id", version_id)

    def verify_original(self) -> bool:
        try:
            if not isinstance(self.original_bytes, bytes):
                return False
            raw_hash = hashlib.sha256(self.original_bytes).hexdigest()
            expected_document_id = _stable_id("rd-doc-v1", {
                "schema_version": self.schema_version,
                "source_id": self.source_id,
                "source_native_id": self.source_native_id,
            })
            expected_version_id = _stable_id("rd-docver-v1", {
                "document_id": expected_document_id,
                "source_version": self.source_version,
                "raw_sha256": raw_hash,
                "schema_version": self.schema_version,
            })
            return (len(self.original_bytes) == self.byte_length
                    and raw_hash == self.raw_sha256
                    and expected_document_id == self.document_id
                    and expected_version_id == self.document_version_id)
        except (AttributeError, ResearchEvidenceError, TypeError, ValueError):
            return False

    def identity_manifest(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "document_id": self.document_id,
            "document_version_id": self.document_version_id,
            "source_id": self.source_id,
            "source_native_id": self.source_native_id,
            "source_version": self.source_version,
            "issuer_id": self.issuer_id,
            "document_type": self.document_type,
            "original_filename": self.original_filename,
            "mime_type": self.mime_type,
            "raw_sha256": self.raw_sha256,
            "byte_length": self.byte_length,
            "published_at": _iso(self.published_at),
            "available_at": _iso(self.available_at),
            "availability_basis": self.availability_basis.value,
            "license_decision_id": self.license_decision_id,
            "license_status": self.license_status.value,
            "source_trust": self.source_trust.value,
        }


@dataclass(frozen=True, slots=True)
class ExcludedDocument:
    document_version_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ResearchSnapshot:
    snapshot_id: str
    manifest_sha256: str
    manifest_json: str
    as_of: datetime
    document_version_ids: tuple[str, ...]
    excluded: tuple[ExcludedDocument, ...]
    source_basis: str
    selection_policy_version: str = SELECTION_POLICY_VERSION
    manifest_version: str = MANIFEST_VERSION


def _eligibility(document: ResearchDocumentVersion, as_of: datetime) -> str | None:
    if not document.verify_original():
        return "ORIGINAL_HASH_MISMATCH"
    if document.license_status is LicenseStatus.DENIED:
        return "LICENSE_NOT_AUTHORIZED"
    if document.license_status is LicenseStatus.UNKNOWN:
        return "LICENSE_NOT_AUTHORIZED"
    if document.source_trust is SourceTrust.UNVERIFIED:
        return "SOURCE_ID_UNVERIFIED"
    if document.published_at is None:
        return "PUBLISHED_AT_UNKNOWN"
    if document.available_at is None or document.availability_basis is AvailabilityBasis.UNKNOWN:
        return "AS_OF_NOT_PROVABLE"
    if document.published_at > as_of or document.available_at > as_of:
        return "STALE_OR_LATE_DATA"
    return None


def build_snapshot(documents: Sequence[ResearchDocumentVersion], *, as_of: datetime) -> ResearchSnapshot:
    """Build a deterministic point-in-time manifest and explicit exclusion list."""
    cutoff = _utc(as_of, "as_of", required=True)
    if not documents:
        raise ResearchEvidenceError("at least one document is required to build a snapshot")
    if any(not isinstance(item, ResearchDocumentVersion) for item in documents):
        raise ResearchEvidenceError("documents must use ResearchDocumentVersion")
    seen: set[str] = set()
    for document in documents:
        if document.document_version_id in seen:
            raise ResearchEvidenceError("duplicate document version in snapshot input")
        seen.add(document.document_version_id)
    eligible: list[ResearchDocumentVersion] = []
    excluded: list[ExcludedDocument] = []
    for document in documents:
        reason = _eligibility(document, cutoff)
        if reason is None:
            eligible.append(document)
        else:
            excluded.append(ExcludedDocument(document.document_version_id, reason))
    eligible.sort(key=lambda item: item.document_version_id)
    excluded.sort(key=lambda item: (item.document_version_id, item.reason))
    trust_kinds = {item.source_trust for item in eligible}
    basis = ("NO_ELIGIBLE_DOCUMENTS" if not trust_kinds else
             "SYNTHETIC_ONLY" if trust_kinds == {SourceTrust.SYNTHETIC_FIXTURE} else
             "MIXED" if SourceTrust.SYNTHETIC_FIXTURE in trust_kinds else "OBSERVED")
    manifest: dict[str, object] = {
        "manifest_version": MANIFEST_VERSION,
        "schema_version": SCHEMA_VERSION,
        "selection_policy_version": SELECTION_POLICY_VERSION,
        "as_of": _iso(cutoff),
        "source_basis": basis,
        "document_version_ids": [item.document_version_id for item in eligible],
        "documents": [item.identity_manifest() for item in eligible],
        "excluded": [{"document_version_id": item.document_version_id, "reason": item.reason}
                     for item in excluded],
    }
    encoded = canonical_json(manifest)
    digest = hashlib.sha256(encoded).hexdigest()
    snapshot_id = f"rd-snapshot-v1-{digest}"
    return ResearchSnapshot(snapshot_id, digest, encoded.decode("utf-8"), cutoff,
                            tuple(item.document_version_id for item in eligible),
                            tuple(excluded), basis)


def verify_snapshot(snapshot: ResearchSnapshot) -> bool:
    if not isinstance(snapshot, ResearchSnapshot):
        return False
    try:
        manifest = json.loads(snapshot.manifest_json)
        if not isinstance(manifest, dict):
            return False
        as_of = _utc(snapshot.as_of, "as_of", required=True)
        document_ids = manifest.get("document_version_ids")
        documents = manifest.get("documents")
        excluded_rows = manifest.get("excluded")
        if (not isinstance(document_ids, list) or not isinstance(documents, list)
                or not isinstance(excluded_rows, list)
                or any(not isinstance(item, dict) for item in documents)
                or any(not isinstance(item, dict)
                       or set(item) != {"document_version_id", "reason"}
                       or not isinstance(item["document_version_id"], str)
                       or not isinstance(item["reason"], str)
                       for item in excluded_rows)):
            return False
        if [item.get("document_version_id") for item in documents] != document_ids:
            return False
        trust_values = {item.get("source_trust") for item in documents}
        expected_basis = ("NO_ELIGIBLE_DOCUMENTS" if not trust_values else
                          "SYNTHETIC_ONLY" if trust_values == {SourceTrust.SYNTHETIC_FIXTURE.value} else
                          "MIXED" if SourceTrust.SYNTHETIC_FIXTURE.value in trust_values else "OBSERVED")
        encoded = canonical_json(manifest)
        digest = hashlib.sha256(encoded).hexdigest()
        return (digest == snapshot.manifest_sha256
                and snapshot.manifest_json == encoded.decode("utf-8")
                and snapshot.snapshot_id == f"rd-snapshot-v1-{digest}"
                and manifest.get("manifest_version") == snapshot.manifest_version
                and manifest.get("selection_policy_version") == snapshot.selection_policy_version
                and manifest.get("as_of") == _iso(as_of)
                and manifest.get("source_basis") == snapshot.source_basis
                and manifest.get("source_basis") == expected_basis
                and tuple(manifest.get("document_version_ids", ())) == snapshot.document_version_ids
                and tuple(ExcludedDocument(row["document_version_id"], row["reason"])
                          for row in excluded_rows) == snapshot.excluded)
    except (AttributeError, KeyError, ResearchEvidenceError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _document_matches_snapshot(snapshot: ResearchSnapshot, document: ResearchDocumentVersion) -> bool:
    try:
        manifest = json.loads(snapshot.manifest_json)
        rows = manifest.get("documents", [])
        return document.identity_manifest() in rows
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return False


@dataclass(frozen=True, slots=True)
class ParserRun:
    """Caller-supplied deterministic parser output, still untrusted as a claim."""

    parser_run_id: str
    document_version_id: str
    parser_version: str
    normalized_text: str = field(repr=False)
    normalized_sha256: str
    trust_label: str = "UNTRUSTED_PARSER_OUTPUT"


def create_parser_run(document: ResearchDocumentVersion, *, parser_version: str,
                      normalized_text: str) -> ParserRun:
    if not isinstance(document, ResearchDocumentVersion) or not document.verify_original():
        raise ResearchEvidenceError("document original failed integrity verification")
    parser = _text(parser_version, "parser_version")
    if not isinstance(normalized_text, str) or not normalized_text:
        raise ResearchEvidenceError("normalized_text must be non-empty text")
    text = unicodedata.normalize("NFC", normalized_text)
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    identity = {"document_version_id": document.document_version_id,
                "parser_version": parser, "normalized_sha256": text_hash,
                "schema_version": SCHEMA_VERSION}
    return ParserRun(_stable_id("rd-parser-v1", identity), document.document_version_id,
                     parser, text, text_hash)


def verify_parser_run(document: ResearchDocumentVersion, parser_run: ParserRun) -> bool:
    if not isinstance(document, ResearchDocumentVersion) or not isinstance(parser_run, ParserRun):
        return False
    if not document.verify_original() or parser_run.document_version_id != document.document_version_id:
        return False
    if parser_run.trust_label != "UNTRUSTED_PARSER_OUTPUT":
        return False
    try:
        if (not isinstance(parser_run.normalized_text, str)
                or not isinstance(parser_run.parser_version, str)
                or not isinstance(parser_run.normalized_sha256, str)
                or not isinstance(parser_run.parser_run_id, str)):
            return False
        text = unicodedata.normalize("NFC", parser_run.normalized_text)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest != parser_run.normalized_sha256:
            return False
        identity = {"document_version_id": document.document_version_id,
                    "parser_version": parser_run.parser_version,
                    "normalized_sha256": digest, "schema_version": SCHEMA_VERSION}
        return parser_run.parser_run_id == _stable_id("rd-parser-v1", identity)
    except (AttributeError, ResearchEvidenceError, TypeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    evidence_id: str
    snapshot_id: str
    document_version_id: str
    parser_run_id: str
    parser_version: str
    normalized_sha256: str
    locator_start: int
    locator_end: int
    quoted_text: str = field(repr=False)
    quoted_text_sha256: str


def create_evidence_ref(snapshot: ResearchSnapshot, document: ResearchDocumentVersion, *,
                        parser_run: ParserRun,
                        start: int, end: int) -> EvidenceRef:
    if not verify_snapshot(snapshot):
        raise ResearchEvidenceError("snapshot integrity check failed")
    if (document.document_version_id not in snapshot.document_version_ids
            or not _document_matches_snapshot(snapshot, document)
            or _eligibility(document, snapshot.as_of) is not None):
        raise ResearchEvidenceError("document version is not eligible in this snapshot")
    if not verify_parser_run(document, parser_run):
        raise ResearchEvidenceError("parser run does not verify against this document version")
    parser = parser_run.parser_version
    text = parser_run.normalized_text
    if (isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int)
            or not isinstance(end, int) or start < 0 or end <= start or end > len(text)):
        raise ResearchEvidenceError("evidence locator is outside the normalized text")
    quote = text[start:end]
    if not quote.strip():
        raise ResearchEvidenceError("evidence quote cannot be empty")
    normalized_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    quote_hash = hashlib.sha256(quote.encode("utf-8")).hexdigest()
    identity = {"snapshot_id": snapshot.snapshot_id,
                "document_version_id": document.document_version_id,
                "parser_run_id": parser_run.parser_run_id,
                "parser_version": parser, "normalized_sha256": normalized_hash,
                "locator_start": start, "locator_end": end,
                "quoted_text_sha256": quote_hash}
    return EvidenceRef(_stable_id("rd-ev-v1", identity), snapshot.snapshot_id,
                       document.document_version_id, parser_run.parser_run_id, parser, normalized_hash,
                       start, end, quote, quote_hash)


def verify_evidence_ref(snapshot: ResearchSnapshot, document: ResearchDocumentVersion,
                        parser_run: ParserRun, evidence: EvidenceRef) -> CitationCheck:
    """Verify technical identity and locator only; semantic relevance stays human-reviewed."""
    if not isinstance(evidence, EvidenceRef):
        return CitationCheck.REJECT
    if (not verify_snapshot(snapshot) or not document.verify_original()
            or not verify_parser_run(document, parser_run)):
        return CitationCheck.REJECT
    if evidence.snapshot_id != snapshot.snapshot_id:
        return CitationCheck.REJECT
    if evidence.document_version_id not in snapshot.document_version_ids:
        return CitationCheck.REJECT
    if (not _document_matches_snapshot(snapshot, document)
            or _eligibility(document, snapshot.as_of) is not None):
        return CitationCheck.REJECT
    if evidence.parser_run_id != parser_run.parser_run_id or evidence.parser_version != parser_run.parser_version:
        return CitationCheck.REJECT
    text = parser_run.normalized_text
    if (not isinstance(evidence.normalized_sha256, str)
            or not isinstance(evidence.quoted_text_sha256, str)
            or not isinstance(evidence.quoted_text, str)
            or not isinstance(evidence.parser_run_id, str)
            or not isinstance(evidence.parser_version, str)
            or not isinstance(evidence.evidence_id, str)
            or not _SHA256.fullmatch(evidence.normalized_sha256)
            or not _SHA256.fullmatch(evidence.quoted_text_sha256)):
        return CitationCheck.REJECT
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != evidence.normalized_sha256:
        return CitationCheck.REJECT
    start, end = evidence.locator_start, evidence.locator_end
    if (isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int)
            or not isinstance(end, int) or start < 0 or end <= start or end > len(text)):
        return CitationCheck.REJECT
    quote = text[start:end]
    if quote != evidence.quoted_text or hashlib.sha256(quote.encode("utf-8")).hexdigest() != evidence.quoted_text_sha256:
        return CitationCheck.REJECT
    identity = {"snapshot_id": evidence.snapshot_id,
                "document_version_id": evidence.document_version_id,
                "parser_run_id": evidence.parser_run_id,
                "parser_version": evidence.parser_version,
                "normalized_sha256": evidence.normalized_sha256,
                "locator_start": start, "locator_end": end,
                "quoted_text_sha256": evidence.quoted_text_sha256}
    return CitationCheck.PASS_TECHNICAL if evidence.evidence_id == _stable_id("rd-ev-v1", identity) else CitationCheck.REJECT
