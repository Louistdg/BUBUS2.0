"""Connexions IA du propriétaire de BUBUS. Les clés sont chiffrées avant écriture."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from bubus.ai_keys import AIKeyError
from bubus.chatgpt_signin import ChatGPTSignInError


@dataclass(frozen=True)
class Provider:
    label: str
    variable: str
    model: str = ""
    endpoint: str = ""


PROVIDERS = {
    "openrouter": Provider("OpenRouter", "OPENROUTER_API_KEY", "anthropic/claude-sonnet-4.6", "https://openrouter.ai/api/v1/chat/completions"),
    "zai": Provider("Z.ai · GLM", "ZAI_API_KEY", "glm-5.1", "https://api.z.ai/api/paas/v4/chat/completions"),
    "openai": Provider("OpenAI", "OPENAI_API_KEY", "gpt-5.4", "https://api.openai.com/v1/chat/completions"),
    "claude": Provider("Claude", "ANTHROPIC_API_KEY", "claude-opus-5-5"),
    "deepseek": Provider("DeepSeek", "DEEPSEEK_API_KEY", "deepseek-v4-pro", "https://api.deepseek.com/chat/completions"),
    "kimi": Provider("Kimi", "MOONSHOT_API_KEY", "kimi-k2.6", "https://api.moonshot.ai/v1/chat/completions"),
    "groq": Provider("Groq", "GROQ_API_KEY", "openai/gpt-oss-120b", "https://api.groq.com/openai/v1/chat/completions"),
    "elevenlabs": Provider("ElevenLabs", "ELEVENLABS_API_KEY"),
    "cursor": Provider("Cursor", "CURSOR_API_KEY"),
}


class AIConnections:
    def __init__(self, directory, protector, legacy=None):
        self.path = Path(directory) / "ai-connections.sqlite3"
        self.protector = protector
        self.legacy = legacy
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS credentials (provider TEXT PRIMARY KEY, secret BLOB NOT NULL, model TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS preference (id INTEGER PRIMARY KEY CHECK (id = 1), provider TEXT NOT NULL);
                """)

    def _lookup(self, db, name):
        row = db.execute("SELECT secret, model FROM credentials WHERE provider=?", (name,)).fetchone()
        if row:
            try:
                return self.protector.unprotect(row[0]).decode(), row[1]
            except (ChatGPTSignInError, UnicodeError):
                return None
        spec = PROVIDERS[name]
        key = os.getenv(spec.variable, "").strip()
        if key:
            return key, spec.model
        if name == "openrouter" and self.legacy:
            return self.legacy.credentials()
        return None

    def selected(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN")
            row = db.execute("SELECT provider FROM preference WHERE id=1").fetchone()
            if row:
                found = self._lookup(db, row[0])
                if found:
                    return row[0], *found
        return None

    def state(self):
        with closing(sqlite3.connect(self.path)) as db:
            saved = dict(db.execute("SELECT provider,model FROM credentials"))
            row = db.execute("SELECT provider FROM preference WHERE id=1").fetchone()
        rows = []
        for name, spec in PROVIDERS.items():
            inherited = bool(os.getenv(spec.variable, "").strip())
            if name == "openrouter" and self.legacy:
                inherited = inherited or self.legacy.state()["saved"]
            rows.append({"id": name, "label": spec.label, "model": saved.get(name, spec.model),
                         "chat": bool(spec.model), "saved": name in saved, "configured": name in saved or inherited})
        return {"providers": rows, "selected": row[0] if row else "", "writable": self.protector.available}

    def update(self, data):
        if not self.protector.available:
            raise AIKeyError("Le coffre de BUBUS est indisponible.")
        if not isinstance(data, dict):
            raise AIKeyError("Demande invalide.")
        op = data.get("action")
        fields = {"save": {"action", "keys"}, "select": {"action", "provider"}, "forget": {"action", "provider"}}
        if not isinstance(op, str) or set(data) != fields.get(op):
            raise AIKeyError("Action inconnue.")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if op == "save":
                keys = data["keys"]
                if not isinstance(keys, dict) or not keys or not keys.keys() <= PROVIDERS.keys():
                    raise AIKeyError("Fournisseur inconnu.")
                for name, record in keys.items():
                    if not isinstance(record, dict) or set(record) != {"key", "model"}:
                        raise AIKeyError("Format de clé invalide.")
                    key, model = record["key"], record["model"]
                    if not isinstance(key, str) or not re.fullmatch(r"[!-~]{12,1024}", key):
                        raise AIKeyError("Vérifie la clé de " + PROVIDERS[name].label + ".")
                    if not isinstance(model, str) or (model and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,159}", model)):
                        raise AIKeyError("Nom de modèle invalide.")
                    if not PROVIDERS[name].model and model:
                        raise AIKeyError("Ce fournisseur ne propose pas de modèle de discussion.")
                    db.execute("INSERT OR REPLACE INTO credentials VALUES (?,?,?)",
                               (name, self.protector.protect(key.encode()), model or PROVIDERS[name].model))
                first = next((name for name, spec in PROVIDERS.items() if name in keys and spec.model), None)
                if first:
                    db.execute("INSERT OR IGNORE INTO preference VALUES(1,?)", (first,))
            else:
                name = data["provider"]
                if name == "" and op == "select":
                    db.execute("DELETE FROM preference")
                elif not isinstance(name, str) or name not in PROVIDERS:
                    raise AIKeyError("Fournisseur inconnu.")
                elif op == "forget":
                    db.execute("DELETE FROM credentials WHERE provider=?", (name,))
                    db.execute("DELETE FROM preference WHERE provider=?", (name,))
                elif PROVIDERS[name].model and self._lookup(db, name):
                    db.execute("INSERT OR REPLACE INTO preference VALUES(1,?)", (name,))
                else:
                    raise AIKeyError("Enregistre une clé de discussion pour ce fournisseur.")
        return self.state()

    def inspect_account(self, name):
        if name not in ("elevenlabs", "cursor"):
            raise AIKeyError("Service inconnu.")
        with closing(sqlite3.connect(self.path)) as db:
            entry = self._lookup(db, name)
        if not entry:
            raise AIKeyError("Enregistre d’abord ta clé.")
        if name == "cursor":
            req = urllib.request.Request("https://api.cursor.com/v1/me")
            req.add_unredirected_header("Authorization", "Bearer " + entry[0])
        else:
            req = urllib.request.Request("https://api.elevenlabs.io/v2/voices?page_size=10")
            req.add_unredirected_header("xi-api-key", entry[0])
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                info = json.load(response)
            if name == "cursor":
                return {"message": "Connexion à l’API Cursor vérifiée."}
            voices = [v["name"] for v in info["voices"] if isinstance(v.get("name"), str)]
            return {"message": "Voix disponibles : " + ", ".join(voices) if voices else "Aucune voix disponible."}
        except urllib.error.HTTPError as exc:
            exc.close()
            raise AIKeyError(f"{PROVIDERS[name].label} a refusé la demande ({exc.code}).") from None
        except (OSError, ValueError, KeyError, TypeError):
            raise AIKeyError("Le service est momentanément indisponible.") from None
