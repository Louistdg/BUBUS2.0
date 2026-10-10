"""Local BUBUS2.0 desk. Inherited account routes remain read-only."""
from __future__ import annotations

import argparse
import copy
import json
import sqlite3
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from bubus import PRODUCT_NAME, __version__
from bubus.market import CandleSource
from bubus.live_signal import LiveSignalError, observe_cryptocom_breakout
from bubus.live_scanner import LiveSignalScanner
from bubus.research import ResearchError, compare_candles
from bubus.reconciliation import ReconciliationEvidenceError, audit_reconciliation
from bubus.risk_readiness import RiskReadinessError, assess_risk_readiness
from bubus.order_chain_audit import OrderChainAuditError, audit_order_chain_configuration
from bubus.order_journal import OfflineOrderJournal, OrderJournalError
from bubus.live_order_journal import LiveOrderJournal, LiveOrderJournalError
from bubus.offline_risk_reservations import OfflineRiskReservations, OfflineRiskError
from bubus.offline_trade_transaction import OfflineTradeTransaction, OfflineTradeTransactionError
from bubus.store import DeskStore
from invest_ai.data.cryptocom_market_overview import CryptoComMarketOverviewError
from invest_ai.data.cryptocom_instruments import CryptoComInstrumentCatalog, CryptoComInstrumentCatalogError
from invest_ai.data.cryptocom_market_readonly import CryptoComMarketDataError, CryptoComMarketEvidenceStore
from invest_ai.execution.cryptocom_spot import CryptoComSpotOrderAdapter, CryptoComSpotOrderError
from invest_ai.portfolio.cryptocom_candidate import (
    CryptoComCandidateError, cryptocom_snapshot_from_persisted, evaluate_spot_portfolio_candidate,
)
from invest_ai.assistant.market_dashboard import MarketDashboardHandler, make_server as make_base_server
from invest_ai.assistant.market_dashboard import _cryptocom_snapshot_payload
from invest_ai.portfolio.cryptocom_credential_store import CryptoComCredentialStoreError, WindowsCryptoComCredentialStore
from invest_ai.portfolio.cryptocom_readonly import CryptoComPortfolioError, CryptoComReadOnlyPortfolioClient
from bubus.portfolio_observer import ReadOnlyPortfolioObserver

ASSETS = Path(__file__).parent / "ui"
ROOT = Path(__file__).resolve().parents[2]


class _ReadCredentialsUnavailable(Exception):
    """A requested local API-key vault has no usable read credentials."""


def _portfolio_read_credentials(server, payload: dict, fields: set[str]):
    """Resolve an explicit raw key or one of the two separate local read vaults."""
    if set(payload) == fields | {"api_key", "api_secret"}:
        return payload["api_key"], payload["api_secret"]
    for selector, attribute in (
        ("use_observer_credentials", "cryptocom_observer_credential_store"),
        ("use_saved_credentials", "cryptocom_credential_store"),
    ):
        if set(payload) == fields | {selector} and payload[selector] is True:
            store = getattr(server, attribute, None)
            credentials = store.load() if store is not None and store.available else None
            if credentials is None:
                raise _ReadCredentialsUnavailable()
            return credentials.api_key, credentials.api_secret
    raise ValueError("Credential selection is invalid.")


