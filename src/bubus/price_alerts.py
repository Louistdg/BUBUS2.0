"""Persistent one-shot price crossings and Android Chrome Web Push."""
import base64
import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

logger = logging.getLogger(__name__)


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def positive(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number > 0 else None


def subscription(data):
    endpoint = data["endpoint"]
    keys = data["keys"]
    url = urlsplit(endpoint)
    if (url.scheme != "https" or url.netloc != "fcm.googleapis.com"
            or not re.fullmatch(r"/fcm/send/[A-Za-z0-9_:-]{10,2048}", url.path)
            or url.query or url.fragment):
        raise ValueError("Abonnement Chrome Android invalide.")
    for key, size in (("p256dh", 87), ("auth", 22)):
        if not re.fullmatch(rf"[A-Za-z0-9_-]{{{size}}}", keys[key]):
            raise ValueError("Clés de notification invalides.")
    return {"endpoint": endpoint, "keys": {key: keys[key] for key in ("p256dh", "auth")}}


class PriceAlerts:
    def __init__(self, directory, *, quote_source, sender=None, clock=time.time):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "price-alerts.sqlite"
        self.quote_source = quote_source
        self.sender = sender or self._push
        self.clock = clock
        self.stop_event = threading.Event()
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS rules(
                id TEXT PRIMARY KEY, symbol TEXT, currency TEXT, direction TEXT,
                threshold TEXT, subscription TEXT NOT NULL, last_side INTEGER,
                last_stamp INTEGER, state TEXT NOT NULL DEFAULT 'active', test_at INTEGER
            )""")
            db.execute("CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE rules SET state='uncertain' WHERE state='sending'")
            row = db.execute("SELECT value FROM settings WHERE name='vapid'").fetchone()
            if row:
                self.private_key = serialization.load_pem_private_key(row[0].encode(), password=None)
            else:
                self.private_key = ec.generate_private_key(ec.SECP256R1())
                pem = self.private_key.private_bytes(serialization.Encoding.PEM,
                                                    serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption())
                db.execute("INSERT INTO settings VALUES ('vapid', ?)", (pem.decode(),))
        self.public_key = b64(self.private_key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))

    @contextmanager
    def _db(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def start(self):
        threading.Thread(target=self._run, name="bubus-price-alerts", daemon=True).start()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.poll()
            except (OSError, ValueError, sqlite3.Error):
                logger.warning("Vérification des alertes indisponible.")
            self.stop_event.wait(20)

    def list(self):
        with self._db() as db:
            rows = db.execute("""SELECT id, symbol, currency, direction, threshold, state
                              FROM rules WHERE test_at IS NULL ORDER BY rowid DESC""")
            return {"rules": [dict(row) for row in rows], "public_key": self.public_key}

    def remove(self, rule_id):
        with self._db() as db:
            db.execute("DELETE FROM rules WHERE id=?", (rule_id,))

    def add(self, payload, *, test=False):
        target = subscription(payload["subscription"])
        cutoff = int(self.clock()) - 3600
        if test:
            symbol, currency, direction, threshold = "TEST", "EUR", "above", Decimal(2)
            baseline, stamp = Decimal(1), int(self.clock())
        else:
            symbol, currency = payload["symbol"], payload["currency"]
            direction, threshold = payload["direction"], positive(payload["threshold"])
            if direction not in ("above", "below") or threshold is None:
                raise ValueError("Seuil ou condition invalide.")
            quote = next((q for q in self.quote_source().get("market", [])
                          if q["symbol"] == symbol and q["currency"] == currency), None)
            baseline = positive(quote["price"]) if quote else None
            stamp = quote.get("stamp") if quote else None
            if baseline is None or not isinstance(stamp, int) or stamp <= 0:
                raise ValueError("Cours indisponible pour ce seuil.")
        side = self._side(baseline, threshold, direction)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM rules WHERE test_at < ?", (cutoff,))
            if not test and db.execute("""SELECT 1 FROM rules WHERE symbol=? AND currency=?
                    AND direction=? AND threshold=? AND subscription=? AND state='active'""",
                    (symbol, currency, direction, str(threshold), json.dumps(target))).fetchone():
                return
            if test:
                exists = db.execute("SELECT 1 FROM rules WHERE test_at IS NOT NULL AND subscription=? AND state='active'",
                                    (json.dumps(target),)).fetchone()
                if exists:
                    return
            count = db.execute("SELECT count(*) FROM rules").fetchone()[0]
            if count >= 100:
                raise ValueError("Limite d’alertes atteinte.")
            db.execute("""INSERT INTO rules VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
                       (uuid4().hex, symbol, currency, direction, str(threshold), json.dumps(target),
                        side, stamp, int(self.clock()) + 25 if test else None))

    @staticmethod
    def _side(price, threshold, direction):
        return int(price >= threshold if direction == "above" else price <= threshold)

    def poll(self):
        with self._db() as db:
            rules = list(db.execute("SELECT * FROM rules WHERE state='active'"))
        for rule in rules:
            if rule["test_at"] is not None:
                if self.clock() >= rule["test_at"]:
                    self._advance(rule, Decimal(3), rule["test_at"])
        real = [rule for rule in rules if rule["test_at"] is None]
        if not real:
            return
        quotes = {(q["symbol"], q["currency"]): q for q in self.quote_source().get("market", [])}
        for rule in real:
            quote = quotes.get((rule["symbol"], rule["currency"]))
            if quote:
                self._advance(rule, positive(quote["price"]), quote.get("stamp"))

    def _advance(self, rule, price, stamp):
        if price is None or not isinstance(stamp, int) or stamp <= rule["last_stamp"]:
            return
        side = self._side(price, Decimal(rule["threshold"]), rule["direction"])
        crossed = not rule["last_side"] and side
        # Claim atomically before delivery. Never retry ambiguous deliveries.
        with self._db() as db:
            changed = db.execute("""UPDATE rules SET last_side=?, last_stamp=?, state=?
                WHERE id=? AND state='active' AND last_stamp=?""",
                (side, stamp, "sending" if crossed else "active", rule["id"], rule["last_stamp"])).rowcount
        if not (crossed and changed):
            return
        body = ("Un seuil fictif a été franchi. Ce test n’envoie qu’une notification."
                if rule["test_at"] else f'{rule["symbol"]} · seuil de {rule["threshold"]} {rule["currency"]} franchi.')
        message = {"title": "BUBUS · Test" if rule["test_at"] else "BUBUS · Alerte de prix",
                   "body": body, "tag": "bubus-price-" + rule["id"]}
        state = "sent"
        try:
            self.sender(json.loads(rule["subscription"]), message)
        except Exception:
            state = "failed"
            logger.warning("Une notification BUBUS n’a pas pu être envoyée.")
        with self._db() as db:
            db.execute("UPDATE rules SET state=? WHERE id=?", (state, rule["id"]))

    def _push(self, target, message):
        from pywebpush import webpush
        from py_vapid import Vapid
        from requests import Session

        class PushSession(Session):
            def request(self, *args, **kwargs):
                kwargs["allow_redirects"] = False
                return super().request(*args, **kwargs)

        key = Vapid(self.private_key)
        origin = "https://" + os.environ.get("RAILWAY_PUBLIC_DOMAIN", "bubus20-production.up.railway.app")
        with PushSession() as session:
            response = webpush(subscription_info=target, data=json.dumps(message), vapid_private_key=key,
                               vapid_claims={"sub": origin}, requests_session=session, ttl=120, timeout=8)
            if response.status_code not in (200, 201, 202, 204):
                raise ValueError("Notification refusée.")
