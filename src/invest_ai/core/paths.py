"""Canonical project paths. Path construction is pure; directory creation is explicit."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    root: Path
    config_dir: Path
    data_dir: Path
    knowledge_dir: Path
    knowledge_db: Path
    runtime_db: Path
    backups_dir: Path
    logs_dir: Path
    models_dir: Path
    docs_dir: Path

    @classmethod
    def from_root(cls, root: str | Path) -> "RuntimePaths":
        canonical_root = Path(root).expanduser().resolve()
        data = canonical_root / "data"
        return cls(
            root=canonical_root,
            config_dir=canonical_root / "config",
            data_dir=data,
            knowledge_dir=data / "knowledge",
            knowledge_db=data / "knowledge" / "knowledge.sqlite3",
            runtime_db=data / "runtime" / "runtime.sqlite3",
            backups_dir=canonical_root / "backups",
            logs_dir=canonical_root / "logs",
            models_dir=canonical_root / "models",
            docs_dir=canonical_root / "docs",
        )

    def ensure_runtime_directories(self) -> None:
        """Create only directories owned by the runtime; never initialize Knowledge Core."""
        for path in (self.config_dir, self.data_dir / "runtime", self.backups_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)
