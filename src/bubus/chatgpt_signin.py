"""« Continuer avec ChatGPT » pour la version PC de BUBUS (abonnement ChatGPT).

Flux « open source / hébergé localement » d'OpenAI : enregistrement dynamique
(`dynamic_agent_client`), PKCE, retour obligatoire sur 127.0.0.1, puis appels
`POST /v1/responses` avec le jeton d'accès. Les jetons restent sur ce PC, dans un
fichier chiffré par Windows (DPAPI, lisible seulement par ce compte Windows).
La version hébergée utilise un client partenaire enregistré et un coffre serveur distinct.

Documentation : https://developers.openai.com/siwc/token-sharing-open-source
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

AUTHORIZE_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
DISCOVERY_URL = "https://auth.openai.com/.well-known/openid-configuration"
ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
RESPONSES_URL = "https://api.openai.com/v1/responses"
MODELS_URL = "https://api.openai.com/v1/models"
USAGE_URL = "https://chatgpt.com/settings/usage"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
AGENT_NAME = "BUBUS"
CALLBACK_PATH = "/auth/callback"
PENDING_SECONDS = 600


class ChatGPTSignInError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --- Chiffrement local (DPAPI) ----------------------------------------------
class DpapiProtector:
    """Chiffre avec le compte Windows courant (CryptProtectData)."""

    @property
    def available(self) -> bool:
        return os.name == "nt"

    def _call(self, data: bytes, protect: bool) -> bytes:
        import ctypes
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        buffer = ctypes.create_string_buffer(data, len(data))
        source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        result = Blob()
        # Même signature pour les deux fonctions ; 0x1 = CRYPTPROTECT_UI_FORBIDDEN.
        function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        ok = function(ctypes.byref(source), "BUBUS ChatGPT" if protect else None, None, None, None, 0x1,
                      ctypes.byref(result))
        if not ok:
            raise ChatGPTSignInError("Le chiffrement Windows des jetons ChatGPT a échoué.", 500)
        try:
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            kernel32.LocalFree(result.pbData)

    def protect(self, data: bytes) -> bytes:
        return self._call(data, True)

    def unprotect(self, data: bytes) -> bytes:
        return self._call(data, False)


# --- HTTP ---------------------------------------------------------------------
def _http_json(method: str, url: str, *, form: dict | None = None, headers: dict | None = None,
               timeout: float = 30) -> tuple[int, Any]:
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/json", **({"Content-Type": "application/x-www-form-urlencoded"} if form else {}),
        **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read()
        exc.close()
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {}


def _stream_responses(token: str, body: dict, timeout: float = 120) -> tuple[int, list[dict]]:
    """Envoie une requête Responses en flux et renvoie les événements lus."""
    request = urllib.request.Request(RESPONSES_URL, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "text/event-stream"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            events = []
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload and payload != "[DONE]":
                        try:
                            events.append(json.loads(payload))
                        except ValueError:
                            continue
            return response.status, events
    except urllib.error.HTTPError as exc:
        body = exc.read()
        exc.close()
        try:
            return exc.code, [json.loads(body)]
        except ValueError:
            return exc.code, []


# --- Validation du jeton d'identité ------------------------------------------
def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _fetch_jwks() -> dict:
    status, discovery = _http_json("GET", DISCOVERY_URL)
    if status != 200 or not isinstance(discovery, dict) or not discovery.get("jwks_uri"):
        raise ChatGPTSignInError("Impossible de lire la configuration de connexion d’OpenAI.", 502)
    status, jwks = _http_json("GET", discovery["jwks_uri"])
    if status != 200 or not isinstance(jwks, dict):
        raise ChatGPTSignInError("Impossible de lire les clés de signature d’OpenAI.", 502)
    return jwks


def verify_id_token(id_token: str, *, client_id: str, nonce: str, jwks: dict, now: float | None = None) -> dict:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.exceptions import InvalidSignature

    try:
        header_b64, payload_b64, signature_b64 = id_token.split(".")
        header = json.loads(_b64url_decode(header_b64))
        claims = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(signature_b64)
    except (ValueError, TypeError):
        raise ChatGPTSignInError("Jeton d’identité ChatGPT illisible.") from None
    if header.get("alg") != "RS256":
        raise ChatGPTSignInError("Signature du jeton ChatGPT non prise en charge.")
    key = next((k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid") and k.get("kty") == "RSA"), None)
    if key is None:
        raise ChatGPTSignInError("Clé de signature ChatGPT inconnue.")
    public_key = rsa.RSAPublicNumbers(int.from_bytes(_b64url_decode(key["e"]), "big"),
                                      int.from_bytes(_b64url_decode(key["n"]), "big")).public_key()
    try:
        public_key.verify(signature, f"{header_b64}.{payload_b64}".encode(), padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise ChatGPTSignInError("Signature du jeton ChatGPT invalide.") from None
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    current = time.time() if now is None else now
    if (claims.get("iss") != ISSUER or client_id not in audiences or claims.get("nonce") != nonce
            or not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < current - 60
            or not claims.get("sub")):
        raise ChatGPTSignInError("Jeton d’identité ChatGPT refusé (émetteur, destinataire, date ou nonce).")
    return claims


# --- Service ------------------------------------------------------------------
class ChatGPTSignIn:
    def __init__(self, data_dir: Path, *, protector: Any | None = None,
                 http_json: Callable[..., tuple[int, Any]] = _http_json,
                 stream: Callable[..., tuple[int, list[dict]]] = _stream_responses,
                 jwks_loader: Callable[[], dict] = _fetch_jwks,
                 clock: Callable[[], float] = time.time, web_client_id: str = "",
                 web_redirect_uri: str = "", web_client_secret: str = ""):
        self._dir = Path(data_dir)
        self._web_client_id = web_client_id
        self._web_redirect_uri = web_redirect_uri
        self._web_client_secret = web_client_secret
        self._session_lock = threading.RLock()
        self._protector = protector or DpapiProtector()
        self._http = http_json
        self._stream = stream
        self._jwks = jwks_loader
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}

    # stockage
    @property
    def available(self) -> bool:
        return bool(getattr(self._protector, "available", False))

    @property
    def _token_file(self) -> Path:
        return self._dir / "chatgpt-session.bin"

    def _host_id(self) -> str:
        path = self._dir / "chatgpt-host-id.txt"
        try:
            value = path.read_text(encoding="ascii").strip()
            if value.startswith("urn:uuid:"):
                return value
        except OSError:
            pass
        value = f"urn:uuid:{uuid.uuid4()}"
        self._dir.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="ascii")
        return value

    def _load(self) -> dict | None:
        try:
            raw = self._token_file.read_bytes()
        except OSError:
            return None
        try:
            return json.loads(self._protector.unprotect(raw))
        except (ValueError, ChatGPTSignInError):
            return None

    def _save(self, record: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        temporary = self._token_file.with_suffix(".tmp")
        temporary.write_bytes(self._protector.protect(json.dumps(record).encode()))
        os.replace(temporary, self._token_file)

    def _forget(self) -> None:
        try:
            self._token_file.unlink()
        except OSError:
            pass

    # état public (aucun jeton n'en sort)
    def state(self) -> dict[str, Any]:
        record = self._load()
        if not record:
            return {"available": self.available, "signed_in": False, "orders_enabled": False}
        return {"available": self.available, "signed_in": True, "email": record.get("email"),
                "plan_enabled": PLAN_SCOPE in record.get("scopes", []), "model": record.get("model"),
                "model_name": record.get("model_name"), "usage_url": USAGE_URL, "orders_enabled": False}

    @property
    def ready(self) -> bool:
        record = self._load()
        return bool(record and PLAN_SCOPE in record.get("scopes", []) and record.get("model"))

    # connexion
    def start(self, port: int, *, browser_session: str = "") -> str:
        if not self.available:
            raise ChatGPTSignInError("La connexion ChatGPT n’est possible que sur la version PC (Windows).", 409)
        verifier = secrets.token_urlsafe(64)
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        if self._web_client_id and not browser_session:
            raise ChatGPTSignInError("Reconnecte-toi à BUBUS avant de continuer.", 401)
        redirect_uri = self._web_redirect_uri or f"http://127.0.0.1:{int(port)}{CALLBACK_PATH}"
        record = self._load()
        client_id = self._web_client_id or (record.get("client_id") if record else None)
        params = {
            "client_id": client_id or "dynamic_agent_client",
            "response_type": "code", "redirect_uri": redirect_uri,
            "scope": SCOPES, "resource": RESOURCE, "state": state, "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(),
        }
        if not self._web_client_id:
            params["ext_agent_host_id"] = self._host_id()
        if not client_id:
            params["agent_name_hint"] = AGENT_NAME
        elif record and record.get("email"):
            params["login_hint"] = record["email"]
        with self._lock:
            now = self._clock()
            self._pending = {k: v for k, v in self._pending.items() if now - v["created"] < PENDING_SECONDS}
            self._pending[state] = {"verifier": verifier, "nonce": nonce, "redirect_uri": redirect_uri,
                                    "client_id": client_id, "browser_session": browser_session, "created": now}
        return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)

    def finish(self, query: dict[str, str], *, browser_session: str = "") -> dict[str, Any]:
        with self._session_lock:
            return self._finish(query, browser_session)

    def _finish(self, query: dict[str, str], browser_session: str) -> dict[str, Any]:
        state = query.get("state", "")
        with self._lock:
            pending = self._pending.get(state)
            if pending and secrets.compare_digest(pending["browser_session"], browser_session):
                del self._pending[state]
            else:
                pending = None
        if pending is None or self._clock() - pending["created"] > PENDING_SECONDS:
            raise ChatGPTSignInError("Cette tentative de connexion a expiré. Recommence depuis BUBUS.")
        if query.get("error"):
            raise ChatGPTSignInError("Connexion ChatGPT annulée.")
        code = query.get("code", "")
        client_id = pending["client_id"]
        returned = query.get("client_id")
        if client_id is None:
            if not returned or returned == "dynamic_agent_client":
                raise ChatGPTSignInError("OpenAI n’a pas renvoyé d’identifiant d’appli.", 502)
            client_id = returned
        elif returned and returned != client_id:
            raise ChatGPTSignInError("OpenAI a renvoyé un autre identifiant d’appli : connexion refusée.", 502)
        if not code:
            raise ChatGPTSignInError("OpenAI n’a pas renvoyé de code de connexion.", 502)
        status, tokens = self._post_token(TOKEN_URL, {
            "grant_type": "authorization_code", "client_id": client_id, "code": code,
            "code_verifier": pending["verifier"], "redirect_uri": pending["redirect_uri"], "resource": RESOURCE})
        if status != 200 or not isinstance(tokens, dict) or not isinstance(tokens.get("id_token"), str):
            raise ChatGPTSignInError("OpenAI a refusé la connexion. Réessaie depuis la discussion.", 502)
        scopes = str(tokens.get("scope", "")).split()
        if PLAN_SCOPE in scopes and not tokens.get("access_token"):
            raise ChatGPTSignInError("OpenAI n’a pas accordé l’accès à la discussion.", 403)
        claims = verify_id_token(tokens.get("id_token", ""), client_id=client_id, nonce=pending["nonce"],
                                 jwks=self._jwks(), now=self._clock())
        record = {"client_id": client_id, "subject": claims["sub"], "email": claims.get("email"),
                  "access_token": tokens.get("access_token"),
                  "refresh_token": tokens.get("refresh_token"),
                  "expires_at": self._clock() + int(tokens.get("expires_in") or 3600),
                  "scopes": scopes}
        if PLAN_SCOPE in record["scopes"]:
            record.update(self._pick_model(record["access_token"]))
        self._save(record)
        return self.state()

    def _pick_model(self, access_token: str) -> dict[str, Any]:
        status, payload = self._http("GET", MODELS_URL, headers={"Authorization": f"Bearer {access_token}"})
        models = payload.get("models", payload.get("data", [])) if isinstance(payload, dict) else []
        listed = [m for m in models if isinstance(m, dict) and m.get("visibility", "list") == "list"
                  and (m.get("slug") or m.get("id"))]
        if status != 200 or not listed:
            return {"model": None, "model_name": None}
        first = listed[0]
        return {"model": first.get("slug") or first.get("id"),
                "model_name": first.get("display_name") or first.get("slug") or first.get("id")}

    def _post_token(self, endpoint: str, form: dict):
        if self._web_client_secret:
            credentials = ":".join(urllib.parse.quote_plus(value, safe="") for value in
                                   (self._web_client_id, self._web_client_secret))
            auth = base64.b64encode(credentials.encode("ascii")).decode("ascii")
            return self._http("POST", endpoint, form=form, headers={"Authorization": f"Basic {auth}"})
        return self._http("POST", endpoint, form=form)

    def disconnect(self) -> None:
        with self._session_lock:
            with self._lock:
                self._pending.clear()
            self._disconnect()

    def _disconnect(self) -> None:
        record = self._load()
        if record and record.get("refresh_token"):
            try:
                status, discovery = self._http("GET", DISCOVERY_URL)
                endpoint = discovery.get("revocation_endpoint") if isinstance(discovery, dict) else None
                if status == 200 and endpoint:
                    self._post_token(endpoint, {"token": record["refresh_token"],
                                                       "token_type_hint": "refresh_token",
                                                       "client_id": record["client_id"]})
            except Exception:  # noqa: BLE001 - la déconnexion locale doit toujours aboutir
                pass
        self._forget()

    # utilisation
    def _fresh_record(self) -> dict:
        record = self._load()
        if not record:
            raise ChatGPTSignInError("Connecte d’abord ton compte ChatGPT.", 409)
        if PLAN_SCOPE not in record.get("scopes", []):
            raise ChatGPTSignInError("L’utilisation de ton abonnement n’a pas été autorisée. Reconnecte ChatGPT.", 409)
        if self._clock() < record.get("expires_at", 0) - 120:
            return record
        status, tokens = self._post_token(TOKEN_URL, {
            "grant_type": "refresh_token", "client_id": record["client_id"],
            "refresh_token": record.get("refresh_token", ""), "resource": RESOURCE})
        if status != 200 or not isinstance(tokens, dict) or not tokens.get("access_token"):
            error = tokens.get("error") if isinstance(tokens, dict) else None
            if status in {400, 401} or error in {"invalid_grant", "invalid_refresh_token", "token_expired",
                                                 "refresh_token_expired", "refresh_token_invalidated",
                                                 "refresh_token_reused"}:
                self._forget()
                raise ChatGPTSignInError("Ta connexion ChatGPT a expiré. Reconnecte-toi.", 401)
            raise ChatGPTSignInError("OpenAI ne répond pas pour renouveler la connexion. Réessaie.", 503)
        record.update({"access_token": tokens["access_token"],
                       "refresh_token": tokens.get("refresh_token") or record.get("refresh_token"),
                       "expires_at": self._clock() + int(tokens.get("expires_in") or 3600)})
        if tokens.get("scope"):
            record["scopes"] = str(tokens["scope"]).split()
        self._save(record)
        return record

    def complete(self, *, system: str, messages: list[dict[str, str]], snapshot: str) -> str:
        with self._session_lock:
            record = self._record_for_request()
        return self._complete(record, system, messages, snapshot)

    def _record_for_request(self) -> dict:
        record = self._fresh_record()
        if PLAN_SCOPE not in record["scopes"]:
            raise ChatGPTSignInError("Autorise BUBUS à utiliser ton abonnement ChatGPT.", 403)
        if not record.get("model"):
            record.update(self._pick_model(record["access_token"]))
            if not record.get("model"):
                raise ChatGPTSignInError("Aucun modèle ChatGPT n’est disponible pour ton compte.", 409)
            self._save(record)
        return record

    def _complete(self, record: dict, system: str, messages: list[dict[str, str]], snapshot: str) -> str:
        items = [{"role": "developer", "content": system}]
        items += [{"role": m["role"], "content": m["content"]} for m in messages[:-1]]
        items.append({"role": "user", "content": snapshot + "\n\n" + messages[-1]["content"]})
        status, events = self._stream(record["access_token"], {
            "model": record["model"], "input": items, "store": False, "stream": True})
        text, completed, error_code = [], False, None
        for event in events:
            kind = event.get("type")
            if kind == "response.output_text.delta":
                text.append(str(event.get("delta", "")))
            elif kind == "response.completed":
                completed = True
            elif kind in {"response.failed", "error"} or "error" in event:
                error = event.get("error") or event.get("response", {}).get("error") or {}
                error_code = error.get("code") if isinstance(error, dict) else None
        if status != 200 or not completed:
            raise _plan_error(status, error_code)
        return "".join(text).strip() or "Je n'ai pas de réponse à donner. Reformule ta question."


def _plan_error(status: int, code: str | None) -> ChatGPTSignInError:
    if code == "subscription_sharing_usage_limit_exceeded" or status == 429:
        return ChatGPTSignInError(f"Limite de ton abonnement ChatGPT atteinte. Vérifie sur {USAGE_URL}", 429)
    if code == "subscription_sharing_user_not_eligible":
        return ChatGPTSignInError("Ton abonnement ChatGPT ne permet pas cette utilisation (Plus ou Pro requis).", 403)
    if code in {"subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable"} or status == 503:
        return ChatGPTSignInError("ChatGPT est momentanément indisponible. Réessaie dans un moment.", 503)
    if code == "subscription_sharing_invalid_user" or status == 401:
        return ChatGPTSignInError("ChatGPT a refusé ta connexion. Reconnecte ton compte.", 401)
    return ChatGPTSignInError("ChatGPT n’a pas pu répondre. Réessaie.", 502)
