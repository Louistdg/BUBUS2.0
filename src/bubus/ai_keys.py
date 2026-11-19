"""Clé OpenRouter saisie dans BUBUS sur le PC, chiffrée par Windows (DPAPI).

La clé n'est jamais renvoyée au navigateur : seul « une clé est enregistrée »
et le modèle choisi sont visibles. Version PC seulement ; en ligne, la clé va
dans la variable Railway OPENROUTER_API_KEY.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from bubus.chatgpt_signin import DpapiProtector

OPENROUTER_KEY = re.compile(r"sk-or-[A-Za-z0-9_\-]{20,200}\Z")
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,80}/[A-Za-z0-9][A-Za-z0-9._:\-]{0,80}\Z")
DEFAULT_OPENROUTER_MODEL = "anthropic/claude-sonnet-4.6"


class AIKeyError(ValueError):
    """Message en français, montrable tel quel."""


class OpenRouterKeyStore:
    def __init__(self, data_dir: Path, *, protector: Any | None = None):
        self._file = Path(data_dir) / "openrouter-key.bin"
        self._protector = protector or DpapiProtector()
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self._protector.available)

    def _load(self) -> dict | None:
        if not self.available or not self._file.exists():
            return None
        try:
            record = json.loads(self._protector.unprotect(self._file.read_bytes()))
        except Exception:  # noqa: BLE001 - fichier abîmé ou autre compte Windows
            return None
        if not isinstance(record, dict) or not OPENROUTER_KEY.fullmatch(str(record.get("key", ""))):
            return None
        return record

    def credentials(self) -> tuple[str, str] | None:
        with self._lock:
            record = self._load()
        if record is None:
            return None
        return record["key"], record.get("model") or DEFAULT_OPENROUTER_MODEL

    def state(self) -> dict[str, Any]:
        found = self.credentials()
        return {"available": self.available, "saved": found is not None,
                "model": found[1] if found else None, "default_model": DEFAULT_OPENROUTER_MODEL}

    def save(self, key: Any, model: Any = "") -> dict[str, Any]:
        if not self.available:
            raise AIKeyError("Le coffre Windows n’est pas disponible sur cet ordinateur.")
        key = key.strip() if isinstance(key, str) else ""
        model = model.strip() if isinstance(model, str) else ""
        if not OPENROUTER_KEY.fullmatch(key):
            raise AIKeyError("Ce n’est pas une clé OpenRouter : elle doit commencer par « sk-or- ».")
        if model and not MODEL_NAME.fullmatch(model):
            raise AIKeyError("Nom de modèle invalide. Exemple : openai/gpt-5 ou anthropic/claude-opus-4.5.")
        blob = self._protector.protect(json.dumps({"key": key, "model": model or None}).encode("utf-8"))
        with self._lock:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._file.with_suffix(".tmp")
            temporary.write_bytes(blob)
            temporary.replace(self._file)
        return self.state()

    def forget(self) -> dict[str, Any]:
        with self._lock:
            self._file.unlink(missing_ok=True)
        return self.state()
