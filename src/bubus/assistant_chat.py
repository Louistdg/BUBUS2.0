"""Discussion libre avec une IA (Claude ou OpenAI), en lecture seule.

L'IA reçoit un relevé des marchés publics déjà chargés par BUBUS2.0 et répond
en français. Elle n'a aucun outil : elle ne peut ni lire de clé, ni passer
d'ordre, ni modifier quoi que ce soit. Les clés d'API IA viennent uniquement des
variables d'environnement et ne sont jamais renvoyées au navigateur.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
MAX_HISTORY_MESSAGES = 12
MAX_MESSAGE_CHARS = 4000
MAX_REQUEST_BYTES = 64 * 1024
DEFAULT_DAILY_LIMIT = 60

SYSTEM_PROMPT = """Tu es l'assistant de BUBUS2.0, une application personnelle de suivi des marchés.
Tu parles français, simplement, comme à un ami qui n'est pas expert. Réponses courtes par défaut :
quelques phrases ou une petite liste, sauf si on te demande plus de détail.

Règles :
- Tu t'appuies d'abord sur le relevé « Données BUBUS » joint au dernier message. Cite l'heure du
  relevé quand tu donnes un cours. Si une donnée n'y est pas, dis-le au lieu d'inventer un chiffre.
- Tu ne donnes pas de conseil d'investissement personnalisé : pas de « achète », « vends » ou
  « mets X € ». Tu peux expliquer les faits, les risques, la volatilité, les scénarios possibles, et
  rappeler des principes généraux (diversifier, acheter progressivement, ne pas investir ce qu'on ne
  peut pas perdre). La décision reste à l'utilisateur.
- Tu n'as aucun accès au portefeuille, aux clés ni aux ordres, et tu ne peux rien exécuter. Si on te
  demande de passer un ordre, explique que BUBUS ne le fait pas et que l'utilisateur doit agir lui-même
  dans son application d'échange.
- Les performances passées et les tendances ne prédisent pas l'avenir : présente-les comme des
  indications, jamais comme des certitudes.
"""


class ChatUnavailable(Exception):
    """L'IA n'est pas configurée ou la limite du jour est atteinte."""

    def __init__(self, message: str, status: int = 503):
        super().__init__(message)
        self.status = status


class ChatRequestError(ValueError):
    """La conversation envoyée par le navigateur est invalide."""


@dataclass(frozen=True)
class ChatConfig:
    provider: str | None
    model: str | None
    api_key: str | None
    daily_limit: int

    @property
    def ready(self) -> bool:
        return bool(self.provider and self.model and self.api_key)

    def public_state(self) -> dict[str, Any]:
        labels = {"claude": "Claude (Anthropic)", "openai": "OpenAI"}
        if self.ready:
            reason = None
        elif self.provider == "openai" and not self.model:
            reason = "Choisis le modèle OpenAI avec la variable BUBUS_AI_MODEL."
        else:
            reason = ("Ajoute ANTHROPIC_API_KEY (Claude) ou OPENAI_API_KEY (OpenAI) "
                      "dans les variables d'environnement du serveur.")
        return {"ready": self.ready, "provider": labels.get(self.provider or ""),
                "model": self.model if self.ready else None, "daily_limit": self.daily_limit,
                "reason": reason, "orders_enabled": False}


def load_config(environ: dict[str, str] | None = None) -> ChatConfig:
    env = os.environ if environ is None else environ
    choice = env.get("BUBUS_AI_PROVIDER", "").strip().casefold()
    claude_key = env.get("ANTHROPIC_API_KEY", "").strip()
    openai_key = env.get("OPENAI_API_KEY", "").strip()
    if choice not in {"claude", "openai"}:
        choice = "claude" if claude_key else ("openai" if openai_key else "")
    try:
        limit = int(env.get("BUBUS_AI_DAILY_MESSAGE_LIMIT", str(DEFAULT_DAILY_LIMIT)))
    except ValueError:
        limit = DEFAULT_DAILY_LIMIT
    limit = max(0, min(limit, 1000))
    model = env.get("BUBUS_AI_MODEL", "").strip() or None
    if choice == "claude":
        return ChatConfig("claude", model or DEFAULT_CLAUDE_MODEL, claude_key or None, limit)
    if choice == "openai":
        return ChatConfig("openai", model, openai_key or None, limit)
    return ChatConfig(None, None, None, limit)


class DailyQuota:
    """Compte les messages envoyés à l'IA par jour (UTC) pour plafonner la dépense."""

    def __init__(self, clock: Callable[[], datetime] | None = None):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._day = None
        self._count = 0

    def take(self, limit: int) -> int:
        with self._lock:
            today = self._clock().date()
            if today != self._day:
                self._day, self._count = today, 0
            if self._count >= limit:
                raise ChatUnavailable(
                    f"Limite de {limit} messages par jour atteinte. Elle se remet à zéro à minuit (UTC).", 429)
            self._count += 1
            return limit - self._count


