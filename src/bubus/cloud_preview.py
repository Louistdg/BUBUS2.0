"""BUBUS2.0 en ligne (Railway) : marchés publics et discussion IA, en lecture seule.

Connexion par page de login et cookie de session signé (compatible avec l'appli
installée sur Android). L'en-tête HTTP Basic reste accepté pour les scripts.
Aucune route de portefeuille privé, de clé ou d'ordre n'est exposée.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import threading
import time
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bubus.chatgpt_signin import ChatGPTSignIn, ChatGPTSignInError
from bubus.server import ASSETS, DeskHandler, make_server


_AUTHENTICATED_GET_PATHS = frozenset({
    "/patrimoine", "/assets/finance.css", "/assets/finance.js", "/assets/finance-chart.js", "/assets/finance-icons.js",
    "/", "/assets/desk.css", "/assets/desk.js", "/assets/ai-connections.js", "/api/desk/ai-connections",
    "/api/desk/state", "/api/market-data/crypto-overview",
    "/api/market-data/coingecko-overview", "/api/market-data/state",
    "/api/desk/candles", "/api/desk/research", "/api/desk/chat",
    "/api/desk/revolutx", "/api/desk/my-list", "/api/desk/my-list/search",
})
# Fichiers d'installation de l'appli : le navigateur les télécharge sans cookie.
_PUBLIC_GET_PATHS = frozenset({
    "/manifest.webmanifest", "/sw.js", "/assets/icon.svg",
    "/assets/icon-192.png", "/assets/icon-512.png", "/assets/icon-maskable-512.png",
})

SESSION_COOKIE = "bubus_session"
SESSION_SECONDS = 30 * 24 * 3600
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES_PER_CLIENT = 8
LOGIN_MAX_FAILURES_TOTAL = 50


def _credentials() -> tuple[str, str]:
    return os.environ.get("BUBUS_PREVIEW_USERNAME", ""), os.environ.get("BUBUS_PREVIEW_PASSWORD", "")


def _session_secret() -> bytes:
    explicit = os.environ.get("BUBUS_SESSION_SECRET", "")
    if len(explicit) >= 32:
        return explicit.encode("utf-8")
    username, password = _credentials()
    # Changer le mot de passe invalide automatiquement toutes les sessions.
    return hashlib.sha256(f"bubus-session|{username}|{password}".encode("utf-8")).digest()


def issue_session_token(now: float | None = None) -> str:
    expires = int((now if now is not None else time.time()) + SESSION_SECONDS)
    body = f"{expires}.{secrets.token_urlsafe(16)}"
    signature = hmac.new(_session_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def session_token_valid(token: str, now: float | None = None) -> bool:
    if not token or len(token) > 256:
        return False
    body, _, signature = token.rpartition(".")
    expires, _, _nonce = body.partition(".")
    if not expires.isdecimal() or not signature:
        return False
    expected = hmac.new(_session_secret(), body.encode("ascii", "replace"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    return int(expires) > (now if now is not None else time.time())


CHATGPT_CALLBACK = "/auth/chatgpt/callback"


class _CloudTokenProtector:
    available = True

    def __init__(self, purpose="chatgpt/web/v1"):
        from cryptography.fernet import Fernet
        username, _ = _credentials()
        context = (f"bubus/{purpose}/" + username).encode()
        key = hmac.digest(_session_secret(), context, "sha256")
        self._cipher = Fernet(base64.urlsafe_b64encode(key))

    def protect(self, data):
        return self._cipher.encrypt(data)

    def unprotect(self, data):
        from cryptography.fernet import InvalidToken
        try:
            return self._cipher.decrypt(data)
        except InvalidToken:
            raise ChatGPTSignInError("Reconnecte ton compte ChatGPT.") from None


def hosted_chatgpt(data_dir):
    client_id = os.environ.get("BUBUS_CHATGPT_CLIENT_ID", "").strip()
    redirect = os.environ.get("BUBUS_CHATGPT_REDIRECT_URI", "").strip()
    if not redirect and os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        redirect = "https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"] + CHATGPT_CALLBACK
    url = urlsplit(redirect)
    if (not client_id or client_id == "dynamic_agent_client" or url.scheme != "https"
            or not url.hostname or url.username or url.password or url.query or url.fragment
            or url.path != CHATGPT_CALLBACK):
        return None
    return ChatGPTSignIn(Path(data_dir) / "chatgpt-web", protector=_CloudTokenProtector(),
                         web_client_id=client_id, web_redirect_uri=redirect,
                         web_client_secret=os.environ.get("BUBUS_CHATGPT_CLIENT_SECRET", ""))


class LoginThrottle:
    """Limite les essais de mot de passe ratés (par client et au total)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    def _prune(self, now: float) -> None:
        for key in list(self._failures):
            kept = [t for t in self._failures[key] if now - t < LOGIN_WINDOW_SECONDS]
            if kept:
                self._failures[key] = kept
            else:
                del self._failures[key]

    def blocked(self, client: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            self._prune(now)
            total = sum(len(v) for v in self._failures.values())
            return (len(self._failures.get(client, [])) >= LOGIN_MAX_FAILURES_PER_CLIENT
                    or total >= LOGIN_MAX_FAILURES_TOTAL)

    def fail(self, client: str, now: float | None = None) -> None:
        with self._lock:
            self._failures.setdefault(client, []).append(time.time() if now is None else now)

    def succeed(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


_LOGIN_PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#1a6354"><link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/assets/icon.svg"><title>BUBUS · Connexion</title>
<style>
:root{{color-scheme:dark;--bg:#0f1716;--card:#16211f;--line:#2a3a37;--text:#eef4f2;--muted:#9fb2ad;--accent:#1f8a6f}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);
font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:16px}}
main{{width:100%;max-width:380px;background:var(--card);border:1px solid var(--line);border-radius:18px;padding:28px 22px}}
.brand{{display:flex;align-items:center;gap:12px;margin-bottom:22px}}.brand img{{width:44px;height:44px}}
h1{{font-size:22px;margin:0}}p{{color:var(--muted);margin:4px 0 0;font-size:14px}}
label{{display:block;font-size:14px;color:var(--muted);margin-top:16px}}
input{{display:block;width:100%;margin-top:6px;padding:13px 14px;font-size:16px;border-radius:12px;border:1px solid var(--line);
background:#0c1312;color:var(--text)}}input:focus{{outline:2px solid var(--accent);outline-offset:1px}}
button{{width:100%;margin-top:22px;padding:14px;font-size:16px;font-weight:600;border:0;border-radius:12px;background:var(--accent);color:#fff}}
.error{{margin-top:16px;padding:10px 12px;border-radius:10px;background:#3a1b1b;color:#ffd7d7;font-size:14px}}
</style></head><body><main>
<div class="brand"><img src="/assets/icon.svg" alt=""><div><h1>BUBUS</h1><p>Marchés et discussion, en lecture seule</p></div></div>
{error}
<form method="post" action="/login" autocomplete="on">
<label>Identifiant<input name="username" autocomplete="username" required maxlength="128" autocapitalize="none"></label>
<label>Mot de passe<input name="password" type="password" autocomplete="current-password" required maxlength="512"></label>
<button type="submit">Se connecter</button>
</form></main></body></html>"""


class AuthenticatedCloudPreviewHandler(DeskHandler):
    """Marchés publics et discussion IA ; aucune route de compte, de clé ou d'ordre."""

    # --- identification -------------------------------------------------
    def _https_ok(self) -> bool:
        return (os.environ.get("BUBUS_PREVIEW_REQUIRE_HTTPS", "true").casefold() != "true"
                or self.headers.get("X-Forwarded-Proto", "").casefold() == "https"
                or self.client_address[0] in {"127.0.0.1", "::1"})

    def _secure_request(self) -> bool:
        return self.headers.get("X-Forwarded-Proto", "").casefold() == "https"

    def _client_key(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "")
        return (forwarded.split(",")[0].strip() if forwarded else self.client_address[0])[:64]

    def _cookie(self, name: str) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return ""

    def _basic_ok(self) -> bool:
        username, password = _credentials()
        header = self.headers.get("Authorization", "")
        scheme, _, encoded = header.partition(" ")
        if scheme.casefold() != "basic" or not encoded or len(encoded) > 2048:
            return False
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            submitted_user, separator, submitted_password = decoded.partition(":")
        except (ValueError, UnicodeError):
            return False
        return bool(separator) and hmac.compare_digest(submitted_user, username) and hmac.compare_digest(
            submitted_password, password)

    def _configured(self) -> bool:
        username, password = _credentials()
        return bool(username) and len(password) >= 24

    def _authorized(self) -> bool:
        if not self._https_ok() or not self._configured():
            return False
        return session_token_valid(self._cookie(SESSION_COOKIE)) or self._basic_ok()

    def _same_site_write(self) -> bool:
        origin = self.headers.get("Origin")
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site not in {None, "same-origin"}:
            return False
        if origin is None:
            return True
        if origin == "null":
            # Avec « Referrer-Policy: no-referrer », Chrome envoie « Origin: null »
            # pour un formulaire : on exige alors la preuve Sec-Fetch-Site.
            return fetch_site == "same-origin"
        return urlsplit(origin).netloc.casefold() == self.headers.get("Host", "").casefold()

    # --- réponses -------------------------------------------------------
    def _auth_required(self) -> None:
        self._send_json(401, {"error": "Connexion requise.", "login": "/login"})

    def _redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self._security_headers()
        self.end_headers()

    def _session_cookie(self, value: str, max_age: int) -> str:
        secure = "; Secure" if self._secure_request() else ""
        return f"{SESSION_COOKIE}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict{secure}"

    def _send_html(self, status: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _login_page(self, status: int = 200, error: str = "") -> None:
        block = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
        self._send_html(status, _LOGIN_PAGE.format(error=block))

    # Les routes héritées sont réservées au PC ; ici elles ne sont joignables
    # qu'après authentification et uniquement pour la liste explicite.
    def _local_host(self) -> bool:
        return True

    def _same_origin_read(self) -> bool:
        return True

    def _same_origin(self) -> bool:
        return self._same_site_write()

    # --- GET ------------------------------------------------------------
    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == CHATGPT_CALLBACK and self.server.chatgpt_signin and self._https_ok():
            # Le navigateur revient d’OpenAI sans son cookie Strict. Cette page fixe
            # termine l’échange par POST sur la même origine, avec le cookie BUBUS.
            return self._send_html(200, '<!doctype html><html lang="fr"><meta charset="utf-8">'
                                   '<meta name="viewport" content="width=device-width,initial-scale=1">'
                                   '<title>Connexion ChatGPT · BUBUS</title><p id="status">Connexion en cours…</p>'
                                   '<script src="/assets/chatgpt-return.js" defer></script>'
                                   '<a href="/#chat">Revenir à la discussion</a></html>')
        if path == "/assets/chatgpt-return.js" and self.server.chatgpt_signin:
            return self._send_file(ASSETS / "chatgpt-return.js", "text/javascript; charset=utf-8")
        if path == "/healthz":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "2")
            self._security_headers()
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if path in _PUBLIC_GET_PATHS:
            return super().do_GET()
        if path == "/login":
            if not self._https_ok():
                return self._send_html(403, "HTTPS requis.")
            if self._authorized():
                return self._redirect("/")
            return self._login_page()
        if not self._authorized():
            return self._redirect("/login") if path == "/" else self._auth_required()
        if path not in _AUTHENTICATED_GET_PATHS:
            return self._send_json(404, {"error": "Cette route privée n’existe pas dans la version en ligne."})
        if path == "/":
            try:
                body = (ASSETS / "index.html").read_bytes()
            except OSError:
                return self._send_json(503, {"error": "Interface indisponible."})
            banner = ('<div class="cloud-preview-banner" role="status"><span>BUBUS EN LIGNE · '
                      'Lecture seule · Aucun ordre</span><form method="post" action="/logout">'
                      '<button type="submit">Se déconnecter</button></form></div>').encode("utf-8")
            body = body.replace(b"<body>", b'<body data-mode="cloud">' + banner, 1)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

    # --- POST -----------------------------------------------------------
    def _handle_login(self) -> None:
        if not self._https_ok():
            return self._send_html(403, "HTTPS requis.")
        if not self._configured():
            return self._login_page(503, "Le serveur n’a pas d’identifiant configuré.")
        if not self._same_site_write():
            self._drain_body(2048)
            return self._login_page(403, "Connexion refusée depuis une autre page.")
        client = self._client_key()
        if LOGIN_THROTTLE.blocked(client):
            self._drain_body(2048)
            return self._login_page(429, "Trop d’essais. Attends 15 minutes avant de réessayer.")
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length.isdecimal() or not 1 <= int(raw_length) <= 2048:
            return self._login_page(400, "Formulaire invalide.")
        try:
            form = parse_qs(self.rfile.read(int(raw_length)).decode("utf-8"), max_num_fields=4)
        except (UnicodeDecodeError, ValueError):
            return self._login_page(400, "Formulaire invalide.")
        submitted_user = (form.get("username") or [""])[0]
        submitted_password = (form.get("password") or [""])[0]
        username, password = _credentials()
        user_ok = hmac.compare_digest(submitted_user.encode("utf-8"), username.encode("utf-8"))
        password_ok = hmac.compare_digest(submitted_password.encode("utf-8"), password.encode("utf-8"))
        if not (user_ok and password_ok):
            LOGIN_THROTTLE.fail(client)
            return self._login_page(401, "Identifiant ou mot de passe incorrect.")
        LOGIN_THROTTLE.succeed(client)
        return self._redirect("/", self._session_cookie(issue_session_token(), SESSION_SECONDS))

    def _chatgpt_action(self, path):
        cookie = self._cookie(SESSION_COOKIE)
        if not self._same_site_write() or not session_token_valid(cookie):
            return self._reject_post(403, "Rouvre la discussion dans BUBUS et reconnecte-toi.")
        signin = self.server.chatgpt_signin
        if signin is None:
            return self._reject_post(503, "La connexion ChatGPT attend son activation pour BUBUS en ligne.")
        size = self.headers.get("Content-Length", "")
        if (self.headers.get_content_type() != "application/json" or not size.isdecimal()
                or len(size) > 5 or not 1 <= int(size) <= 8192):
            return self._reject_post(400, "Demande de connexion invalide.")
        try:
            data = json.loads(self.rfile.read(int(size)))
            if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
                raise ValueError
            session = hashlib.sha256(cookie.encode("ascii")).hexdigest()
            if path.endswith("/connect"):
                result = {"authorize_url": signin.start(self.server.server_port, browser_session=session)}
            elif path.endswith("/finish"):
                result = signin.finish(data, browser_session=session)
            else:
                signin.disconnect()
                result = signin.state()
        except ChatGPTSignInError as exc:
            return self._reject_post(exc.status, str(exc))
        except (ValueError, UnicodeError):
            return self._reject_post(400, "Demande de connexion invalide.")
        except OSError:
            return self._reject_post(503, "Connexion interrompue. Réessaie depuis la discussion.")
        return self._send_json(200, result)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/login":
            return self._handle_login()
        if path == "/logout":
            self._drain_body(4096)
            if not self._same_site_write():
                return self._reject_post(403, "Déconnexion refusée depuis une autre page.")
            return self._redirect("/login", self._session_cookie("", 0))
        if not self._authorized():
            self._drain_body()
            return self._auth_required()
        if path == "/api/desk/ai-connections":
            if not self._same_site_write() or not session_token_valid(self._cookie(SESSION_COOKIE)):
                return self._reject_post(403, "Ouvre cette action depuis ta session BUBUS.")
            return self._handle_connections_post()
        if path in {"/api/desk/chatgpt/connect", "/api/desk/chatgpt/finish", "/api/desk/chatgpt/disconnect"}:
            return self._chatgpt_action(path)
        if path == "/api/desk/chat":
            if not self._same_site_write():
                self._drain_body()
                return self._reject_post(403, "Origine requise.")
            return self._handle_chat_post()
        if path == "/api/desk/my-list":
            if not self._same_site_write():
                self._drain_body(1024)
                return self._reject_post(403, "Origine requise.")
            return self._handle_my_list_post()
        self._drain_body()
        self._deny_write()

    def _deny_write(self) -> None:
        if not self._authorized():
            return self._auth_required()
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        self.send_header("Content-Length", "0")
        self._security_headers()
        self.end_headers()

    def do_PUT(self) -> None:
        self._deny_write()

    def do_PATCH(self) -> None:
        self._deny_write()

    def do_DELETE(self) -> None:
        self._deny_write()


LOGIN_THROTTLE = LoginThrottle()


def main() -> int:
    if not os.environ.get("BUBUS_PREVIEW_USERNAME") or len(os.environ.get("BUBUS_PREVIEW_PASSWORD", "")) < 24:
        raise SystemExit("BUBUS en ligne exige un identifiant et un mot de passe d’au moins 24 caractères.")
    try:
        port = int(os.environ.get("PORT", "8790"))
    except ValueError:
        raise SystemExit("PORT must be a valid TCP port.") from None
    # Un « volume » Railway garde « Ma liste » entre deux mises à jour ; sans lui, /tmp est vidé.
    volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "")
    default_dir = str(Path(volume) / "bubus") if volume else "/tmp/bubus-preview"
    data_dir = Path(os.environ.get("BUBUS_PREVIEW_DATA_DIR", default_dir))
    server = make_server(port, data_dir=data_dir, bind_host="0.0.0.0",
                         handler_class=AuthenticatedCloudPreviewHandler, chatgpt_signin=hosted_chatgpt(data_dir),
                         ai_protector=_CloudTokenProtector("ai-keys/v1"))
    print("BUBUS2.0 en ligne : marchés publics et discussion IA, en lecture seule.", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
