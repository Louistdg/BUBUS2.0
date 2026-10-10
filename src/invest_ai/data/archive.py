"""Append-only, content-addressed storage for raw and normalized observations."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from invest_ai.data.models import DataContractError, DataObservation, canonical_json_bytes
from invest_ai.data.providers import ArchivedRaw, RawPayload


class ArchiveError(RuntimeError):
    """An archive write or integrity check failed."""


@dataclass(frozen=True, slots=True)
class AppendResult:
    reference: str
    sha256: str
    status: str


def _component(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", value):
        raise DataContractError("provider/dataset path component is unsafe")
    if value in {".", ".."}:
        raise DataContractError("provider/dataset path component is unsafe")
    return value.lower()


def _safe_root(project_root: str | Path) -> Path:
    root = Path(project_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise ArchiveError("project root is not a directory")
    return root


def _atomic_write(target: Path, data: bytes, root: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = target.parent.resolve()
    try:
        resolved_parent.relative_to(root)
    except ValueError as exc:
        raise ArchiveError("archive path escapes project root") from exc
    resolved_target = resolved_parent / target.name
    if resolved_target.exists():
        if resolved_target.read_bytes() != data:
            raise ArchiveError("content-addressed path exists with different bytes")
        return
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=resolved_parent, prefix=".tmp-", delete=False) as handle:
            temp_name = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, resolved_target)
    except OSError as exc:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        raise ArchiveError(f"atomic archive write failed: {exc}") from exc


class RawArchive:
    def __init__(self, project_root: str | Path) -> None:
        self.project_root = _safe_root(project_root)
        self.root = self.project_root / "data" / "raw"

    def archive(self, payload: RawPayload) -> ArchivedRaw:
        provider = _component(payload.provider)
        dataset = _component(payload.dataset)
        digest = hashlib.sha256(payload.body).hexdigest()
        stamp = payload.received_at.strftime("%Y/%m/%d")
        extension = ".json" if "json" in payload.media_type.lower() else ".bin"
        relative = Path("data") / "raw" / provider / dataset / stamp / f"{digest}{extension}"
        target = self.project_root / relative
        _atomic_write(target, payload.body, self.project_root)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ArchiveError("raw payload hash mismatch after write")
        return ArchivedRaw(relative.as_posix(), digest, len(payload.body))


class NormalizedArchive:
    def __init__(self, project_root: str | Path) -> None:
        self.project_root = _safe_root(project_root)
        self.root = self.project_root / "data" / "normalized"

    def append(self, observation: DataObservation) -> AppendResult:
        provenance = observation.provenance
        raw_path = self.project_root.joinpath(*Path(provenance.raw_reference).parts)
        try:
            raw_path.resolve().relative_to(self.project_root)
        except ValueError as exc:
            raise ArchiveError("raw reference escapes project root") from exc
        if not raw_path.is_file():
            raise ArchiveError("raw payload must exist before normalization is stored")
        raw_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        if raw_hash != provenance.raw_sha256:
            raise ArchiveError("normalized observation does not match raw payload hash")
        provider = _component(provenance.provider)
        dataset = _component(provenance.dataset)
        payload = canonical_json_bytes(observation)
        digest = hashlib.sha256(payload).hexdigest()
        relative = Path("data") / "normalized" / provider / dataset / f"{provenance.observation_id}.json"
        target = self.project_root / relative
        existed = target.exists()
        _atomic_write(target, payload, self.project_root)
        return AppendResult(relative.as_posix(), digest, "DUPLICATE" if existed else "APPENDED")