def parse_conversation(raw: bytes) -> list[dict[str, str]]:
    if len(raw) > MAX_REQUEST_BYTES:
        raise ChatRequestError("Conversation trop longue.")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ChatRequestError("JSON invalide.") from None
    if not isinstance(payload, dict) or set(payload) != {"messages"} or not isinstance(payload["messages"], list):
        raise ChatRequestError("Format attendu : {\"messages\": [...]}.")
    messages = payload["messages"][-MAX_HISTORY_MESSAGES:]
    cleaned: list[dict[str, str]] = []
    for item in messages:
        if (not isinstance(item, dict) or set(item) != {"role", "content"}
                or item["role"] not in {"user", "assistant"} or not isinstance(item["content"], str)):
            raise ChatRequestError("Message invalide.")
        text = item["content"].strip()
        if not text or len(text) > MAX_MESSAGE_CHARS:
            raise ChatRequestError(f"Chaque message doit faire entre 1 et {MAX_MESSAGE_CHARS} caractères.")
        cleaned.append({"role": item["role"], "content": text})
    while cleaned and cleaned[0]["role"] != "user":
        cleaned.pop(0)
    if not cleaned or cleaned[-1]["role"] != "user":
        raise ChatRequestError("Le dernier message doit venir de toi.")
    return cleaned


def market_snapshot(server: Any, *, crypto_limit: int = 30, global_limit: int = 20) -> str:
    """Relevé compact des marchés publics déjà chargés par le serveur."""
    lines = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"Données BUBUS — relevé du {now}. Aucune donnée de portefeuille n'est fournie.")
    try:
        crypto = server.cryptocom_market_overview.get_overview()
        rows = [m for m in crypto.get("markets", []) if isinstance(m, dict)]
        rows.sort(key=lambda m: _number(m.get("volume_24h_usd")), reverse=True)
        lines.append(f"Crypto.com Exchange (Spot, source {crypto.get('fetched_at', 'heure inconnue')}) — "
                     f"{len(rows)} paires, les {min(crypto_limit, len(rows))} plus échangées :")
        for m in rows[:crypto_limit]:
            lines.append(f"- {m.get('symbol')} : {m.get('price')} {m.get('currency', '')}, "
                         f"24 h {m.get('change_24h_pct', '?')} %, volume 24 h {m.get('volume_24h_usd', '?')} USD")
    except Exception:  # noqa: BLE001 - une source en panne ne doit pas bloquer la discussion
        lines.append("Crypto.com Exchange : données indisponibles pour le moment.")
    try:
        gecko = server.coingecko_market_overview.get_overview()
        rows = [m for m in gecko.get("markets", []) if isinstance(m, dict)]
        rows.sort(key=lambda m: _number(m.get("market_cap_rank"), default=10**9))
        lines.append(f"CoinGecko (vue globale, source {gecko.get('fetched_at', 'heure inconnue')}) :")
        for m in rows[:global_limit]:
            lines.append(f"- #{m.get('market_cap_rank')} {m.get('name')} ({m.get('symbol')}) : "
                         f"{m.get('price_usd')} USD, 24 h {m.get('change_24h_pct', '?')} %, "
                         f"capitalisation {m.get('market_cap_usd', '?')} USD")
    except Exception:  # noqa: BLE001
        lines.append("CoinGecko : données indisponibles pour le moment.")
    lines.append("Actions et ETF : pas encore de cours dans BUBUS.")
    return "\n".join(lines)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class ChatService:
    def __init__(self, *, config_loader: Callable[[], ChatConfig] = load_config,
                 claude_call: Callable[..., str] | None = None,
                 openai_call: Callable[..., str] | None = None,
                 quota: DailyQuota | None = None):
        self._config_loader = config_loader
        self._claude_call = claude_call or _call_claude
        self._openai_call = openai_call or _call_openai
        self._quota = quota or DailyQuota()

    def state(self) -> dict[str, Any]:
        return self._config_loader().public_state()

    def reply(self, messages: list[dict[str, str]], snapshot: str) -> dict[str, Any]:
        config = self._config_loader()
        if not config.ready:
            raise ChatUnavailable(config.public_state()["reason"])
        remaining = self._quota.take(config.daily_limit)
        call = self._claude_call if config.provider == "claude" else self._openai_call
        text = call(config=config, system=SYSTEM_PROMPT, messages=messages, snapshot=snapshot)
        return {"reply": text, "provider": config.public_state()["provider"], "model": config.model,
                "remaining_today": remaining, "orders_enabled": False}


