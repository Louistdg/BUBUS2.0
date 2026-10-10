"""Typed contracts for the local INVEST-AI coordination registry."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MissionStatus(StrEnum):
    DRAFT = "DRAFT"
    DECLARED = "DECLARED"
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    WAITING_FOR_RESOURCES = "WAITING_FOR_RESOURCES"
    FIN_REPORTED = "FIN_REPORTED"
    VERIFIED = "VERIFIED"
    VALIDATED = "VALIDATED"
    CORRECTION_REQUIRED = "CORRECTION_REQUIRED"
    CLOSED = "CLOSED"


class ClaimScope(StrEnum):
    PATH = "PATH"
    RESOURCE = "RESOURCE"
    SERVICE = "SERVICE"


class ClaimMode(StrEnum):
    READ = "READ"
    WRITE = "WRITE"
    EXCLUSIVE = "EXCLUSIVE"


class DependencyType(StrEnum):
    REQUIRES = "REQUIRES"
    BLOCKS = "BLOCKS"
    SUPERSEDES = "SUPERSEDES"


class ResourceMode(StrEnum):
    SHARED = "SHARED"
    EXCLUSIVE = "EXCLUSIVE"
    THRESHOLD = "THRESHOLD"
    FORBIDDEN = "FORBIDDEN"


class EvidenceType(StrEnum):
    OBSERVED_ACTION = "OBSERVED_ACTION"
    RAW_RESULT = "RAW_RESULT"
    AGENT_CONCLUSION = "AGENT_CONCLUSION"
    LEAD_DECISION = "LEAD_DECISION"


@dataclass(frozen=True, slots=True)
class CoordinationVerdict:
    mission_id: str
    color: str
    reasons: tuple[str, ...]
    may_start: bool