class DeskHandler(MarketDashboardHandler):
    server_version = "BUBUS2.0/2"

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        path = urlsplit(self.path).path
        if (status == 200 and path in {"/api/cryptocom/portfolio/import", "/api/cryptocom/portfolio/refresh"}
                and isinstance(payload, dict) and payload.get("provider") == "Crypto.com Exchange"):
            try:
                payload = self.server.desk_store.record_cryptocom_portfolio(payload)
            except (sqlite3.Error, ValueError):
                payload = dict(payload)
                payload["persistent"] = False
                payload["persistence_warning"] = "Lecture réussie; BUBUS2.0 n’a pas pu conserver ce relevé localement."
                payload["usable_for_risk"] = False
                payload["usable_for_execution"] = False
                payload["orders_enabled"] = False
        super()._send_json(status, payload)

    def do_GET(self):
        if not self._local_host():
            return self._send_json(403, {"error": "Accès local uniquement."})
        parts = urlsplit(self.path)
        files = {"/": ("index.html", "text/html; charset=utf-8"),
                 "/assets/desk.css": ("desk.css", "text/css; charset=utf-8"),
                 "/assets/desk.js": ("desk.js", "text/javascript; charset=utf-8"),
                 "/assets/icon.svg": ("icon.svg", "image/svg+xml")}
        if parts.path in files:
            name, mime = files[parts.path]
            return self._send_file(ASSETS / name, mime)
        if parts.path.startswith("/api/desk/"):
            if not self._same_origin_read():
                return self._send_json(403, {"error": "Origine locale requise."})
            if parts.path == "/api/desk/state":
                return self._send_json(200, {"product": PRODUCT_NAME, "version": __version__,
                    "watchlist": self.server.desk_store.watchlist(), "trading": {
                        "state": "NOT_READY", "armed": False, "orders_enabled": False,
                        "reason": "Moteur réel et politique de risque en construction."}})
            if parts.path == "/api/desk/portfolio-observer":
                if not self._same_origin_read():
                    return self._send_json(403, {"error": "Origine locale requise."})
                credential_store = self.server.cryptocom_observer_credential_store
                try:
                    credentials_persisted = bool(
                        credential_store is not None and credential_store.available
                        and credential_store.has_credentials())
                except CryptoComCredentialStoreError:
                    return self._send_json(503, {"error": "Le coffre de la clé observateur n’est pas lisible."})
                return self._send_json(200, {
                    "observer": self.server.cryptocom_portfolio_observer.state(),
                    "credentials_persisted": credentials_persisted,
                    "permissions_verified": False,
                })
            if parts.path == "/api/desk/activity":
                return self._send_json(200, {"events": self.server.desk_store.activity()})
            if parts.path == "/api/desk/risk-limits":
                try:
                    return self._send_json(200, {"candidate": self.server.desk_store.latest_risk_limits()})
                except (sqlite3.Error, ValueError):
                    return self._send_json(503, {"error": "Le brouillon Risk a échoué à la vérification d’intégrité."})
            if parts.path == "/api/desk/risk-readiness":
                try:
                    store = self.server.desk_store
                    valuations = store.cryptocom_valuation_history(limit=3000)
                    broker = store.latest_cryptocom_portfolio()
                    reconciliation = audit_reconciliation(
                        valuations=valuations,
                        transaction_pages=store.cryptocom_transaction_history(limit=50),
                        portfolio=broker,
                        portfolio_history=store.cryptocom_portfolio_history(limit=5000),
                        cash_history=store.latest_cryptocom_balance_history(),
                        trade_pages=store.cryptocom_trade_history(limit=50),
                        funding_history=store.cryptocom_funding_history(limit=100),
                    )
                    report = assess_risk_readiness(
                        draft=store.latest_risk_limits(), portfolio=broker,
                        valuations=valuations[:100], reconciliation=reconciliation,
                        order_gate_connected=self.server.cryptocom_spot_order_adapter._gate is not None,
                    )
                    return self._send_json(200, {"readiness": report})
                except (sqlite3.Error, ValueError, TypeError, RiskReadinessError):
                    return self._send_json(503, {"error": "Le contrôle de préparation Risk a refusé une preuve locale invalide."})
            if parts.path == "/api/desk/order-chain-audit":
                try:
                    store = self.server.desk_store
                    valuations = store.cryptocom_valuation_history(limit=3000)
                    broker = store.latest_cryptocom_portfolio()
                    reconciliation = audit_reconciliation(
                        valuations=valuations,
                        transaction_pages=store.cryptocom_transaction_history(limit=50),
                        portfolio=broker,
                        portfolio_history=store.cryptocom_portfolio_history(limit=5000),
                        cash_history=store.latest_cryptocom_balance_history(),
                        trade_pages=store.cryptocom_trade_history(limit=50),
                        funding_history=store.cryptocom_funding_history(limit=100),
                    )
                    risk = assess_risk_readiness(
                        draft=store.latest_risk_limits(), portfolio=broker,
                        valuations=valuations[:100], reconciliation=reconciliation,
                        order_gate_connected=self.server.cryptocom_spot_order_adapter._gate is not None,
                    )
                    adapter = self.server.cryptocom_spot_order_adapter
                    live_journal = self.server.live_order_journal.audit()
                    result = audit_order_chain_configuration(
                        gate_connected=adapter._gate is not None,
                        spot_verifier_connected=adapter._instrument_verifier is not None,
                        risk_readiness=risk,
                        manual_readback_route_connected=(
                            callable(getattr(adapter, "get_order_detail", None))
                            and callable(getattr(self.server.live_order_journal, "record_order_readback", None))
                        ),
                        durable_journal_ready=(
                            live_journal.get("status") == "INTEGRITY_OK"
                            and live_journal.get("authority_status") == "LIVE_ORDER_JOURNAL_EVIDENCE_ONLY"
                            and live_journal.get("orders_enabled") is False
                        ),
                    )
                    return self._send_json(200, {"audit": result})
                except (sqlite3.Error, ValueError, TypeError, OrderChainAuditError, LiveOrderJournalError):
                    return self._send_json(503, {"error": "La chaîne d’ordres ne dispose pas de preuves locales intègres."})
            if parts.path == "/api/desk/live-order-journal":
                try:
                    return self._send_json(200, {"journal": self.server.live_order_journal.audit()})
                except (sqlite3.Error, ValueError, TypeError, LiveOrderJournalError):
                    return self._send_json(503, {"error": "Le journal réel des ordres a échoué au contrôle d’intégrité."})
            if parts.path == "/api/desk/order-journal":
                try:
                    report = self.server.offline_order_journal.audit(limit=20)
                    return self._send_json(200, {"journal": report})
                except (sqlite3.Error, ValueError, TypeError, OrderJournalError):
                    return self._send_json(503, {"error": "Le journal synthétique d’ordres a échoué au contrôle d’intégrité."})
            if parts.path == "/api/desk/offline-risk-reservations":
                try:
                    report = self.server.offline_risk_reservations.audit(limit=20)
                    return self._send_json(200, {"reservations": report})
                except (sqlite3.Error, ValueError, TypeError, OfflineRiskError):
                    return self._send_json(503, {"error": "Les réservations Risk synthétiques ont échoué au contrôle d’intégrité."})
            if parts.path == "/api/desk/offline-trade-transaction":
                try:
                    return self._send_json(200, {"transaction": self.server.offline_trade_transaction.audit()})
                except (sqlite3.Error, ValueError, TypeError, OfflineTradeTransactionError,
                        OfflineRiskError, OrderJournalError):
                    return self._send_json(503, {"error": "Le rapprochement atomique local a échoué au contrôle d’intégrité."})
            if parts.path == "/api/desk/portfolio":
                try:
                    return self._send_json(200, {"snapshot": self.server.desk_store.latest_cryptocom_portfolio()})
                except (sqlite3.Error, ValueError):
                    return self._send_json(503, {"error": "Le relevé local a échoué à la vérification d’intégrité."})
            if parts.path == "/api/desk/transactions":
                try:
                    return self._send_json(200, {"snapshot": self.server.desk_store.latest_cryptocom_transactions()})
                except (sqlite3.Error, ValueError):
                    return self._send_json(503, {"error": "L’historique local a échoué à la vérification d’intégrité."})
            if parts.path == "/api/desk/funding-history":
                try:
                    direction = parse_qs(parts.query).get("direction", [None])[0]
                    return self._send_json(200, {"snapshot": self.server.desk_store.latest_cryptocom_funding_history(direction)})
                except (sqlite3.Error, ValueError):
                    return self._send_json(503, {"error": "L’historique dépôts/retraits a échoué à la vérification d’intégrité."})
            if parts.path == "/api/desk/trades":
                try:
                    return self._send_json(200, {"snapshot": self.server.desk_store.latest_cryptocom_trades()})
                except (sqlite3.Error, ValueError):
                    return self._send_json(503, {"error": "L’historique des exécutions a échoué à la vérification d’intégrité."})
            if parts.path == "/api/desk/balance-history":
                try:
                    return self._send_json(200, {"snapshot": self.server.desk_store.latest_cryptocom_balance_history()})
                except (sqlite3.Error, ValueError):
                    return self._send_json(503, {"error": "L’historique cash local a échoué à la vérification d’intégrité."})
            if parts.path == "/api/desk/portfolio-valuations":
                try:
                    return self._send_json(200, {"valuations": self.server.desk_store.cryptocom_valuation_history(limit=100)})
                except (sqlite3.Error, ValueError):
                    return self._send_json(503, {"error": "L’historique des valorisations a échoué à la vérification d’intégrité."})
            if parts.path == "/api/desk/reconciliation":
                try:
                    store = self.server.desk_store
                    report = audit_reconciliation(
                        valuations=store.cryptocom_valuation_history(limit=3000),
                        transaction_pages=store.cryptocom_transaction_history(limit=50),
                        portfolio=store.latest_cryptocom_portfolio(),
                        portfolio_history=store.cryptocom_portfolio_history(limit=5000),
                        cash_history=store.latest_cryptocom_balance_history(),
                        trade_pages=store.cryptocom_trade_history(limit=50),
                        funding_history=store.cryptocom_funding_history(limit=100),
                    )
                    return self._send_json(200, {"reconciliation": report})
                except (sqlite3.Error, ValueError, TypeError, ReconciliationEvidenceError):
                    return self._send_json(503, {"error": "Les preuves locales du rapprochement ont échoué au contrôle d’intégrité."})
            if parts.path == "/api/desk/candles":
                query = parse_qs(parts.query)
                try:
                    return self._send_json(200, self.server.candles.get(
                        query.get("symbol", [""])[0], query.get("timeframe", ["1h"])[0]))
                except ValueError as exc:
                    return self._send_json(400, {"error": str(exc)})
            if parts.path == "/api/desk/research":
                query = parse_qs(parts.query)
                try:
                    candles = self.server.candles.get(
                        query.get("symbol", [""])[0], query.get("timeframe", ["1h"])[0])["candles"]
                    return self._send_json(200, compare_candles(
                        candles,
                        commission_bps=int(query.get("commission_bps", ["50"])[0]),
                        slippage_bps=int(query.get("slippage_bps", ["5"])[0]),
                        test_percent=int(query.get("test_percent", ["30"])[0]),
                        timeframe=query.get("timeframe", ["1h"])[0]))
                except (ResearchError, ValueError) as exc:
                    return self._send_json(400, {"error": str(exc) or "Paramètre de recherche invalide."})
            if parts.path == "/api/desk/live-signal":
                query = parse_qs(parts.query)
                symbol = query.get("symbol", [""])[0]
                timeframe = query.get("timeframe", [""])[0]
                if set(query) != {"symbol", "timeframe"} or len(query["symbol"]) != 1 or len(query["timeframe"]) != 1:
                    return self._send_json(400, {"error": "Paire et intervalle sont obligatoires."})
                if (not isinstance(symbol, str) or timeframe not in {"5m", "15m", "1h", "4h", "1D"}
                        or not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,15}_[A-Z0-9]{2,12}", symbol)):
                    return self._send_json(400, {"error": "Paire Spot ou intervalle invalide."})
                try:
                    stream = self.server.cryptocom_market_stream.state()
                    if stream.get("connection_status") != "CONNECTED":
                        return self._send_json(409, {"error": "Connecte d’abord le flux public Crypto.com."})
                    tick = self.server.cryptocom_market_stream.get_tick(symbol)
                    if tick is None:
                        return self._send_json(409, {"error": "Aucun tick live reçu pour cette paire."})
                    rules = self.server.cryptocom_instrument_catalog.get_spot_catalog().get(symbol)
                    if rules is None:
                        return self._send_json(409, {"error": "Cette paire n’a pas de règles Spot tradables actuelles."})
                    candles = self.server.candles.get(symbol, timeframe)
                    result = observe_cryptocom_breakout(symbol, timeframe, candles, tick, rules,
                                                        as_of=datetime.now(timezone.utc))
                    return self._send_json(200, {
                        "status": result.status.value, "direction": result.direction,
                        "symbol": result.symbol,
                        "observed_at": result.observed_at.isoformat().replace("+00:00", "Z"),
                        "reason_codes": list(result.reason_codes),
                        "metrics": [{"name": name, "value": value} for name, value in result.metrics],
                        "evidence_refs": list(result.evidence_refs),
                        "authority_status": result.authority_status,
                        "execution_allowed": result.execution_allowed,
                        "orders_enabled": False,
                    })
                except CryptoComInstrumentCatalogError:
                    return self._send_json(503, {"error": "Les règles Spot Crypto.com sont indisponibles."})
                except LiveSignalError as exc:
                    return self._send_json(409, {"error": str(exc)})
                except (ValueError, TypeError, KeyError):
                    return self._send_json(503, {"error": "Les preuves live Crypto.com sont invalides ou indisponibles."})
            if parts.path == "/api/desk/live-signals":
                scan = self.server.live_signal_scanner.state(result_limit=2000)
                return self._send_json(200, scan)
            return self._send_json(404, {"error": "Route inconnue."})
        if parts.path == "/api/market-data/crypto-overview":
            if not self._same_origin_read():
                return self._send_json(403, {"error": "Origine locale requise."})
            overview = copy.deepcopy(self.server.cryptocom_market_overview.get_overview())
            try:
                catalog = self.server.cryptocom_instrument_catalog.get_spot_catalog()
                for market in overview.get("markets", []):
                    if isinstance(market, dict):
                        market["instrument_rules"] = catalog.get(market.get("symbol"))
                overview["instrument_catalog_status"] = "CURRENT" if catalog else "EMPTY"
            except CryptoComInstrumentCatalogError:
                overview["instrument_catalog_status"] = "UNAVAILABLE"
            stream = self.server.cryptocom_market_stream.state()
            ticks = {row["symbol"]: row for row in stream.get("quotes", [])
                     if isinstance(row, dict) and isinstance(row.get("symbol"), str)}
            if stream.get("connection_status") == "CONNECTED" and ticks:
                updated_pairs = 0
                now = datetime.now(timezone.utc)
                for market in overview.get("markets", []):
                    tick = ticks.get(market.get("symbol")) if isinstance(market, dict) else None
                    if tick is not None:
                        try:
                            received_at = datetime.fromisoformat(str(tick["received_at"]).replace("Z", "+00:00"))
                            age_seconds = (now - received_at.astimezone(timezone.utc)).total_seconds()
                        except (KeyError, TypeError, ValueError):
                            continue
                        if not 0 <= age_seconds <= 30:
                            continue
                        market["rest_price"] = market.get("price")
                        market["rest_provider_timestamp"] = market.get("provider_timestamp")
                        market["price"] = tick["price"]
                        market["last_trade_price"] = tick.get("last_trade_price", tick["price"])
                        market["best_bid"] = tick.get("best_bid")
                        market["best_ask"] = tick.get("best_ask")
                        market["quote_status"] = tick.get("quote_status", "INCOMPLETE")
                        market["provider_timestamp"] = tick["provider_timestamp"]
                        market["price_source"] = "Crypto.com Exchange public ticker WebSocket"
                        updated_pairs += 1
                overview["realtime"] = {"status": "PARTIAL" if updated_pairs else "REST", "updated_pairs": updated_pairs,
                    "subscribed_pairs": len(stream.get("symbols", [])),
                    "coverage": "Cotations WebSocket reçues depuis 30 s maximum; les autres paires restent actualisées par REST."}
            else:
                overview["realtime"] = {"status": "REST", "updated_pairs": 0,
                    "subscribed_pairs": len(stream.get("symbols", [])) if stream.get("connection_status") == "CONNECTED" else 0,
                    "coverage": "REST Crypto.com; aucune cotation WebSocket fraîche reçue."}
            return self._send_json(200, overview)
        return super().do_GET()

    def do_POST(self):
        observer_paths = {
            "/api/cryptocom/portfolio-observer/credentials",
            "/api/cryptocom/portfolio-observer/start",
            "/api/cryptocom/portfolio-observer/stop",
        }
        if urlsplit(self.path).path in observer_paths:
            if not self._local_host() or not self._same_origin():
                return self._reject_post(403, "Origine locale requise.")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold() != "application/json":
                return self._reject_post(415, "JSON requis.")
            raw_length = self.headers.get("Content-Length", "")
            if not raw_length.isdecimal() or len(raw_length) > 4 or not 1 <= int(raw_length) <= 2048:
                return self._reject_post(413, "Requête observateur invalide.")
            path = urlsplit(self.path).path
            try:
                payload = json.loads(self.rfile.read(int(raw_length)))
                credential_store = self.server.cryptocom_observer_credential_store
                if path.endswith("/credentials"):
                    if (not isinstance(payload, dict) or set(payload) != {
                            "api_key", "api_secret", "confirm_read_only"}
                            or payload.get("confirm_read_only") is not True):
                        return self._reject_post(400, "Confirme une clé dédiée « Can Read », sans trading ni retraits.")
                    if self.server.cryptocom_portfolio_observer.state()["status"] != "STOPPED":
                        return self._reject_post(409, "Arrête l’observateur avant de remplacer sa clé dédiée.")
                    if credential_store is None or not credential_store.available:
                        return self._reject_post(409, "Le coffre Windows est requis pour conserver la clé observateur.")
                    primary_store = self.server.cryptocom_credential_store
                    if primary_store is None or not primary_store.available:
                        return self._reject_post(409, "Impossible de vérifier que cette clé est différente de la clé principale.")
                    primary_credentials = primary_store.load()
                    if primary_credentials is None:
                        return self._reject_post(409, "Impossible de vérifier que cette clé est différente de la clé principale.")
                    if payload["api_key"] == primary_credentials.api_key:
                        return self._reject_post(409, "Utilise une clé API distincte, dédiée à la lecture seule.")
                    credential_store.store(payload["api_key"], payload["api_secret"])
                    return self._send_json(200, {"credentials_persisted": True, "permissions_verified": False})
                if path.endswith("/start"):
                    if payload != {"confirm_read_only": True}:
                        return self._reject_post(400, "Confirme le démarrage avec la clé séparée en lecture seule.")
                    if credential_store is None or not credential_store.available:
                        return self._reject_post(409, "Le coffre Windows est requis pour démarrer l’observateur.")
                    if credential_store.load() is None:
                        return self._reject_post(409, "Enregistre d’abord la clé dédiée « Can Read ».")
                    started = self.server.cryptocom_portfolio_observer.start()
                    return self._send_json(202, {"started": started,
                        "observer": self.server.cryptocom_portfolio_observer.state(),
                        "permissions_verified": False, "orders_enabled": False})
                if payload != {"stop": True}:
                    return self._reject_post(400, "Confirme l’arrêt de l’observateur.")
                stopped = self.server.cryptocom_portfolio_observer.stop()
                return self._send_json(200 if stopped else 202, {
                    "stopped": stopped, "observer": self.server.cryptocom_portfolio_observer.state(),
                    "orders_enabled": False})
            except (CryptoComCredentialStoreError, ValueError, TypeError):
                return self._reject_post(503, "L’opération sur la clé ou l’observateur a échoué.")
        if urlsplit(self.path).path == "/api/cryptocom/orders/readback":
            if not self._local_host() or not self._same_origin():
                return self._reject_post(403, "Origine locale requise.")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold() != "application/json":
                return self._reject_post(415, "JSON requis.")
            raw_length = self.headers.get("Content-Length", "")
            if not raw_length.isdecimal() or len(raw_length) > 4 or not 1 <= int(raw_length) <= 1024:
                return self._reject_post(413, "Requête invalide.")
            try:
                payload = json.loads(self.rfile.read(int(raw_length)))
                if (not isinstance(payload, dict) or set(payload) != {"use_saved_credentials", "client_oid"}
                        or payload["use_saved_credentials"] is not True):
                    return self._reject_post(400, "Lecture d’ordre locale invalide.")
                client_oid = payload["client_oid"]
                try:
                    canonical_oid = str(uuid.UUID(client_oid))
                except (AttributeError, TypeError, ValueError):
                    canonical_oid = ""
                if not isinstance(client_oid, str) or canonical_oid != client_oid:
                    return self._reject_post(400, "Identifiant d’ordre invalide.")
                journal = self.server.live_order_journal
                if not journal.contains_intent(client_oid):
                    return self._reject_post(404, "Aucune tentative locale ne correspond à cet identifiant.")
                journal_report = journal.audit(client_oid)
                intent = next((row for row in journal_report["intents"]
                               if row["client_oid"] == client_oid), None)
                if intent is None or intent["state"] not in {"ATTEMPTED", "UNCERTAIN", "ACKNOWLEDGED"}:
                    return self._reject_post(409, "La tentative n’est pas dans un état autorisant une relecture.")
                store = self.server.cryptocom_credential_store
                credentials = store.load() if store is not None and store.available else None
                if credentials is None:
                    return self._reject_post(409, "Aucune clé de lecture enregistrée dans le coffre Windows.")
                status = self.server.cryptocom_spot_order_adapter.get_order_detail(
                    api_key=credentials.api_key, api_secret=credentials.api_secret, client_oid=client_oid)
                return self._send_json(200, {"order": {
                    "client_oid": status.client_oid, "order_id": status.order_id, "status": status.status,
                    "executed_quantity": format(status.executed_quantity, "f"),
                    "executed_value": format(status.executed_value, "f"), "fees": format(status.fees, "f"),
                    "fee_currency": status.fee_currency,
                    "updated_at": status.updated_at.isoformat() if status.updated_at else None,
                    "authority_status": "BROKER_ORDER_READBACK_EVIDENCE_ONLY",
                    "orders_enabled": False,
                }})
            except CryptoComSpotOrderError:
                return self._reject_post(503, "Crypto.com n’a pas confirmé la relecture; aucune reprise automatique n’est autorisée.")
            except CryptoComCredentialStoreError:
                return self._reject_post(503, "Le coffre Windows a refusé l’accès à la clé de lecture.")
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError,
                    sqlite3.Error, LiveOrderJournalError):
                return self._reject_post(503, "Le journal ou les preuves de relecture ont échoué au contrôle d’intégrité.")
        if urlsplit(self.path).path == "/api/cryptocom/portfolio/balance-history":
            if not self._local_host() or not self._same_origin():
                return self._reject_post(403, "Origine locale requise.")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold() != "application/json":
                return self._reject_post(415, "JSON requis.")
            raw_length = self.headers.get("Content-Length", "")
            if not raw_length.isdecimal() or len(raw_length) > 4 or not 1 <= int(raw_length) <= 1024:
                return self._reject_post(413, "Requête invalide.")
            try:
                payload = json.loads(self.rfile.read(int(raw_length)))
                if not isinstance(payload, dict):
                    return self._reject_post(400, "Requête invalide.")
                timeframe, limit = payload.get("timeframe", "H1"), payload.get("limit", 24)
                try:
                    api_key, api_secret = _portfolio_read_credentials(
                        self.server, payload, {"timeframe", "limit"})
                except _ReadCredentialsUnavailable:
                    return self._reject_post(409, "Aucune clé dédiée à la lecture n’est enregistrée dans le coffre demandé.")
                except ValueError:
                    return self._reject_post(400, "Clé API ou choix du coffre requis.")
                client = self.server.cryptocom_portfolio_client or CryptoComReadOnlyPortfolioClient()
                history = client.read_balance_history(api_key, api_secret, timeframe=timeframe, limit=limit)
                snapshot = self.server.desk_store.record_cryptocom_balance_history({
                    "observed_at": history.observed_at.isoformat(), "timeframe": history.timeframe,
                    "currency": history.points[0].currency if history.points else "UNKNOWN",
                    "points": [{"timestamp": point.timestamp.isoformat(), "total_cash": format(point.total_cash, "f")}
                               for point in history.points],
                    "source": history.source, "provider_payload_sha256": history.payload_sha256,
                })
                return self._send_json(200, {"snapshot": snapshot})
            except CryptoComPortfolioError as exc:
                return self._reject_post(503, str(exc))
            except CryptoComCredentialStoreError:
                return self._reject_post(503, "Le coffre Windows a refusé l’accès aux identifiants enregistrés.")
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError, sqlite3.Error):
                return self._reject_post(503, "La lecture ou la conservation de l’historique cash a échoué.")
        if urlsplit(self.path).path == "/api/cryptocom/portfolio/fees":
            if not self._local_host() or not self._same_origin():
                return self._reject_post(403, "Origine locale requise.")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold() != "application/json":
                return self._reject_post(415, "JSON requis.")
            raw_length = self.headers.get("Content-Length", "")
            if not raw_length.isdecimal() or len(raw_length) > 4 or not 1 <= int(raw_length) <= 1024:
                return self._reject_post(413, "Requête invalide.")
            try:
                payload = json.loads(self.rfile.read(int(raw_length)))
                if not isinstance(payload, dict):
                    return self._reject_post(400, "Requête invalide.")
                try:
                    api_key, api_secret = _portfolio_read_credentials(self.server, payload, set())
                except _ReadCredentialsUnavailable:
                    return self._reject_post(409, "Aucune clé dédiée à la lecture n’est enregistrée dans le coffre demandé.")
                except ValueError:
                    return self._reject_post(400, "Clé API ou choix du coffre requis.")
                client = self.server.cryptocom_portfolio_client or CryptoComReadOnlyPortfolioClient()
                fee = client.read_fee_schedule(api_key, api_secret)
                return self._send_json(200, {"spot_tier": fee.spot_tier,
                    "maker_rate_bps": format(fee.maker_rate_bps, "f"),
                    "taker_rate_bps": format(fee.taker_rate_bps, "f"),
                    "maker_rate": format(fee.maker_rate_bps / 10_000, "f"),
                    "taker_rate": format(fee.taker_rate_bps / 10_000, "f"),
                    "observed_at": fee.observed_at.isoformat(), "source": fee.source,
                    "payload_sha256": fee.payload_sha256, "authority_status": "OBSERVATION_ONLY",
                    "usable_for_risk": False, "orders_enabled": False})
            except CryptoComPortfolioError as exc:
                return self._reject_post(503, str(exc))
            except CryptoComCredentialStoreError:
                return self._reject_post(503, "Le coffre Windows a refusé l’accès aux identifiants enregistrés.")
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
                return self._reject_post(503, "La lecture des frais Crypto.com a échoué.")
        if urlsplit(self.path).path == "/api/cryptocom/portfolio/funding-history":
            if not self._local_host() or not self._same_origin():
                return self._reject_post(403, "Origine locale requise.")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold() != "application/json":
                return self._reject_post(415, "JSON requis.")
            raw_length = self.headers.get("Content-Length", "")
            if not raw_length.isdecimal() or len(raw_length) > 4 or not 1 <= int(raw_length) <= 2048:
                return self._reject_post(413, "Requête invalide.")
            try:
                payload = json.loads(self.rfile.read(int(raw_length)))
                if not isinstance(payload, dict):
                    return self._reject_post(400, "Requête invalide.")
                start_ms, end_ms, direction = payload.get("start_time_ms"), payload.get("end_time_ms"), payload.get("direction")
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                if (type(start_ms) is not int or type(end_ms) is not int or direction not in {"DEPOSIT", "WITHDRAWAL"}
                        or start_ms < 0 or end_ms <= start_ms or end_ms > now_ms + 60_000
                        or end_ms - start_ms > 7 * 86_400_000):
                    return self._reject_post(400, "Choisis une direction et une période valide de sept jours maximum.")
                try:
                    api_key, api_secret = _portfolio_read_credentials(
                        self.server, payload, {"start_time_ms", "end_time_ms", "direction"})
                except _ReadCredentialsUnavailable:
                    return self._reject_post(409, "Aucune clé dédiée à la lecture n’est enregistrée dans le coffre demandé.")
                except ValueError:
                    return self._reject_post(400, "Clé API ou choix du coffre requis.")
                client = self.server.cryptocom_portfolio_client or CryptoComReadOnlyPortfolioClient()
                history = client.read_funding_history(api_key, api_secret, direction=direction,
                    start_time_ms=start_ms, end_time_ms=end_ms, page_size=200, max_pages=16)
                pages = [{"page": page.page, "page_size": page.page_size,
                    "payload_sha256": page.payload_sha256,
                    "items": [{"direction": item.direction, "transfer_id": item.transfer_id,
                        "currency": item.currency, "amount": format(item.amount, "f"), "fee": format(item.fee, "f"),
                        "status": item.status, "created_at": item.created_at.isoformat(),
                        "updated_at": item.updated_at.isoformat(), "source": item.source,
                        "payload_sha256": item.payload_sha256} for item in page.items]} for page in history.pages]
                snapshot = self.server.desk_store.record_cryptocom_funding_history({
                    "observed_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                    "direction": direction, "start_time_ms": history.start_time_ms, "end_time_ms": history.end_time_ms,
                    "pages": pages, "complete": history.complete, "next_page": history.next_page,
                    "request_count": history.request_count})
                return self._send_json(200, {"snapshot": snapshot})
            except CryptoComPortfolioError as exc:
                return self._reject_post(503, str(exc))
            except CryptoComCredentialStoreError:
                return self._reject_post(503, "Le coffre Windows a refusé l’accès aux identifiants enregistrés.")
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError, sqlite3.Error):
                return self._reject_post(503, "La lecture ou la conservation des dépôts/retraits a échoué.")
        if urlsplit(self.path).path == "/api/cryptocom/portfolio/transactions":
            if not self._local_host() or not self._same_origin():
                return self._reject_post(403, "Origine locale requise.")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold() != "application/json":
                return self._reject_post(415, "JSON requis.")
            raw_length = self.headers.get("Content-Length", "")
            if not raw_length.isdecimal() or len(raw_length) > 4 or not 1 <= int(raw_length) <= 2048:
                return self._reject_post(413, "Requête invalide.")
            try:
                payload = json.loads(self.rfile.read(int(raw_length)))
                if not isinstance(payload, dict):
                    return self._reject_post(400, "Requête invalide.")
                start_ms, end_ms = payload.get("start_time_ms"), payload.get("end_time_ms")
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                if (type(start_ms) is not int or type(end_ms) is not int or start_ms < 0
                        or end_ms <= start_ms or end_ms > now_ms + 60_000 or end_ms - start_ms > 7 * 86_400_000):
                    return self._reject_post(400, "La période doit être valide et ne pas dépasser sept jours.")
                try:
                    api_key, api_secret = _portfolio_read_credentials(
                        self.server, payload, {"start_time_ms", "end_time_ms"})
                except _ReadCredentialsUnavailable:
                    return self._reject_post(409, "Aucune clé dédiée à la lecture n’est enregistrée dans le coffre demandé.")
                except ValueError:
                    return self._reject_post(400, "Clé API ou choix du coffre requis.")
                client = self.server.cryptocom_portfolio_client or CryptoComReadOnlyPortfolioClient()
                paged_reader = getattr(client, "read_transactions_partitioned", None)
                if callable(paged_reader):
                    page_set = paged_reader(api_key, api_secret, start_time_ms=start_ms,
                        end_time_ms=end_ms, limit=100, max_requests=16)
                    pages = page_set.pages
                    batch_complete = page_set.complete
                    incomplete = [list(interval) for interval in page_set.incomplete_intervals]
                    request_count = page_set.request_count
                else:
                    transactions, digest = client.read_transactions(api_key, api_secret,
                        start_time_ms=start_ms, end_time_ms=end_ms, limit=100)
                    pages = (type("TransactionPage", (), {"start_time_ms": start_ms,
                        "end_time_ms": end_ms, "items": transactions, "payload_sha256": digest})(),)
                    batch_complete, incomplete, request_count = False, [[start_ms, end_ms]], 1
                observed_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
                batch = {"batch_id": str(uuid.uuid4()), "query_start_time_ms": start_ms,
                    "query_end_time_ms": end_ms, "coverage_complete": batch_complete,
                    "incomplete_intervals": incomplete, "request_count": request_count,
                    "page_count": len(pages)}
                for page in pages:
                    items = [{"journal_type": item.journal_type, "journal_id": item.journal_id,
                        "transaction_qty": format(item.transaction_qty, "f"),
                        "transaction_cost": format(item.transaction_cost, "f"),
                        "realized_pnl": format(item.realized_pnl, "f"), "order_id": item.order_id,
                        "trade_id": item.trade_id, "instrument_name": item.instrument_name, "side": item.side,
                        "occurred_at": item.occurred_at.isoformat(), "source": item.source,
                        "payload_sha256": item.payload_sha256} for item in page.items]
                    self.server.desk_store.record_cryptocom_transactions({"observed_at": observed_at,
                        "start_time_ms": page.start_time_ms, "end_time_ms": page.end_time_ms,
                        "items": items, "provider_payload_sha256": page.payload_sha256, "batch": batch})
                snapshot = self.server.desk_store.latest_cryptocom_transactions()
                if snapshot is None:
                    return self._reject_post(503, "Aucune page de transactions n’a été conservée.")
                return self._send_json(200, {"snapshot": snapshot})
            except CryptoComPortfolioError as exc:
                return self._reject_post(503, str(exc))
            except CryptoComCredentialStoreError:
                return self._reject_post(503, "Le coffre Windows a refusé l’accès aux identifiants enregistrés.")
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError, sqlite3.Error):
                return self._reject_post(503, "La lecture ou la conservation de l’historique a échoué.")
        if urlsplit(self.path).path == "/api/cryptocom/portfolio/trades":
            if not self._local_host() or not self._same_origin():
                return self._reject_post(403, "Origine locale requise.")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold() != "application/json":
                return self._reject_post(415, "JSON requis.")
            raw_length = self.headers.get("Content-Length", "")
            if not raw_length.isdecimal() or len(raw_length) > 4 or not 1 <= int(raw_length) <= 2048:
                return self._reject_post(413, "Requête invalide.")
            try:
                payload = json.loads(self.rfile.read(int(raw_length)))
                if not isinstance(payload, dict):
                    return self._reject_post(400, "Requête invalide.")
                start_raw, end_raw = payload.get("start_time_ns"), payload.get("end_time_ns")
                if (not isinstance(start_raw, str) or not start_raw.isdecimal()
                        or not isinstance(end_raw, str) or not end_raw.isdecimal()):
                    return self._reject_post(400, "La période d’exécutions est invalide.")
                start_ns, end_ns = int(start_raw), int(end_raw)
                now_ns = time.time_ns()
                if (start_ns < 0 or end_ns <= start_ns or end_ns > now_ns + 60_000_000_000
                        or end_ns - start_ns > 7 * 86_400_000_000_000):
                    return self._reject_post(400, "La période doit être valide et ne pas dépasser sept jours.")
                try:
                    api_key, api_secret = _portfolio_read_credentials(
                        self.server, payload, {"start_time_ns", "end_time_ns"})
                except _ReadCredentialsUnavailable:
                    return self._reject_post(409, "Aucune clé dédiée à la lecture n’est enregistrée dans le coffre demandé.")
                except ValueError:
                    return self._reject_post(400, "Clé API ou choix du coffre requis.")
                client = self.server.cryptocom_portfolio_client or CryptoComReadOnlyPortfolioClient()
                observed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                partitioned_reader = getattr(client, "read_trades_partitioned", None)
                if callable(partitioned_reader):
                    page_set = partitioned_reader(api_key, api_secret, start_time_ns=start_ns,
                        end_time_ns=end_ns, limit=100, max_requests=16)
                    batch = {"batch_id": str(uuid.uuid4()), "query_start_time_ns": start_ns,
                        "query_end_time_ns": end_ns, "coverage_complete": page_set.complete,
                        "incomplete_intervals": [list(interval) for interval in page_set.incomplete_intervals],
                        "request_count": page_set.request_count, "page_count": len(page_set.pages)}
                    snapshots = []
                    for page in page_set.pages:
                        items = [{"trade_id": item.trade_id, "order_id": item.order_id,
                            "trade_match_id": item.trade_match_id, "client_oid": item.client_oid,
                            "instrument_name": item.instrument_name, "side": item.side, "taker_side": item.taker_side,
                            "quantity": format(item.quantity, "f"), "price": format(item.price, "f"),
                            "fees": format(item.fees, "f"), "fee_currency": item.fee_currency,
                            "occurred_at": item.occurred_at.isoformat(), "timestamp_ns": item.timestamp_ns,
                            "source": item.source, "payload_sha256": item.payload_sha256} for item in page.items]
                        snapshots.append(self.server.desk_store.record_cryptocom_trades({
                            "observed_at": observed_at, "start_time_ns": page.start_time_ns,
                            "end_time_ns": page.end_time_ns, "items": items,
                            "provider_payload_sha256": page.payload_sha256, "batch": batch}))
                    all_items = [item for snapshot in snapshots for item in snapshot["items"]]
                    snapshot = {"observed_at": observed_at, "items": all_items, "item_count": len(all_items),
                        "page_count": len(snapshots), "expected_page_count": batch["page_count"],
                        "persistence_complete": len(snapshots) == batch["page_count"],
                        "page_status": "PAGINATED_COMPLETE" if page_set.complete
                            else "PAGINATED_INCOMPLETE", "coverage_complete": page_set.complete,
                        "incomplete_intervals": batch["incomplete_intervals"], "request_count": page_set.request_count,
                        "batch_id": batch["batch_id"], "usable_for_risk": False, "orders_enabled": False}
                else:
                    trades, provider_digest = client.read_trades(api_key, api_secret,
                        start_time_ns=start_ns, end_time_ns=end_ns, limit=100)
                    items = [{"trade_id": item.trade_id, "order_id": item.order_id,
                        "trade_match_id": item.trade_match_id, "client_oid": item.client_oid,
                        "instrument_name": item.instrument_name, "side": item.side, "taker_side": item.taker_side,
                        "quantity": format(item.quantity, "f"), "price": format(item.price, "f"),
                        "fees": format(item.fees, "f"), "fee_currency": item.fee_currency,
                        "occurred_at": item.occurred_at.isoformat(), "timestamp_ns": item.timestamp_ns,
                        "source": item.source, "payload_sha256": item.payload_sha256} for item in trades]
                    snapshot = self.server.desk_store.record_cryptocom_trades({"observed_at": observed_at,
                        "start_time_ns": start_ns, "end_time_ns": end_ns, "items": items,
                        "provider_payload_sha256": provider_digest})
                    snapshot["page_status"] = "PAGE_LIMIT_REACHED" if len(items) == 100 else "ONE_PAGE_READ"
                return self._send_json(200, {"snapshot": snapshot})
            except CryptoComPortfolioError as exc:
                return self._reject_post(503, str(exc))
            except CryptoComCredentialStoreError:
                return self._reject_post(503, "Le coffre Windows a refusé l’accès aux identifiants enregistrés.")
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError, sqlite3.Error):
                return self._reject_post(503, "La lecture ou la conservation des exécutions a échoué.")
        if urlsplit(self.path).path == "/api/desk/risk-limits":
            if not self._local_host() or not self._same_origin():
                return self._reject_post(403, "Origine locale requise.")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold() != "application/json":
                return self._reject_post(415, "JSON requis.")
            try:
                raw_length = self.headers.get("Content-Length", "")
                if not raw_length.isdecimal() or len(raw_length) > 4 or not 1 <= int(raw_length) <= 1024:
                    return self._reject_post(413, "Requête invalide.")
                payload = json.loads(self.rfile.read(int(raw_length)))
                candidate = self.server.desk_store.propose_risk_limits(payload)
                return self._send_json(201, {"candidate": candidate})
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                return self._reject_post(400, str(exc))
        if urlsplit(self.path).path == "/api/cryptocom/portfolio/candidate":
            if not self._local_host() or not self._same_origin():
                return self._reject_post(403, "Origine locale requise.")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold() != "application/json":
                return self._reject_post(415, "JSON requis.")
            try:
                raw_length = self.headers.get("Content-Length", "")
                if not raw_length.isdecimal() or len(raw_length) > 4 or not 1 <= int(raw_length) <= 2048:
                    return self._reject_post(413, "Requête invalide.")
                payload = json.loads(self.rfile.read(int(raw_length)))
                if (not isinstance(payload, dict) or set(payload) != {"base_currency"}
                        or not isinstance(payload["base_currency"], str)
                        or re.fullmatch(r"[A-Z]{3}", payload["base_currency"]) is None):
                    return self._reject_post(400, "Choisis explicitement une devise de base à trois lettres.")
                result = record_spot_portfolio_valuation_candidate(self.server, payload["base_currency"])
                return self._send_json(200, result)
            except CryptoComCandidateError as exc:
                return self._reject_post(409, str(exc))
            except CryptoComMarketOverviewError:
                return self._reject_post(503, "L’univers actuel des paires Spot Crypto.com est indisponible.")
            except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
                return self._reject_post(409, "Le relevé ou les preuves locales ne permettent pas cette estimation.")
        if urlsplit(self.path).path == "/api/market-data/connect-all":
            if not self._local_host() or not self._same_origin():
                return self._reject_post(403, "Origine locale requise.")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold() != "application/json":
                return self._reject_post(415, "JSON requis.")
            try:
                raw_length = self.headers.get("Content-Length", "")
                if (not raw_length.isdecimal() or len(raw_length) > 4
                        or not 1 <= int(raw_length) <= 1024):
                    return self._reject_post(413, "Requête invalide.")
                payload = json.loads(self.rfile.read(int(raw_length)))
                if (not isinstance(payload, dict) or set(payload) != {"provider", "all_spot"}
                        or payload.get("provider") != "cryptocom" or payload.get("all_spot") is not True):
                    return self._reject_post(400, "Confirmation des marchés Spot invalide.")
                if self.server.active_market_provider is not None:
                    return self._reject_post(409, "Déconnecte le flux actif avant d’en ouvrir un autre.")
                overview = self.server.cryptocom_market_overview.get_overview()
                symbols = [row["symbol"] for row in overview.get("markets", [])
                           if isinstance(row, dict) and isinstance(row.get("symbol"), str)]
                if not symbols:
                    return self._reject_post(503, "Aucune paire Spot Crypto.com n’est disponible.")
                state = self.server.cryptocom_market_stream.connect(",".join(symbols))
                self.server.active_market_provider = "cryptocom"
                self.server.live_signal_scanner.start(symbols, timeframe="5m")
                return self._send_json(202, state)
            except CryptoComMarketOverviewError:
                return self._reject_post(503, "La liste actuelle des marchés Spot est indisponible.")
            except CryptoComMarketDataError as exc:
                return self._reject_post(400, str(exc))
            except (json.JSONDecodeError, TypeError, ValueError):
                return self._reject_post(400, "La demande de flux complet est invalide.")
        if urlsplit(self.path).path == "/api/market-data/disconnect":
            if not self._local_host() or not self._same_origin():
                return self._reject_post(403, "Origine locale requise.")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold() != "application/json":
                return self._reject_post(415, "JSON requis.")
            try:
                raw_length = self.headers.get("Content-Length", "")
                if not raw_length.isdecimal() or len(raw_length) > 1024:
                    return self._reject_post(413, "Requête invalide.")
                payload = json.loads(self.rfile.read(int(raw_length)))
                if payload != {"confirm_disconnect": True}:
                    return self._reject_post(400, "Confirmation de déconnexion invalide.")
                scan = self.server.live_signal_scanner.stop()
                stream = self.server.cryptocom_market_stream.disconnect()
                self.server.active_market_provider = None
                return self._send_json(200, {"stream": stream, "scanner": scan,
                                             "orders_enabled": False})
            except (json.JSONDecodeError, TypeError, ValueError):
                return self._reject_post(400, "La demande de déconnexion est invalide.")
        if urlsplit(self.path).path in {"/api/market-evidence", "/api/market-evidence/assess"}:
            if not self._local_host() or not self._same_origin():
                return self._reject_post(403, "Origine locale requise.")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold() != "application/json":
                return self._reject_post(415, "JSON requis.")
            try:
                raw_length = self.headers.get("Content-Length", "")
                if not raw_length.isdecimal() or len(raw_length) > 4 or not 1 <= int(raw_length) <= 1024:
                    return self._reject_post(413, "Requête invalide.")
                payload = json.loads(self.rfile.read(int(raw_length)))
                if not isinstance(payload, dict):
                    return self._reject_post(400, "Contenu invalide.")
                if urlsplit(self.path).path == "/api/market-evidence":
                    if set(payload) != {"symbol"} or not isinstance(payload["symbol"], str):
                        return self._reject_post(400, "Paire Spot invalide.")
                    overview = self.server.cryptocom_market_overview.get_overview()
                    spot_symbols = {row.get("symbol") for row in overview.get("markets", [])
                                    if isinstance(row, dict) and isinstance(row.get("symbol"), str)}
                    if payload["symbol"] not in spot_symbols:
                        return self._reject_post(400, "Cette paire n’appartient pas aux marchés Spot Crypto.com disponibles.")
                    state = self.server.cryptocom_market_stream.state()
                    tick = next((row for row in state.get("quotes", [])
                        if isinstance(row, dict) and row.get("symbol") == payload["symbol"]), None)
                    if tick is None:
                        return self._reject_post(409, "Aucun tick temps réel actuel pour cette paire.")
                    from decimal import Decimal
                    from invest_ai.data.cryptocom_market_readonly import CryptoComMarketTick
                    try:
                        received = datetime.fromisoformat(str(tick["received_at"]).replace("Z", "+00:00"))
                        observed = datetime.fromisoformat(str(tick["provider_timestamp"]).replace("Z", "+00:00"))
                        age = (datetime.now(timezone.utc) - received.astimezone(timezone.utc)).total_seconds()
                        if (not 0 <= age <= 5 or tick.get("quote_status") != "COMPLETE"
                                or tick.get("instrument_type") not in {"Spot", "Exchange ticker"}
                                or tick.get("exchange") != "Crypto.com Exchange"):
                            return self._reject_post(409, "Le bid/ask n’est pas complet et frais pour Risk.")
                        typed = CryptoComMarketTick(
                            symbol=tick["symbol"], price=Decimal(tick["last_trade_price"]),
                            best_bid=Decimal(tick["best_bid"]), best_ask=Decimal(tick["best_ask"]),
                            best_bid_size=Decimal(tick["best_bid_size"]) if tick.get("best_bid_size") is not None else None,
                            best_ask_size=Decimal(tick["best_ask_size"]) if tick.get("best_ask_size") is not None else None,
                            quote_status=tick["quote_status"], provider_timestamp=observed,
                            received_at=received, currency=tick["currency"], exchange=tick["exchange"],
                            instrument_type=tick["instrument_type"], source=tick["source"],
                            payload_sha256=tick["payload_sha256"],
                        )
                    except (KeyError, TypeError, ValueError):
                        return self._reject_post(409, "Tick Crypto.com incomplet ou invalide.")
                    record = self.server.market_evidence_store.record(typed, max_age_seconds=5)
                    assessment = self.server.market_evidence_store.assess(
                        record.snapshot_id, as_of=datetime.now(timezone.utc), max_age_seconds=5)
                    return self._send_json(201, {"snapshot_id": record.snapshot_id,
                        "snapshot_sha256": record.snapshot_sha256, "instrument_id": record.snapshot.instrument_id,
                        "symbol": record.snapshot.symbol, "price": str(record.snapshot.price),
                        "currency": record.snapshot.currency, "venue": record.snapshot.venue,
                        "observed_at": record.snapshot.observed_at.isoformat(),
                        "received_at": record.snapshot.received_at.isoformat(),
                        "eligible_for_risk_price_candidate": assessment.eligible,
                        "assessment_reasons": list(assessment.reasons),
                        "authority_status": "MARKET_PRICE_CANDIDATE_ONLY",
                        "orders_enabled": False})
                if set(payload) != {"snapshot_id"} or not isinstance(payload["snapshot_id"], str):
                    return self._reject_post(400, "Référence de snapshot invalide.")
                record = self.server.market_evidence_store.get(payload["snapshot_id"])
                assessment = self.server.market_evidence_store.assess(
                    payload["snapshot_id"], as_of=datetime.now(timezone.utc), max_age_seconds=5)
                return self._send_json(200, {"snapshot_id": record.snapshot_id,
                    "symbol": record.snapshot.symbol, "price": str(record.snapshot.price),
                    "currency": record.snapshot.currency, "eligible_for_risk_price_candidate": assessment.eligible,
                    "assessment_reasons": list(assessment.reasons), "authority_status": "MARKET_PRICE_CANDIDATE_ONLY",
                    "orders_enabled": False})
            except CryptoComMarketDataError as exc:
                return self._reject_post(409, str(exc))
            except (json.JSONDecodeError, TypeError, ValueError):
                return self._reject_post(400, "La preuve de marché est invalide.")
        if urlsplit(self.path).path != "/api/desk/watchlist":
            return super().do_POST()
        if not self._local_host() or not self._same_origin():
            return self._reject_post(403, "Origine locale requise.")
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self._reject_post(415, "JSON requis.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 1024:
                return self._reject_post(413, "Requête trop grande.")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or set(payload) != {"symbol", "enabled"}:
                raise ValueError("Champs invalides.")
            watchlist = self.server.desk_store.set_watch(payload["symbol"], payload["enabled"])
            self._send_json(200, {"watchlist": watchlist})
        except (ValueError, TypeError):
            self._send_json(400, {"error": "Favori invalide."})


class _PortfolioObservationStopped(Exception):
    """The observer was stopped before a gated evidence write could begin."""


def record_spot_portfolio_valuation_candidate(server, base_currency: str, *, persist: bool = True,
                                              evidence_write_gate=None):
    """Persist one Spot valuation using the same live evidence checks as the manual route."""
    stored = server.desk_store.latest_cryptocom_portfolio()
    if stored is None:
        raise ValueError("Aucun relevé de portefeuille persistant.")
    snapshot = cryptocom_snapshot_from_persisted(stored)
    overview = server.cryptocom_market_overview.get_overview()
    spot_symbols = {row.get("symbol") for row in overview.get("markets", [])
                    if isinstance(row, dict) and isinstance(row.get("symbol"), str)}
    required_assets = {item.currency for item in snapshot.wallet_balances if item.quantity > 0}
    for order in snapshot.open_orders:
        match = re.fullmatch(r"([A-Z0-9][A-Z0-9.-]{0,15})_([A-Z]{3})", order.instrument_name)
        if match is None:
            raise ValueError("Un ordre ouvert ne correspond pas à une paire Spot valorisable.")
        base_asset, quote_asset = match.groups()
        required_assets.add(quote_asset if order.side == "BUY" else base_asset)
    required_symbols: set[str] = set()
    for asset in required_assets:
        if asset == base_currency:
            continue
        options = (f"{asset}_{base_currency}", f"{base_currency}_{asset}")
        match_symbol = next((symbol for symbol in options if symbol in spot_symbols), None)
        if match_symbol is None:
            raise ValueError(f"Aucune paire Spot Crypto.com ne valorise {asset} en {base_currency}.")
        required_symbols.add(match_symbol)
    stream = server.cryptocom_market_stream.state()
    if stream.get("connection_status") != "CONNECTED":
        raise ValueError("Le flux temps réel Crypto.com n’est pas connecté.")
    tick_by_symbol = {row.get("symbol"): row for row in stream.get("quotes", [])
                      if isinstance(row, dict) and isinstance(row.get("symbol"), str)}
    market_snapshot_ids: dict[str, str] = {}
    for symbol in sorted(required_symbols):
        tick = tick_by_symbol.get(symbol)
        if tick is None or symbol not in spot_symbols:
            raise ValueError(f"Le flux temps réel ne contient pas {symbol}.")
        try:
            typed = server.cryptocom_market_stream.get_tick(symbol)
            if typed is None:
                raise ValueError(f"Le tick vérifié de {symbol} est indisponible.")
            now = datetime.now(timezone.utc)
            age = (now - typed.received_at.astimezone(timezone.utc)).total_seconds()
            if (not 0 <= age <= 30 or tick.get("quote_status") != "COMPLETE"
                    or tick.get("exchange") != "Crypto.com Exchange"
                    or tick.get("instrument_type") not in {"Spot", "Exchange ticker"}):
                raise ValueError(f"Le bid/ask de {symbol} est incomplet ou périmé.")
            write_evidence = lambda: server.market_evidence_store.record(typed, max_age_seconds=30)
            if evidence_write_gate is None:
                record = write_evidence()
            else:
                record = evidence_write_gate(write_evidence)
                if record is None:
                    raise _PortfolioObservationStopped()
            market_snapshot_ids[symbol] = record.snapshot_id
        except _PortfolioObservationStopped:
            raise
        except (KeyError, TypeError, CryptoComMarketDataError) as exc:
            raise ValueError(f"La preuve de marché de {symbol} n’a pas passé la vérification.") from exc
    now = datetime.now(timezone.utc)
    candidate = evaluate_spot_portfolio_candidate(snapshot, market_snapshot_ids,
        server.market_evidence_store.snapshot_store, base_currency=base_currency,
        as_of=now, max_age_seconds=30)
    result = {
        "snapshot_id": candidate.snapshot_id, "as_of": candidate.as_of.isoformat(),
        "base_currency": candidate.base_currency, "total_equity": str(candidate.total_equity),
        "cash_value": str(candidate.cash_value), "spot_assets_value": str(candidate.spot_assets_value),
        "held_asset_values": [{"asset": asset, "value": str(value)}
                               for asset, value in candidate.held_asset_values],
        "order_reservations": [{"order_id": order_id, "value": str(value)}
                                for order_id, value in candidate.order_reservations],
        "market_snapshot_ids": list(candidate.market_snapshot_ids),
        "source_payload_sha256": list(candidate.source_payload_sha256),
        "authority_status": candidate.authority_status, "usable_for_risk": False,
        "usable_for_execution": False, "orders_enabled": False,
    }
    return server.desk_store.record_cryptocom_valuation_candidate(result) if persist else result


def make_server(port=8790, *, data_dir=None, cryptocom_instrument_catalog=None,
                bind_host="127.0.0.1", handler_class=DeskHandler,
                cryptocom_observer_credential_store=None, **kwargs):
    kwargs.setdefault("cryptocom_credential_store", WindowsCryptoComCredentialStore(
        "BUBUS2.0/Crypto.com Exchange/Read-only"))
    server = make_base_server(port, bind_host=bind_host, handler_class=handler_class, **kwargs)
    server.RequestHandlerClass = handler_class
    server.desk_store = DeskStore(Path(data_dir or ROOT / "data") / "desk.sqlite3")
    server.cryptocom_observer_credential_store = cryptocom_observer_credential_store or WindowsCryptoComCredentialStore(
        "BUBUS2.0/Crypto.com Exchange/Observer-Can-Read")
    # Independent OFFLINE-only journal: no order adapter injection and no
    # HTTP write route. Nothing here can submit or authorize an exchange order.
    server.offline_order_journal = OfflineOrderJournal(
        Path(data_dir or ROOT / "data") / "offline-order-journal.sqlite3")
    # Separate, explicitly fictional capital/approval ledger; never injected
    # into CryptoComSpotOrderAdapter or a real OrderAuthorizationGate.
    server.offline_risk_reservations = OfflineRiskReservations(
        Path(data_dir or ROOT / "data") / "offline-risk-reservations.sqlite3")
    # Separate shared database for offline tests of cross-ledger atomicity.
    # No write endpoint is exposed and no adapter or execution gate is connected.
    server.offline_trade_transaction = OfflineTradeTransaction(
        Path(data_dir or ROOT / "data") / "offline-trade-transaction.sqlite3")
    server.live_order_journal = LiveOrderJournal(
        Path(data_dir or ROOT / "data") / "live-order-journal.sqlite3")
    server.market_evidence_store = CryptoComMarketEvidenceStore(str(Path(data_dir or ROOT)))
    server.candles = CandleSource()
    server.cryptocom_market_stream.configure_candlesticks(
        "5m", server.candles.update_live_candle)
    server.cryptocom_instrument_catalog = cryptocom_instrument_catalog or CryptoComInstrumentCatalog()
    server.cryptocom_spot_order_adapter = CryptoComSpotOrderAdapter(
        instrument_verifier=server.cryptocom_instrument_catalog,
        order_journal=server.live_order_journal)
    def collect_portfolio_observation():
        credential_store = server.cryptocom_observer_credential_store
        credentials = credential_store.load() if credential_store is not None and credential_store.available else None
        if credentials is None:
            raise CryptoComCredentialStoreError("Observer credential is unavailable.")
        client = server.cryptocom_portfolio_client or CryptoComReadOnlyPortfolioClient()
        result = _cryptocom_snapshot_payload(client.read_portfolio(credentials.api_key, credentials.api_secret))
        observer = server.cryptocom_portfolio_observer
        if not observer.run_if_active(lambda: server.desk_store.record_cryptocom_portfolio(result)):
            return {"portfolio_recorded": False, "valuation_recorded": False}
        def gate_evidence_write(action):
            captured = []
            if not observer.run_if_active(lambda: captured.append(action())):
                return None
            return captured[0]
        try:
            valuation = record_spot_portfolio_valuation_candidate(
                server, "EUR", persist=False, evidence_write_gate=gate_evidence_write)
        except _PortfolioObservationStopped:
            return {"portfolio_recorded": True, "valuation_recorded": False}
        except (CryptoComCandidateError, CryptoComMarketOverviewError, CryptoComMarketDataError,
                sqlite3.Error, TypeError, ValueError):
            return {"portfolio_recorded": True, "valuation_recorded": False,
                    "valuation_error_code": "MARKET_DATA_UNAVAILABLE"}
        recorded = observer.run_if_active(lambda: server.desk_store.record_cryptocom_valuation_candidate(valuation))
        return {"portfolio_recorded": True, "valuation_recorded": recorded}
    server.cryptocom_portfolio_observer = ReadOnlyPortfolioObserver(collect_portfolio_observation)
    server.live_signal_scanner = LiveSignalScanner(
        server.cryptocom_market_stream, server.cryptocom_instrument_catalog, server.candles)
    return server


def start_local_public_market_tracking(server) -> dict[str, object]:
    """Start credential-free Spot tickers and the bounded live signal scanner."""
    try:
        if server.active_market_provider is not None:
            return server.cryptocom_market_stream.state()
        overview = server.cryptocom_market_overview.get_overview()
        symbols = tuple(row["symbol"] for row in overview.get("markets", [])
                        if isinstance(row, dict) and isinstance(row.get("symbol"), str))
        if not symbols:
            return {"connection_status": "UNAVAILABLE", "orders_enabled": False}
        state = server.cryptocom_market_stream.connect(",".join(symbols))
        server.active_market_provider = "cryptocom"
        server.live_signal_scanner.start(symbols, timeframe="5m")
        return state
    except (CryptoComMarketOverviewError, CryptoComMarketDataError, ValueError, RuntimeError):
        try:
            server.cryptocom_market_stream.disconnect()
        except Exception:
            pass
        server.active_market_provider = None
        return {"connection_status": "UNAVAILABLE", "orders_enabled": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description="BUBUS2.0 — poste de trading")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    server = make_server(args.port)
    print(f"BUBUS2.0 : http://127.0.0.1:{server.server_port}/", flush=True)
    tracking = start_local_public_market_tracking(server)
    print(f"Suivi Crypto.com : {tracking.get('connection_status')} · "
          f"{len(tracking.get('symbols', []))} paires · aucun ordre activé.", flush=True)
    if not args.no_browser:
        threading.Timer(.5, lambda: webbrowser.open(f"http://127.0.0.1:{server.server_port}/")).start()
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
