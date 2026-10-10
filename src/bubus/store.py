"""Local watchlist and activity, independent from capital authority."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,31}_[A-Z0-9]{2,12}\Z")


class DeskStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS watchlist(symbol TEXT PRIMARY KEY, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                    kind TEXT NOT NULL, message TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cryptocom_portfolio_snapshots(
                    id INTEGER PRIMARY KEY, fetched_at TEXT NOT NULL, stored_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                    authority_status TEXT NOT NULL DEFAULT 'OBSERVATION_ONLY');
                CREATE TABLE IF NOT EXISTS risk_limit_candidates(
                    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, base_currency TEXT NOT NULL,
                    capital_limit TEXT NOT NULL, max_order_notional TEXT NOT NULL,
                    max_daily_loss TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                    approval_status TEXT NOT NULL DEFAULT 'PENDING_OWNER_REVIEW');
                CREATE TABLE IF NOT EXISTS cryptocom_transaction_snapshots(
                    id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, start_time_ms INTEGER NOT NULL,
                    end_time_ms INTEGER NOT NULL, item_count INTEGER NOT NULL,
                    payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL, batch_id TEXT);
                CREATE TABLE IF NOT EXISTS cryptocom_trade_snapshots(
                    id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, start_time_ns INTEGER NOT NULL,
                    end_time_ns INTEGER NOT NULL, item_count INTEGER NOT NULL,
                    payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL, batch_id TEXT);
                CREATE TABLE IF NOT EXISTS cryptocom_balance_history_snapshots(
                    id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, timeframe TEXT NOT NULL,
                    currency TEXT NOT NULL, point_count INTEGER NOT NULL,
                    payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cryptocom_funding_history_snapshots(
                    id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, direction TEXT NOT NULL,
                    start_time_ms INTEGER NOT NULL, end_time_ms INTEGER NOT NULL, item_count INTEGER NOT NULL,
                    payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cryptocom_portfolio_valuation_candidates(
                    id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, base_currency TEXT NOT NULL,
                    candidate_snapshot_id TEXT NOT NULL, payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(cryptocom_transaction_snapshots)")}
            if "batch_id" not in columns:
                db.execute("ALTER TABLE cryptocom_transaction_snapshots ADD COLUMN batch_id TEXT")
            trade_columns = {row[1] for row in db.execute("PRAGMA table_info(cryptocom_trade_snapshots)")}
            if "batch_id" not in trade_columns:
                db.execute("ALTER TABLE cryptocom_trade_snapshots ADD COLUMN batch_id TEXT")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def watchlist(self):
        with self.connection() as db:
            return [row[0] for row in db.execute("SELECT symbol FROM watchlist ORDER BY created_at,symbol")]

    def set_watch(self, symbol: str, enabled: bool):
        if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol) or type(enabled) is not bool:
            raise ValueError("Paire ou état de favori invalide.")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT 1 FROM watchlist WHERE symbol=?", (symbol,)).fetchone() is not None
            if enabled:
                if db.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] >= 100:
                    if not db.execute("SELECT 1 FROM watchlist WHERE symbol=?", (symbol,)).fetchone():
                        raise ValueError("La liste est limitée à 100 favoris.")
                db.execute("INSERT OR IGNORE INTO watchlist VALUES(?,?)", (symbol, self.now()))
            else:
                db.execute("DELETE FROM watchlist WHERE symbol=?", (symbol,))
            if previous != enabled:
                db.execute("INSERT INTO activity(created_at,kind,message) VALUES(?,?,?)",
                           (self.now(), "WATCHLIST", f"{symbol} {'ajoutée aux' if enabled else 'retirée des'} favoris."))
        return self.watchlist()

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def record(self, kind: str, message: str):
        with self.connection() as db:
            db.execute("INSERT INTO activity(created_at,kind,message) VALUES(?,?,?)", (self.now(), kind, message))

    def activity(self):
        with self.connection() as db:
            return [dict(zip(("id", "created_at", "kind", "message"), row))
                    for row in db.execute("SELECT id,created_at,kind,message FROM activity ORDER BY id DESC LIMIT 100")]

    def record_cryptocom_portfolio(self, payload):
        if not isinstance(payload, dict) or payload.get("provider") != "Crypto.com Exchange":
            raise ValueError("Crypto.com snapshot provenance is required.")
        safe = dict(payload)
        safe.pop("credential_warning", None)
        safe.pop("credentials_persisted", None)
        safe["persistent"] = True
        safe["authority_status"] = "OBSERVATION_ONLY"
        safe["usable_for_risk"] = False
        safe["usable_for_execution"] = False
        safe["orders_enabled"] = False
        safe["source_payload_sha256"] = safe.get("payload_sha256")
        content = json.dumps(safe, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("INSERT INTO cryptocom_portfolio_snapshots(fetched_at,stored_at,payload_json,payload_sha256) VALUES(?,?,?,?)",
                (str(safe.get("fetched_at", "")), self.now(), content, digest))
            db.execute("DELETE FROM cryptocom_portfolio_snapshots WHERE id NOT IN "
                       "(SELECT id FROM cryptocom_portfolio_snapshots ORDER BY id DESC LIMIT 5000)")
        safe["snapshot_id"] = cursor.lastrowid
        safe["stored_payload_sha256"] = digest
        if "credential_warning" in payload:
            safe["credential_warning"] = payload["credential_warning"]
        if "credentials_persisted" in payload:
            safe["credentials_persisted"] = payload["credentials_persisted"]
        return safe

    def latest_cryptocom_portfolio(self):
        history = self.cryptocom_portfolio_history(limit=1)
        return history[0] if history else None

    def cryptocom_portfolio_history(self, *, limit: int = 1000):
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise ValueError("Crypto.com portfolio history limit is invalid.")
        with self.connection() as db:
            rows = db.execute("SELECT id,stored_at,payload_json,payload_sha256 FROM cryptocom_portfolio_snapshots "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            if hashlib.sha256(row[2].encode("utf-8")).hexdigest() != row[3]:
                raise ValueError("Persisted Crypto.com snapshot integrity check failed.")
            snapshot = json.loads(row[2])
            if (not isinstance(snapshot, dict) or snapshot.get("provider") != "Crypto.com Exchange"
                    or snapshot.get("persistent") is not True
                    or snapshot.get("authority_status") != "OBSERVATION_ONLY"
                    or snapshot.get("usable_for_risk") is not False
                    or snapshot.get("usable_for_execution") is not False
                    or snapshot.get("orders_enabled") is not False):
                raise ValueError("Persisted Crypto.com snapshot authority/provenance check failed.")
            snapshot["snapshot_id"] = row[0]
            snapshot["stored_at"] = row[1]
            snapshot["stored_payload_sha256"] = row[3]
            snapshot["authority_status"] = "OBSERVATION_ONLY"
            snapshot["usable_for_risk"] = False
            snapshot["usable_for_execution"] = False
            snapshot["orders_enabled"] = False
            result.append(snapshot)
        return result

    def propose_risk_limits(self, payload):
        if not isinstance(payload, dict) or set(payload) != {
                "base_currency", "capital_limit", "max_order_notional", "max_daily_loss"}:
            raise ValueError("Devise et trois limites explicites sont obligatoires.")
        currency = payload["base_currency"]
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("La devise doit être un code de trois lettres.")

        def amount(field):
            raw = payload[field]
            if not isinstance(raw, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,11})(?:[.,][0-9]{1,8})?", raw):
                raise ValueError(f"{field} doit être un montant décimal positif écrit en chiffres.")
            try:
                value = Decimal(raw.replace(",", "."))
            except InvalidOperation:
                raise ValueError(f"{field} est invalide.") from None
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{field} doit être supérieur à zéro.")
            return value

        capital = amount("capital_limit")
        max_order = amount("max_order_notional")
        max_daily_loss = amount("max_daily_loss")
        if max_order > capital or max_daily_loss > capital:
            raise ValueError("L’ordre maximal et la perte journalière doivent rester sous le capital maximal.")
        values = {"base_currency": currency, "capital_limit": format(capital, "f"),
                  "max_order_notional": format(max_order, "f"), "max_daily_loss": format(max_daily_loss, "f")}
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        created_at = self.now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("INSERT INTO risk_limit_candidates(created_at,base_currency,capital_limit,"
                "max_order_notional,max_daily_loss,payload_sha256) VALUES(?,?,?,?,?,?)",
                (created_at, currency, values["capital_limit"], values["max_order_notional"],
                 values["max_daily_loss"], digest))
        return {"candidate_id": cursor.lastrowid, "created_at": created_at, **values,
                "payload_sha256": digest, "approval_status": "PENDING_OWNER_REVIEW",
                "active": False, "orders_enabled": False}

    def latest_risk_limits(self):
        with self.connection() as db:
            row = db.execute("SELECT id,created_at,base_currency,capital_limit,max_order_notional,"
                "max_daily_loss,payload_sha256,approval_status FROM risk_limit_candidates ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        values = {"base_currency": row[2], "capital_limit": row[3],
                  "max_order_notional": row[4], "max_daily_loss": row[5]}
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":"))
        if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != row[6] or row[7] != "PENDING_OWNER_REVIEW":
            raise ValueError("Risk limit candidate integrity or status check failed.")
        return {"candidate_id": row[0], "created_at": row[1], **values, "payload_sha256": row[6],
                "approval_status": row[7], "active": False, "orders_enabled": False}

    def record_cryptocom_transactions(self, payload):
        if (not isinstance(payload, dict) or set(payload) not in ({
                "observed_at", "start_time_ms", "end_time_ms", "items", "provider_payload_sha256"}, {
                "observed_at", "start_time_ms", "end_time_ms", "items", "provider_payload_sha256", "batch"})
                or not isinstance(payload["observed_at"], str)
                or type(payload["start_time_ms"]) is not int or type(payload["end_time_ms"]) is not int
                or payload["start_time_ms"] < 0 or payload["end_time_ms"] <= payload["start_time_ms"]
                or not isinstance(payload["items"], list) or len(payload["items"]) > 100
                or not isinstance(payload["provider_payload_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", payload["provider_payload_sha256"])):
            raise ValueError("Transaction observation is malformed.")
        batch = payload.get("batch")
        batch_id = None
        if batch is not None:
            required = {"batch_id", "query_start_time_ms", "query_end_time_ms", "coverage_complete",
                        "incomplete_intervals", "request_count", "page_count"}
            if (not isinstance(batch, dict) or set(batch) != required
                    or not isinstance(batch["batch_id"], str)
                    or str(uuid.UUID(batch["batch_id"])) != batch["batch_id"]
                    or type(batch["query_start_time_ms"]) is not int
                    or type(batch["query_end_time_ms"]) is not int
                    or batch["query_start_time_ms"] < 0
                    or batch["query_end_time_ms"] <= batch["query_start_time_ms"]
                    or not batch["query_start_time_ms"] <= payload["start_time_ms"] < payload["end_time_ms"] <= batch["query_end_time_ms"]
                    or type(batch["coverage_complete"]) is not bool
                    or not isinstance(batch["incomplete_intervals"], list)
                    or len(batch["incomplete_intervals"]) > 64
                    or type(batch["request_count"]) is not int or not 1 <= batch["request_count"] <= 64
                    or type(batch["page_count"]) is not int or not 1 <= batch["page_count"] <= 16
                    or batch["coverage_complete"] != (len(batch["incomplete_intervals"]) == 0)):
                raise ValueError("Transaction batch metadata is invalid.")
            for interval in batch["incomplete_intervals"]:
                if (not isinstance(interval, list) or len(interval) != 2
                        or type(interval[0]) is not int or type(interval[1]) is not int
                        or not batch["query_start_time_ms"] <= interval[0] < interval[1] <= batch["query_end_time_ms"]):
                    raise ValueError("Transaction batch incomplete interval is invalid.")
            batch_id = batch["batch_id"]
        # Reject malformed data before SQLite persistence, not just on later reads.
        from bubus.reconciliation import validate_transaction_page
        validate_transaction_page({
            "snapshot_id": 1, "observed_at": payload["observed_at"],
            "start_time_ms": payload["start_time_ms"], "end_time_ms": payload["end_time_ms"],
            "item_count": len(payload["items"]), "items": payload["items"],
            "provider_payload_sha256": payload["provider_payload_sha256"],
            "payload_sha256": "0" * 64,
        })
        evidence = {"items": payload["items"], "provider_payload_sha256": payload["provider_payload_sha256"]}
        if batch is not None:
            evidence["batch"] = batch
        encoded = json.dumps(evidence,
            sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("INSERT INTO cryptocom_transaction_snapshots(observed_at,start_time_ms,end_time_ms,"
                "item_count,payload_json,payload_sha256,batch_id) VALUES(?,?,?,?,?,?,?)",
                (payload["observed_at"], payload["start_time_ms"], payload["end_time_ms"],
                 len(payload["items"]), encoded, digest, batch_id))
            db.execute("DELETE FROM cryptocom_transaction_snapshots WHERE id <= ?",
                (max(0, cursor.lastrowid - 50),))
        return {"snapshot_id": cursor.lastrowid, **payload, "item_count": len(payload["items"]),
                "payload_sha256": digest, "usable_for_risk": False, "orders_enabled": False}

    def latest_cryptocom_transactions(self):
        pages = self.cryptocom_transaction_history(limit=50)
        if not pages:
            return None
        latest = pages[0]
        batch_id = latest.get("batch_id")
        selected = [page for page in pages if page.get("batch_id") == batch_id] if batch_id else [latest]
        selected.sort(key=lambda page: (page["start_time_ms"], page["end_time_ms"]))
        batch = latest.get("batch")
        if batch is not None and (len(selected) != batch["page_count"]
                or any(page.get("batch") != batch or page["observed_at"] != latest["observed_at"]
                       for page in selected)):
            raise ValueError("Crypto.com transaction batch is incomplete or internally inconsistent.")
        items = [item for page in selected for item in page["items"]]
        page_digests = [page["payload_sha256"] for page in selected]
        aggregate_payload = json.dumps({"batch_id": batch_id, "page_sha256": page_digests,
                                        "item_count": len(items)},
                                       sort_keys=True, separators=(",", ":"))
        aggregate_sha = hashlib.sha256(aggregate_payload.encode("utf-8")).hexdigest()
        return {
            "snapshot_id": latest["snapshot_id"],
            "observed_at": latest["observed_at"],
            "start_time_ms": batch["query_start_time_ms"] if batch else selected[0]["start_time_ms"],
            "end_time_ms": batch["query_end_time_ms"] if batch else selected[-1]["end_time_ms"],
            "items": items, "item_count": len(items),
            "payload_sha256": page_digests[0] if len(page_digests) == 1 else aggregate_sha,
            "provider_payload_sha256": (selected[0]["provider_payload_sha256"] if len(selected) == 1
                else aggregate_sha),
            "page_count": len(selected), "coverage_complete": batch["coverage_complete"] if batch else False,
            "incomplete_intervals": batch["incomplete_intervals"] if batch else [],
            "page_status": ("PAGINATED_COMPLETE" if batch and batch["coverage_complete"] else
                "PAGINATION_INCOMPLETE" if batch else
                "PAGE_LIMIT_REACHED" if latest["item_count"] == 100 else "ONE_PAGE_READ"),
            "pages": [{"snapshot_id": page["snapshot_id"], "start_time_ms": page["start_time_ms"],
                "end_time_ms": page["end_time_ms"], "item_count": page["item_count"],
                "payload_sha256": page["payload_sha256"]} for page in selected],
            "batch_id": batch_id,
            "usable_for_risk": False, "orders_enabled": False,
        }

    def cryptocom_transaction_history(self, *, limit: int = 50):
        """Return all retained verified pages, newest first, without pretending pagination is complete."""
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Transaction history page limit is invalid.")
        with self.connection() as db:
            rows = db.execute(
                "SELECT id,observed_at,start_time_ms,end_time_ms,item_count,payload_json,payload_sha256,batch_id "
                "FROM cryptocom_transaction_snapshots ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        result = []
        for row in rows:
            if hashlib.sha256(row[5].encode("utf-8")).hexdigest() != row[6]:
                raise ValueError("Crypto.com transaction page integrity check failed.")
            try:
                evidence = json.loads(row[5])
            except (TypeError, ValueError):
                raise ValueError("Crypto.com transaction page JSON is invalid.") from None
            if (not isinstance(evidence, dict) or set(evidence) not in ({"items", "provider_payload_sha256"},
                    {"items", "provider_payload_sha256", "batch"})
                    or type(row[0]) is not int or type(row[2]) is not int or type(row[3]) is not int
                    or type(row[4]) is not int or not 0 <= row[4] <= 100
                    or not isinstance(evidence["items"], list) or len(evidence["items"]) != row[4]
                    or not isinstance(evidence["provider_payload_sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", evidence["provider_payload_sha256"])):
                raise ValueError("Crypto.com transaction page metadata is invalid.")
            if (("batch" in evidence and (not isinstance(evidence["batch"], dict)
                    or evidence["batch"].get("batch_id") != row[7]))
                    or ("batch" not in evidence and row[7] is not None)):
                raise ValueError("Crypto.com transaction page batch identity is inconsistent.")
            snapshot = {"snapshot_id": row[0], "observed_at": row[1],
                "start_time_ms": row[2], "end_time_ms": row[3],
                "item_count": row[4], "items": evidence["items"],
                "provider_payload_sha256": evidence["provider_payload_sha256"],
                "payload_sha256": row[6], "batch_id": row[7],
                "usable_for_risk": False, "orders_enabled": False}
            if "batch" in evidence:
                snapshot["batch"] = evidence["batch"]
            from bubus.reconciliation import validate_transaction_page
            validate_transaction_page(snapshot)
            result.append(snapshot)
        return result

    @staticmethod
    def _validate_cryptocom_trade_payload(payload):
        fields = {"observed_at", "start_time_ns", "end_time_ns", "items", "provider_payload_sha256"}
        if (not isinstance(payload, dict) or set(payload) not in (fields, fields | {"batch"})
                or not isinstance(payload["observed_at"], str)
                or type(payload["start_time_ns"]) is not int or type(payload["end_time_ns"]) is not int
                or payload["start_time_ns"] < 0 or payload["end_time_ns"] <= payload["start_time_ns"]
                or payload["end_time_ns"] - payload["start_time_ns"] > 7 * 86_400_000_000_000
                or not isinstance(payload["items"], list) or len(payload["items"]) > 100
                or not isinstance(payload["provider_payload_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", payload["provider_payload_sha256"])):
            raise ValueError("Crypto.com trade observation is malformed.")
        batch = payload.get("batch")
        if "batch" in payload and batch is None:
            raise ValueError("Crypto.com trade batch metadata is missing.")
        if batch is not None:
            required_batch = {"batch_id", "query_start_time_ns", "query_end_time_ns", "coverage_complete",
                              "incomplete_intervals", "request_count", "page_count"}
            try:
                batch_id = str(uuid.UUID(batch.get("batch_id"))) if isinstance(batch, dict) else None
            except (AttributeError, TypeError, ValueError):
                batch_id = None
            if (not isinstance(batch, dict) or set(batch) != required_batch or batch_id != batch.get("batch_id")
                    or type(batch["query_start_time_ns"]) is not int
                    or type(batch["query_end_time_ns"]) is not int
                    or not batch["query_start_time_ns"] <= payload["start_time_ns"]
                    < payload["end_time_ns"] <= batch["query_end_time_ns"]
                    or type(batch["coverage_complete"]) is not bool
                    or not isinstance(batch["incomplete_intervals"], list)
                    or len(batch["incomplete_intervals"]) > 64
                    or type(batch["request_count"]) is not int or not 1 <= batch["request_count"] <= 64
                    or type(batch["page_count"]) is not int or not 1 <= batch["page_count"] <= 64
                    or batch["coverage_complete"] != (len(batch["incomplete_intervals"]) == 0)):
                raise ValueError("Crypto.com trade batch metadata is invalid.")
            for interval in batch["incomplete_intervals"]:
                if (not isinstance(interval, list) or len(interval) != 2
                        or type(interval[0]) is not int or type(interval[1]) is not int
                        or not batch["query_start_time_ns"] <= interval[0] < interval[1]
                        <= batch["query_end_time_ns"]):
                    raise ValueError("Crypto.com trade batch interval is invalid.")
        observed = datetime.fromisoformat(payload["observed_at"].replace("Z", "+00:00"))
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ValueError("Crypto.com trade observation timestamp needs a timezone.")
        required = {"trade_id", "order_id", "trade_match_id", "client_oid", "instrument_name", "side",
                    "taker_side", "quantity", "price", "fees", "fee_currency", "occurred_at",
                    "timestamp_ns", "source", "payload_sha256"}
        seen = set()
        for item in payload["items"]:
            if not isinstance(item, dict) or set(item) != required:
                raise ValueError("Crypto.com trade row fields are malformed.")
            if (not isinstance(item["trade_id"], str) or not item["trade_id"].isdecimal()
                    or not isinstance(item["order_id"], str) or not item["order_id"].isdecimal()
                    or not isinstance(item["trade_match_id"], str) or not item["trade_match_id"].isdecimal()
                    or not isinstance(item["instrument_name"], str)
                    or re.fullmatch(r"[A-Z0-9._-]{2,64}", item["instrument_name"]) is None
                    or item["side"] not in {"BUY", "SELL"}
                    or item["taker_side"] not in {None, "MAKER", "TAKER"}
                    or not isinstance(item["fee_currency"], str)
                    or re.fullmatch(r"[A-Z0-9]{2,16}", item["fee_currency"]) is None
                    or item["source"] != "private/get-trades"
                    or item["payload_sha256"] != payload["provider_payload_sha256"]
                    or not isinstance(item["timestamp_ns"], str) or not item["timestamp_ns"].isdecimal()
                    or not payload["start_time_ns"] <= int(item["timestamp_ns"]) < payload["end_time_ns"]):
                raise ValueError("Crypto.com trade identity or provenance is invalid.")
            identity = (item["instrument_name"], item["trade_id"])
            if identity in seen:
                raise ValueError("Crypto.com trade identity is duplicated.")
            client_oid = item["client_oid"]
            if client_oid is not None and (not isinstance(client_oid, str)
                    or re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", client_oid) is None):
                raise ValueError("Crypto.com trade client order reference is invalid.")
            occurred = datetime.fromisoformat(item["occurred_at"].replace("Z", "+00:00"))
            if occurred.tzinfo is None or occurred.utcoffset() is None:
                raise ValueError("Crypto.com trade timestamp needs a timezone.")
            if any(not isinstance(item[key], str) for key in ("quantity", "price", "fees")):
                raise ValueError("Crypto.com trade amounts must be decimal strings.")
            try:
                qty, price, fees = (Decimal(item[key]) for key in ("quantity", "price", "fees"))
            except InvalidOperation:
                raise ValueError("Crypto.com trade amounts are invalid.") from None
            if not qty.is_finite() or qty <= 0 or not price.is_finite() or price <= 0 or not fees.is_finite():
                raise ValueError("Crypto.com trade amounts are invalid.")
            seen.add(identity)

    def record_cryptocom_trades(self, payload):
        self._validate_cryptocom_trade_payload(payload)
        evidence = {"items": payload["items"], "provider_payload_sha256": payload["provider_payload_sha256"]}
        if "batch" in payload:
            evidence["batch"] = payload["batch"]
        encoded = json.dumps(evidence,
            sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("INSERT INTO cryptocom_trade_snapshots(observed_at,start_time_ns,end_time_ns,"
                "item_count,payload_json,payload_sha256,batch_id) VALUES(?,?,?,?,?,?,?)",
                (payload["observed_at"], payload["start_time_ns"], payload["end_time_ns"],
                 len(payload["items"]), encoded, digest,
                 payload["batch"]["batch_id"] if "batch" in payload else None))
            db.execute("DELETE FROM cryptocom_trade_snapshots WHERE id <= ?", (max(0, cursor.lastrowid - 50),))
        return {"snapshot_id": cursor.lastrowid, **payload, "item_count": len(payload["items"]),
            "payload_sha256": digest, "usable_for_risk": False, "orders_enabled": False}

    def cryptocom_trade_history(self, *, limit: int = 50):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Crypto.com trade-history page limit is invalid.")
        with self.connection() as db:
            rows = db.execute("SELECT id,observed_at,start_time_ns,end_time_ns,item_count,payload_json,payload_sha256,batch_id "
                "FROM cryptocom_trade_snapshots ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            if hashlib.sha256(row[5].encode("utf-8")).hexdigest() != row[6]:
                raise ValueError("Crypto.com trade history integrity check failed.")
            try:
                evidence = json.loads(row[5])
            except (TypeError, ValueError):
                raise ValueError("Crypto.com trade history JSON is invalid.") from None
            if (not isinstance(evidence, dict) or set(evidence) not in ({"items", "provider_payload_sha256"},
                    {"items", "provider_payload_sha256", "batch"})
                    or type(row[0]) is not int or type(row[2]) is not int or type(row[3]) is not int
                    or type(row[4]) is not int or not 0 <= row[4] <= 100
                    or not isinstance(evidence["items"], list) or len(evidence["items"]) != row[4]):
                raise ValueError("Crypto.com trade history item count is invalid.")
            if (("batch" in evidence and (not isinstance(evidence["batch"], dict)
                    or evidence["batch"].get("batch_id") != row[7]))
                    or ("batch" not in evidence and row[7] is not None)):
                raise ValueError("Crypto.com trade history batch identity is inconsistent.")
            snapshot = {"observed_at": row[1], "start_time_ns": row[2], "end_time_ns": row[3],
                "items": evidence["items"], "provider_payload_sha256": evidence["provider_payload_sha256"]}
            if "batch" in evidence:
                snapshot["batch"] = evidence["batch"]
            self._validate_cryptocom_trade_payload(snapshot)
            if row[7] is not None:
                snapshot["batch_id"] = row[7]
            result.append({"snapshot_id": row[0], **snapshot, "item_count": row[4], "payload_sha256": row[6],
                "page_status": "PAGE_LIMIT_REACHED" if row[4] == 100 else "ONE_PAGE_READ",
                "usable_for_risk": False, "orders_enabled": False})
        return result

    def latest_cryptocom_trades(self):
        history = self.cryptocom_trade_history(limit=50)
        if not history:
            return None
        latest = history[0]
        batch = latest.get("batch")
        if batch is None:
            return latest
        pages = [page for page in history if page.get("batch_id") == latest["batch_id"]]
        items = [item for page in pages for item in page["items"]]
        items.sort(key=lambda item: int(item["timestamp_ns"]))
        persistence_complete = len(pages) == batch["page_count"]
        coverage_complete = batch["coverage_complete"] and persistence_complete
        return {"observed_at": latest["observed_at"], "items": items, "item_count": len(items),
            "page_count": len(pages), "expected_page_count": batch["page_count"],
            "persistence_complete": persistence_complete,
            "page_status": "PAGINATED_COMPLETE" if coverage_complete else "PAGINATED_INCOMPLETE",
            "coverage_complete": coverage_complete,
            "incomplete_intervals": batch["incomplete_intervals"], "request_count": batch["request_count"],
            "batch_id": latest["batch_id"], "usable_for_risk": False, "orders_enabled": False,
            "payload_sha256": hashlib.sha256("".join(page["payload_sha256"] for page in pages)
                .encode("ascii")).hexdigest()}

    def record_cryptocom_funding_history(self, payload):
        required = {"observed_at", "direction", "start_time_ms", "end_time_ms", "pages", "complete",
                    "next_page", "request_count"}
        if (not isinstance(payload, dict) or set(payload) != required
                or payload["direction"] not in {"DEPOSIT", "WITHDRAWAL"}
                or not isinstance(payload["observed_at"], str)
                or type(payload["start_time_ms"]) is not int or type(payload["end_time_ms"]) is not int
                or payload["start_time_ms"] < 0 or payload["end_time_ms"] <= payload["start_time_ms"]
                or type(payload["complete"]) is not bool
                or (payload["next_page"] is not None and (type(payload["next_page"]) is not int or payload["next_page"] < 1))
                or type(payload["request_count"]) is not int or not 1 <= payload["request_count"] <= 64
                or not isinstance(payload["pages"], list) or not 1 <= len(payload["pages"]) <= 64):
            raise ValueError("Funding-history observation is malformed.")
        observed = datetime.fromisoformat(payload["observed_at"].replace("Z", "+00:00"))
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ValueError("Funding-history observation time must include a timezone.")
        items = []
        page_numbers = set()
        seen_ids = set()
        for page in payload["pages"]:
            if (not isinstance(page, dict) or set(page) != {"page", "page_size", "items", "payload_sha256"}
                    or type(page["page"]) is not int or not 0 <= page["page"] < 64
                    or page["page"] in page_numbers or type(page["page_size"]) is not int
                    or not 1 <= page["page_size"] <= 200 or not isinstance(page["items"], list)
                    or len(page["items"]) > page["page_size"]
                    or not isinstance(page["payload_sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", page["payload_sha256"])):
                raise ValueError("Funding-history page is malformed.")
            page_numbers.add(page["page"])
            for item in page["items"]:
                fields = {"direction", "transfer_id", "currency", "amount", "fee", "status",
                          "created_at", "updated_at", "source", "payload_sha256"}
                if (not isinstance(item, dict) or set(item) != fields or item["direction"] != payload["direction"]
                        or not isinstance(item["transfer_id"], str)
                        or re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", item["transfer_id"]) is None
                        or item["transfer_id"] in seen_ids or not isinstance(item["currency"], str)
                        or re.fullmatch(r"[A-Z0-9]{2,16}", item["currency"]) is None
                        or not isinstance(item["status"], str) or not item["status"].isdigit()
                        or item["source"] != ("private/get-deposit-history" if payload["direction"] == "DEPOSIT"
                                               else "private/get-withdrawal-history")
                        or not isinstance(item["payload_sha256"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", item["payload_sha256"])):
                    raise ValueError("Funding transfer identity or provenance is invalid.")
                try:
                    amount, fee = Decimal(item["amount"]), Decimal(item["fee"])
                    created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
                    updated = datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00"))
                except (InvalidOperation, TypeError, ValueError, AttributeError):
                    raise ValueError("Funding transfer values are invalid.") from None
                if (not amount.is_finite() or amount <= 0 or not fee.is_finite() or fee < 0
                        or created.tzinfo is None or updated.tzinfo is None
                        or created.timestamp() * 1000 < payload["start_time_ms"]
                        or created.timestamp() * 1000 >= payload["end_time_ms"] or updated < created):
                    raise ValueError("Funding transfer values or timestamps are invalid.")
                seen_ids.add(item["transfer_id"])
                items.append(item)
        if (page_numbers != set(range(len(page_numbers))) or payload["request_count"] != len(payload["pages"])
                or payload["complete"] != (payload["next_page"] is None)):
            raise ValueError("Funding-history pagination evidence is inconsistent.")
        evidence = {key: payload[key] for key in ("direction", "start_time_ms", "end_time_ms", "pages",
                                                   "complete", "next_page", "request_count")}
        encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("INSERT INTO cryptocom_funding_history_snapshots(observed_at,direction,start_time_ms,"
                "end_time_ms,item_count,payload_json,payload_sha256) VALUES(?,?,?,?,?,?,?)",
                (payload["observed_at"], payload["direction"], payload["start_time_ms"], payload["end_time_ms"],
                 len(items), encoded, digest))
            db.execute("DELETE FROM cryptocom_funding_history_snapshots WHERE id <= ?",
                (max(0, cursor.lastrowid - 100),))
        return {"snapshot_id": cursor.lastrowid, "observed_at": payload["observed_at"], **evidence,
                "items": items, "item_count": len(items), "payload_sha256": digest,
                "authority_status": "OBSERVATION_ONLY", "usable_for_risk": False, "orders_enabled": False}

    def latest_cryptocom_funding_history(self, direction=None):
        if direction is not None and direction not in {"DEPOSIT", "WITHDRAWAL"}:
            raise ValueError("Funding-history direction is invalid.")
        history = self.cryptocom_funding_history(limit=100)
        return next((item for item in history if direction is None or item["direction"] == direction), None)

    def cryptocom_funding_history(self, *, limit: int = 100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Funding-history limit is invalid.")
        with self.connection() as db:
            rows = db.execute("SELECT id,observed_at,direction,start_time_ms,end_time_ms,item_count,payload_json,payload_sha256 "
                "FROM cryptocom_funding_history_snapshots ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            if hashlib.sha256(row[6].encode("utf-8")).hexdigest() != row[7]:
                raise ValueError("Crypto.com funding-history integrity check failed.")
            try:
                evidence = json.loads(row[6])
                if (not isinstance(evidence, dict)
                        or set(evidence) != {"direction", "start_time_ms", "end_time_ms", "pages", "complete", "next_page", "request_count"}
                        or evidence["direction"] != row[2] or evidence["start_time_ms"] != row[3]
                        or evidence["end_time_ms"] != row[4] or not isinstance(evidence["pages"], list)
                        or type(evidence["complete"]) is not bool or type(evidence["request_count"]) is not int
                        or evidence["request_count"] != len(evidence["pages"])
                        or evidence["complete"] != (evidence["next_page"] is None)):
                    raise ValueError
                items = []
                for page in evidence["pages"]:
                    if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                        raise ValueError
                    items.extend(page["items"])
                if len(items) != row[5]:
                    raise ValueError
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                raise ValueError("Crypto.com funding-history evidence is malformed.") from None
            result.append({"snapshot_id": row[0], "observed_at": row[1], "direction": row[2],
                "start_time_ms": row[3], "end_time_ms": row[4], "item_count": row[5],
                "items": items, **{key: evidence[key] for key in ("pages", "complete", "next_page", "request_count")},
                "payload_sha256": row[7], "authority_status": "OBSERVATION_ONLY",
                "usable_for_risk": False, "orders_enabled": False})
        return result

    def record_cryptocom_balance_history(self, payload):
        if (not isinstance(payload, dict) or set(payload) != {
                "observed_at", "timeframe", "currency", "points", "source", "provider_payload_sha256"}
                or not isinstance(payload["observed_at"], str)
                or payload["timeframe"] not in {"H1", "D1"}
                or not isinstance(payload["currency"], str) or not re.fullmatch(r"[A-Z0-9]{2,16}", payload["currency"])
                or not isinstance(payload["points"], list) or len(payload["points"]) > 120
                or payload["source"] != "private/user-balance-history"
                or not isinstance(payload["provider_payload_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", payload["provider_payload_sha256"])):
            raise ValueError("Balance-history observation is malformed.")
        parsed_observed = datetime.fromisoformat(payload["observed_at"].replace("Z", "+00:00"))
        if parsed_observed.tzinfo is None or parsed_observed.utcoffset() is None:
            raise ValueError("Balance-history timestamp must include a timezone.")
        previous_timestamp = None
        for point in payload["points"]:
            if (not isinstance(point, dict) or set(point) != {"timestamp", "total_cash"}
                    or not isinstance(point["timestamp"], str) or not isinstance(point["total_cash"], str)):
                raise ValueError("Balance-history point is malformed.")
            instant = datetime.fromisoformat(point["timestamp"].replace("Z", "+00:00"))
            amount = Decimal(point["total_cash"])
            if instant.tzinfo is None or instant.utcoffset() is None or not amount.is_finite():
                raise ValueError("Balance-history point values are invalid.")
            instant = instant.astimezone(timezone.utc)
            if previous_timestamp is not None and instant <= previous_timestamp:
                raise ValueError("Balance-history points must be strictly ordered.")
            previous_timestamp = instant
        encoded = json.dumps({"points": payload["points"], "source": payload["source"],
            "provider_payload_sha256": payload["provider_payload_sha256"]},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("INSERT INTO cryptocom_balance_history_snapshots(observed_at,timeframe,currency,point_count,"
                "payload_json,payload_sha256) VALUES(?,?,?,?,?,?)",
                (payload["observed_at"], payload["timeframe"], payload["currency"], len(payload["points"]), encoded, digest))
            db.execute("DELETE FROM cryptocom_balance_history_snapshots WHERE id <= ?",
                (max(0, cursor.lastrowid - 120),))
        return {"snapshot_id": cursor.lastrowid, **payload, "point_count": len(payload["points"]),
            "payload_sha256": digest, "warning": "Historique de cash seulement; Crypto.com signale des écarts temporaires possibles.",
            "usable_for_risk": False, "orders_enabled": False}

    def latest_cryptocom_balance_history(self):
        with self.connection() as db:
            row = db.execute("SELECT id,observed_at,timeframe,currency,point_count,payload_json,payload_sha256 "
                "FROM cryptocom_balance_history_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        if hashlib.sha256(row[5].encode("utf-8")).hexdigest() != row[6]:
            raise ValueError("Crypto.com balance-history integrity check failed.")
        evidence = json.loads(row[5])
        if (not isinstance(evidence, dict) or set(evidence) != {"points", "source", "provider_payload_sha256"}
                or not isinstance(evidence["points"], list) or len(evidence["points"]) != row[4]
                or evidence["source"] != "private/user-balance-history"
                or not isinstance(evidence["provider_payload_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", evidence["provider_payload_sha256"])):
            raise ValueError("Crypto.com balance-history count or provenance check failed.")
        return {"snapshot_id": row[0], "observed_at": row[1], "timeframe": row[2], "currency": row[3],
            "points": evidence["points"], "source": evidence["source"],
            "provider_payload_sha256": evidence["provider_payload_sha256"], "point_count": row[4],
            "payload_sha256": row[6], "warning": "Historique de cash seulement; Crypto.com signale des écarts temporaires possibles.",
            "usable_for_risk": False, "orders_enabled": False}

    @staticmethod
    def _validate_cryptocom_valuation_candidate(payload):
        """Check every value before writing and again when reading local observations."""
        required = {"snapshot_id", "as_of", "base_currency", "total_equity", "cash_value",
                    "spot_assets_value", "held_asset_values", "order_reservations", "market_snapshot_ids",
                    "source_payload_sha256", "authority_status", "usable_for_risk",
                    "usable_for_execution", "orders_enabled"}
        if (not isinstance(payload, dict) or set(payload) != required
                or not isinstance(payload["snapshot_id"], str)
                or not re.fullmatch(r"cryptocom-spot-candidate:[0-9a-f]{64}", payload["snapshot_id"])
                or not isinstance(payload["as_of"], str)
                or not isinstance(payload["base_currency"], str) or not re.fullmatch(r"[A-Z]{3}", payload["base_currency"])
                or payload["authority_status"] != "BROKER_SPOT_PORTFOLIO_CANDIDATE_ONLY"
                or payload["usable_for_risk"] is not False or payload["usable_for_execution"] is not False
                or payload["orders_enabled"] is not False):
            raise ValueError("Portfolio valuation candidate or authority flags are invalid.")
        instant = datetime.fromisoformat(payload["as_of"].replace("Z", "+00:00"))
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("Portfolio valuation timestamp must include a timezone.")
        def amount_text(value):
            if not isinstance(value, str):
                raise ValueError("Portfolio valuation amounts must be decimal strings.")
            try:
                amount = Decimal(value)
            except InvalidOperation:
                raise ValueError("Portfolio valuation amounts must be valid decimals.") from None
            if not amount.is_finite() or amount < 0:
                raise ValueError("Portfolio valuation amounts must be finite and nonnegative.")
            return amount

        amounts = {}
        for key in ("total_equity", "cash_value", "spot_assets_value"):
            amounts[key] = amount_text(payload[key])
        if amounts["total_equity"] != amounts["cash_value"] + amounts["spot_assets_value"]:
            raise ValueError("Portfolio valuation components do not reconcile to total equity.")
        for key in ("market_snapshot_ids", "source_payload_sha256", "held_asset_values", "order_reservations"):
            if not isinstance(payload[key], list) or len(payload[key]) > 500:
                raise ValueError("Portfolio valuation evidence list is invalid.")
        if (any(not isinstance(value, str) or not value for value in payload["market_snapshot_ids"])
                or len(set(payload["market_snapshot_ids"])) != len(payload["market_snapshot_ids"])):
            raise ValueError("Portfolio market evidence references are invalid.")
        if (not payload["source_payload_sha256"]
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                       for value in payload["source_payload_sha256"])):
            raise ValueError("Portfolio source evidence hashes are invalid.")
        held_total = Decimal("0")
        held_cash = Decimal("0")
        for collection, identity in (("held_asset_values", "asset"), ("order_reservations", "order_id")):
            seen = set()
            for item in payload[collection]:
                if not isinstance(item, dict) or set(item) != {identity, "value"}:
                    raise ValueError("Portfolio valuation breakdown is invalid.")
                identifier = item[identity]
                if (not isinstance(identifier, str) or len(identifier) > 128 or not identifier
                        or identifier in seen):
                    raise ValueError("Portfolio valuation breakdown identity is invalid.")
                value = amount_text(item["value"])
                if collection == "held_asset_values":
                    if re.fullmatch(r"[A-Z0-9]{2,16}", identifier) is None:
                        raise ValueError("Portfolio held asset code is invalid.")
                    held_total += value
                    if identifier == payload["base_currency"]:
                        held_cash = value
                seen.add(identifier)
        if held_total != amounts["total_equity"] or held_cash != amounts["cash_value"]:
            raise ValueError("Portfolio held assets do not reconcile to the candidate totals.")

    def record_cryptocom_valuation_candidate(self, payload):
        self._validate_cryptocom_valuation_candidate(payload)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("INSERT INTO cryptocom_portfolio_valuation_candidates(observed_at,base_currency,"
                "candidate_snapshot_id,payload_json,payload_sha256) VALUES(?,?,?,?,?)",
                (payload["as_of"], payload["base_currency"], payload["snapshot_id"], encoded, digest))
            db.execute("DELETE FROM cryptocom_portfolio_valuation_candidates WHERE id <= ?",
                (max(0, cursor.lastrowid - 5000),))
        return {"valuation_id": cursor.lastrowid, **payload, "persistent": True,
            "stored_payload_sha256": digest, "authority_status": "BROKER_SPOT_PORTFOLIO_CANDIDATE_ONLY",
            "usable_for_risk": False, "usable_for_execution": False, "orders_enabled": False}

    def cryptocom_valuation_history(self, *, limit: int = 100):
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise ValueError("Portfolio valuation history limit is invalid.")
        with self.connection() as db:
            rows = db.execute("SELECT id,observed_at,base_currency,candidate_snapshot_id,payload_json,payload_sha256 "
                "FROM cryptocom_portfolio_valuation_candidates ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            if hashlib.sha256(row[4].encode("utf-8")).hexdigest() != row[5]:
                raise ValueError("Portfolio valuation history integrity check failed.")
            payload = json.loads(row[4])
            if (not isinstance(payload, dict) or payload.get("snapshot_id") != row[3]
                    or payload.get("as_of") != row[1] or payload.get("base_currency") != row[2]
                    or payload.get("authority_status") != "BROKER_SPOT_PORTFOLIO_CANDIDATE_ONLY"
                    or payload.get("usable_for_risk") is not False
                    or payload.get("usable_for_execution") is not False or payload.get("orders_enabled") is not False):
                raise ValueError("Portfolio valuation history metadata or authority flags are invalid.")
            self._validate_cryptocom_valuation_candidate(payload)
            result.append({"valuation_id": row[0], **payload, "persistent": True,
                "stored_payload_sha256": row[5], "usable_for_risk": False,
                "usable_for_execution": False, "orders_enabled": False})
        return result
