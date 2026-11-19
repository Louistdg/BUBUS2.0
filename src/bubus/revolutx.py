"""Lecture du portefeuille Revolut X par requête signée."""
import base64
import ctypes
import datetime
import http.client
import json
import os
import re

import time
from ctypes import wintypes
from pathlib import Path


HOST = "revx.revolut.com"
PATH = "/api/1.0/balances"
TARGET = "BUBUS_REVOLUTX_API_KEY"


class DiagnosticError(Exception):
    pass


def read_bubus_key():
    class Credential(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                    ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.c_void_p), ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    api = ctypes.WinDLL("advapi32", use_last_error=True)
    pointer = ctypes.POINTER(Credential)()
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(type(pointer))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    if not api.CredReadW(TARGET, 1, 0, ctypes.byref(pointer)):
        raise DiagnosticError("Impossible de lire BUBUS_REVOLUTX_API_KEY. Utilise le même compte Windows que pour cmdkey.")
    try:
        size = pointer.contents.CredentialBlobSize
        if not 0 < size <= 2048:
            raise DiagnosticError("L’entrée du coffre est vide ou invalide.")
        raw = ctypes.string_at(pointer.contents.CredentialBlob, size)
        for codec in ("utf-16-le", "ascii"):
            try:
                value = raw.decode(codec).rstrip("\0").strip()
            except UnicodeError:
                continue
            if re.fullmatch(r"[A-Za-z0-9]{64}", value):
                return value
        raise DiagnosticError("L’entrée du coffre ne contient pas la clé API Revolut X à 64 caractères.")
    finally:
        api.CredFree(pointer)


def _request(api_key, signer, path, query=''):
    stamp = str(time.time_ns() // 1_000_000)
    signature = signer.sign((stamp + "GET" + path + query).encode("ascii"))
    conn = http.client.HTTPSConnection(HOST, timeout=20)
    try:
        conn.request("GET", path + ("?" + query if query else ""), headers={
            "Accept": "application/json", "X-Revx-API-Key": api_key, "X-Revx-Timestamp": stamp,
            "X-Revx-Signature": base64.b64encode(signature).decode("ascii")})
        response = conn.getresponse()
        if response.status != 200:
            advice = {401: "Vérifie la paire de clés et l’heure du PC.",
                      403: "Vérifie la permission de lecture et les adresses IP autorisées.",
                      429: "Attends un moment avant de réessayer."}
            raise DiagnosticError(f"Revolut X a répondu HTTP {response.status}. " + advice.get(response.status, "Réessaie plus tard."))
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise DiagnosticError("Réponse trop grande pour ce diagnostic.")
        try:
            balances = json.loads(body)
        except (ValueError, UnicodeError):
            raise DiagnosticError("Réponse inattendue de Revolut X.") from None
        return balances
    finally:
        conn.close()


def _activity(token, signer, kind):
    from urllib.parse import urlencode
    rows = []
    cursor = None
    route = "/api/1.0/transactions" if kind == "transactions" else "/api/1.0/orders/" + kind
    fields = ("type", "status", "processed_date", "symbol", "side", "state", "order_state", "order_type", "created_date",
              "updated_date", "price", "quantity", "filled_quantity", "average_fill_price")
    try:
        for _ in range(10):
            query = urlencode({"limit": 100, **({"cursor": cursor} if cursor else {})})
            page = _request(token, signer, route, query)
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise DiagnosticError("Format des ordres inattendu.")
            for row in page["data"]:
                if isinstance(row, dict):
                    entry = {key: row[key] for key in fields if key in row}
                    for side in ("source", "destination"):
                        leg = row.get(side)
                        if isinstance(leg, dict):
                            entry[side] = f"{leg.get('amount', '?')} {leg.get('currency', '')}"
                    rows.append(entry)
            cursor = (page.get("metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return {"rows": rows, "partial": bool(cursor)}
    except (DiagnosticError, OSError, ValueError, http.client.HTTPException):
        return {"rows": rows, "partial": True, "error": "Lecture des opérations incomplète."}


def snapshot():
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    try:
        token = os.environ.get("BUBUS_REVOLUTX_API_KEY")
        pem = os.environ.get("BUBUS_REVOLUTX_PRIVATE_PEM")
        if token or pem:
            if not token or not pem:
                raise DiagnosticError("La configuration Revolut X est incomplète.")
            raw = pem.encode()
        elif os.name == "nt":
            token = read_bubus_key()
            raw = (Path.home() / ".bubus/revolutx/revolutx_private.pem").read_bytes()
        else:
            raise DiagnosticError("Les deux clés de lecture Revolut X doivent être ajoutées aux variables Railway.")
        signer = load_pem_private_key(raw, password=None)
        if not isinstance(signer, Ed25519PrivateKey):
            raise DiagnosticError("La clé de signature doit être au format Ed25519.")
        payload = _request(token, signer, PATH)
        if not isinstance(payload, list) or not all(isinstance(row, dict) and "currency" in row for row in payload):
            raise DiagnosticError("Format des soldes inattendu.")
        balances = [{key: str(row.get(key, "—")) for key in ("currency", "available", "total")} for row in payload]
        return {"connected": True, "balances": balances,
                "transactions": _activity(token, signer, "transactions"),
                "active": _activity(token, signer, "active"), "historical": _activity(token, signer, "historical"),
                "observed": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    except DiagnosticError as exc:
        return {"connected": False, "reason": str(exc)}
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        return {"connected": False, "reason": "Lecture impossible. Vérifie les clés et la connexion à Revolut X."}