def _call_claude(*, config: ChatConfig, system: str, messages: list[dict[str, str]], snapshot: str) -> str:
    import anthropic

    api_messages: list[dict[str, Any]] = [dict(m) for m in messages[:-1]]
    api_messages.append({"role": "user", "content": [
        {"type": "text", "text": snapshot},
        {"type": "text", "text": messages[-1]["content"]},
    ]})
    client = anthropic.Anthropic(api_key=config.api_key, timeout=90.0, max_retries=1)
    try:
        response = client.beta.messages.create(
            model=config.model,
            max_tokens=4000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=api_messages,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError:
        raise ChatUnavailable("La clé Claude est refusée. Vérifie ANTHROPIC_API_KEY.") from None
    except anthropic.RateLimitError:
        raise ChatUnavailable("Claude est saturé ou la limite de dépense est atteinte. Réessaie plus tard.", 429) from None
    except anthropic.APIStatusError as exc:
        raise ChatUnavailable(f"Claude a renvoyé une erreur ({exc.status_code}). Réessaie plus tard.", 502) from None
    except anthropic.APIConnectionError:
        raise ChatUnavailable("Impossible de joindre Claude pour le moment.", 502) from None
    if response.stop_reason == "refusal":
        return "Je ne peux pas répondre à cette demande. Essaie de la formuler autrement."
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return text or "Je n'ai pas de réponse à donner. Reformule ta question."


def _call_openai(*, config: ChatConfig, system: str, messages: list[dict[str, str]], snapshot: str) -> str:
    api_messages = [{"role": "system", "content": system}] + [dict(m) for m in messages[:-1]]
    api_messages.append({"role": "user", "content": snapshot + "\n\n" + messages[-1]["content"]})
    body = json.dumps({"model": config.model, "messages": api_messages}).encode("utf-8")
    request = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {config.api_key}",
                                              "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 401:
            raise ChatUnavailable("La clé OpenAI est refusée. Vérifie OPENAI_API_KEY.") from None
        if exc.code == 429:
            raise ChatUnavailable("OpenAI est saturé ou la limite de dépense est atteinte. Réessaie plus tard.", 429) from None
        raise ChatUnavailable(f"OpenAI a renvoyé une erreur ({exc.code}). Réessaie plus tard.", 502) from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise ChatUnavailable("Impossible de joindre OpenAI pour le moment.", 502) from None
    try:
        text = payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise ChatUnavailable("Réponse OpenAI illisible.", 502) from None
    return text.strip() or "Je n'ai pas de réponse à donner. Reformule ta question."
