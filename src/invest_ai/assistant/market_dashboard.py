"""Local-first market dashboard for real provider data and read-only accounts."""
from __future__ import annotations

import argparse
import json
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from invest_ai.data.cryptocom_market_readonly import CryptoComMarketDataError, CryptoComPublicMarketStream
from invest_ai.data.cryptocom_market_overview import CryptoComMarketOverview, CryptoComMarketOverviewError
from invest_ai.data.coingecko_market_overview import CoinGeckoMarketOverview, CoinGeckoMarketOverviewError
from invest_ai.data.twelvedata_readonly import TwelveDataError, TwelveDataReadOnlyQuoteStream
from invest_ai.portfolio.cryptocom_readonly import (
    CryptoComPortfolioError,
    CryptoComPortfolioSnapshot,
    CryptoComReadOnlyPortfolioClient,
)
from invest_ai.portfolio.cryptocom_credential_store import (
    CredentialStore,
    CryptoComCredentialStoreError,
    WindowsCryptoComCredentialStore,
)
from invest_ai.portfolio.ibkr_readonly import (
    IBKRLocalGatewayTransport,
    IBKRPortfolioError,
    IBKRReadOnlyPortfolioClient,
    IBKRReadOnlyPortfolioSnapshot,
)


APP_DIR = Path(__file__).resolve().parent
MAX_BODY_BYTES = 8192


class MarketDashboardServer(ThreadingHTTPServer):
    def server_close(self) -> None:
        observer = getattr(self, "cryptocom_portfolio_observer", None)
        if observer is not None:
            # A private read can take several bounded network timeouts. Do not close
            # SQLite or streams while its callback can still persist a snapshot.
            observer.stop(timeout=None)
        scanner = getattr(self, "live_signal_scanner", None)
        if scanner is not None:
            scanner.stop()
        for name in ("market_quote_stream", "cryptocom_market_stream"):
            quotes = getattr(self, name, None)
            if quotes is not None:
                quotes.disconnect()
        super().server_close()


class MarketDashboardHandler(BaseHTTPRequestHandler):
    server_version = "InvestAIMarketDashboard/1"
    sys_version = ""

    def log_message(self, _format: str, *_args: object) -> None:
        # Request bodies and API credentials are intentionally never logged.
        return

    def _local_host(self) -> bool:
        return self.headers.get("Host", "").casefold() == f"127.0.0.1:{self.server.server_port}".casefold()

    def _same_origin(self) -> bool:
        expected = f"http://127.0.0.1:{self.server.server_port}"
        return self.headers.get("Origin") == expected

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self' https://*.tradingview.com https://*.tradingview-widget.com; style-src 'self' 'unsafe-inline' https://*.tradingview.com; script-src 'self' https://s3.tradingview.com https://widgets.tradingview-widget.com; img-src 'self' data: https://*.tradingview.com https://*.tradingview-widget.com; frame-src https://*.tradingview.com https://*.tradingview-widget.com; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")

    def _reject_post(self, status: int, message: str) -> None:
        self.close_connection = True
        self._send_json(status, {"error": message})

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        if not self._local_host():
            self._send_json(403, {"error": "Local access only."})
            return
        path = urlsplit(self.path).path
        if path == "/api/market-data/state":
            if not self._same_origin_read():
                self._send_json(403, {"error": "Same-origin local market state required."})
                return
            provider = self.server.active_market_provider
            if provider == "cryptocom":
                state = self.server.cryptocom_market_stream.state()
            else:
                state = self.server.market_quote_stream.state()
            self._send_json(200, state)
            return
        if path == "/api/market-data/crypto-overview":
            if not self._same_origin_read():
                self._send_json(403, {"error": "Same-origin local market overview required."})
                return
            try:
                self._send_json(200, self.server.cryptocom_market_overview.get_overview())
            except CryptoComMarketOverviewError as exc:
                self._send_json(503, {"error": str(exc)})
            return
        if path == "/api/market-data/coingecko-overview":
            if not self._same_origin_read():
                self._send_json(403, {"error": "Same-origin local market overview required."})
                return
            try:
                self._send_json(200, self.server.coingecko_market_overview.get_overview())
            except CoinGeckoMarketOverviewError as exc:
                self._send_json(503, {"error": str(exc)})
            return
        if path == "/api/ibkr/portfolio/config":
            self._send_json(200, {"configured": self.server.ibkr_portfolio_client is not None})
            return
        if path == "/api/ibkr/portfolio/accounts":
            if not self._same_origin_read():
                self._send_json(403, {"error": "Same-origin local account discovery required."})
                return
            client = self.server.ibkr_portfolio_client
            if client is None:
                self._send_json(409, {"error": "IBKR read-only adapter is not configured."})
                return
            try:
                accounts = client.list_accounts()
            except (IBKRPortfolioError, OSError):
                self._send_json(503, {"error": "Unable to read accounts from the authenticated local IBKR Gateway."})
                return
            self._send_json(200, {"accounts": [{
                "account_id": item.account_id, "display_name": item.display_name,
                "currency": item.currency,
            } for item in accounts]})
            return
        if path == "/api/cryptocom/portfolio/config":
            if not self._same_origin_read():
                self._send_json(403, {"error": "Same-origin local credential status required."})
                return
            store = self.server.cryptocom_credential_store
            available = bool(store is not None and store.available)
            try:
                persisted = bool(available and store.has_credentials())
            except CryptoComCredentialStoreError:
                self._send_json(503, {"error": "Windows credential vault could not be read."})
                return
            self._send_json(200, {"available": available, "credentials_persisted": persisted})
            return
        assets = {
            "/assets/market-dashboard.css": ("market_dashboard.css", "text/css; charset=utf-8"),
            "/assets/market-dashboard.js": ("market_dashboard.js", "text/javascript; charset=utf-8"),
        }
        if path in assets:
            filename, content_type = assets[path]
            self._send_file(APP_DIR / filename, content_type)
            return
        if path != "/":
            self._send_json(404, {"error": "Not found."})
            return
        self._send_file(APP_DIR / "market_dashboard.html", "text/html; charset=utf-8")

    def _same_origin_read(self) -> bool:
        expected = f"http://127.0.0.1:{self.server.server_port}"
        origin = self.headers.get("Origin")
        fetch_site = self.headers.get("Sec-Fetch-Site")
        return origin in {None, expected} and fetch_site in {None, "same-origin", "none"}

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(503, {"error": "Dashboard resource is unavailable."})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        if not self._local_host():
            self._reject_post(403, "Local access only.")
            return
        path = urlsplit(self.path).path
        routes = {
            "/api/market-data/connect", "/api/market-data/disconnect",
            "/api/ibkr/portfolio/import", "/api/cryptocom/portfolio/import",
            "/api/cryptocom/portfolio/refresh", "/api/cryptocom/portfolio/forget",
        }
        if path not in routes:
            self._reject_post(404, "Not found.")
            return
        if not self._same_origin():
            self._reject_post(403, "Same-origin confirmation required.")
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold() != "application/json":
            self._reject_post(415, "JSON request required.")
            return
        raw_length = self.headers.get("Content-Length", "")
        if not re.fullmatch(r"[0-9]{1,6}", raw_length):
            self._reject_post(400, "Invalid request size.")
            return
        length = int(raw_length)
        if not 1 <= length <= MAX_BODY_BYTES:
            self._reject_post(413, "Request is too large.")
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                self._reject_post(400, "Request fields are invalid.")
                return
            if path == "/api/market-data/connect":
                provider = payload.get("provider", "twelvedata")
                if not isinstance(provider, str) or provider not in {"cryptocom", "twelvedata"}:
                    self._send_json(400, {"error": "Choose a supported market-data provider."})
                    return
                if provider == "cryptocom":
                    if set(payload) != {"provider", "symbols"}:
                        self._send_json(400, {"error": "Crypto.com public market data accepts instruments only; account credentials are not used."})
                        return
                elif set(payload) not in ({"provider", "api_key", "symbols"}, {"api_key", "symbols"}):
                    self._send_json(400, {"error": "Enter a Twelve Data API key and symbols to follow."})
                    return
                if self.server.active_market_provider is not None:
                    self._send_json(409, {"error": "Disconnect the active market stream before connecting another source."})
                    return
                if provider == "cryptocom":
                    state = self.server.cryptocom_market_stream.connect(payload["symbols"])
                else:
                    state = self.server.market_quote_stream.connect(payload["api_key"], payload["symbols"])
                self.server.active_market_provider = provider
                self._send_json(202, state)
                return
            if path == "/api/market-data/disconnect":
                if payload != {"confirm_disconnect": True}:
                    self._send_json(400, {"error": "Confirm the local market-data disconnection explicitly."})
                    return
                provider = self.server.active_market_provider
                if provider == "cryptocom":
                    state = self.server.cryptocom_market_stream.disconnect()
                else:
                    state = self.server.market_quote_stream.disconnect()
                self.server.active_market_provider = None
                self._send_json(200, state)
                return
            if path == "/api/ibkr/portfolio/import":
                if set(payload) != {"account_id"} or not isinstance(payload["account_id"], str):
                    self._send_json(400, {"error": "Select one account returned by the local IBKR Gateway."})
                    return
                client = self.server.ibkr_portfolio_client
                if client is None:
                    self._send_json(409, {"error": "IBKR read-only adapter is not configured."})
                    return
                self._send_json(200, _ibkr_snapshot_payload(client.read_portfolio(payload["account_id"])))
                return
            if path == "/api/cryptocom/portfolio/forget":
                if payload != {"forget_saved_credentials": True}:
                    self._send_json(400, {"error": "Confirm the local credential removal explicitly."})
                    return
                store = self.server.cryptocom_credential_store
                if store is None or not store.available:
                    self._send_json(409, {"error": "Windows credential vault is unavailable."})
                    return
                store.delete()
                self._send_json(200, {"credentials_persisted": False, "forgotten": True})
                return
            if path == "/api/cryptocom/portfolio/refresh":
                if payload == {"use_observer_credentials": True}:
                    store = getattr(self.server, "cryptocom_observer_credential_store", None)
                    missing_message = "Aucune clé observateur dédiée n’est enregistrée dans le coffre Windows."
                elif payload == {"use_saved_credentials": True}:
                    store = self.server.cryptocom_credential_store
                    missing_message = "Aucun identifiant Crypto.com n’est enregistré sur ce PC."
                else:
                    self._send_json(400, {"error": "Confirm a refresh using one explicit saved read credential."})
                    return
                if store is None or not store.available:
                    self._send_json(409, {"error": "Windows credential vault is unavailable."})
                    return
                credentials = store.load()
                if credentials is None:
                    self._send_json(409, {"error": missing_message})
                    return
                client = self.server.cryptocom_portfolio_client or CryptoComReadOnlyPortfolioClient()
                result = _cryptocom_snapshot_payload(client.read_portfolio(
                    credentials.api_key, credentials.api_secret,
                ))
                result["credentials_persisted"] = True
                self._send_json(200, result)
                return
            allowed_import_fields = (
                {"api_key", "api_secret"},
                {"api_key", "api_secret", "remember_credentials"},
            )
            if set(payload) not in allowed_import_fields:
                self._send_json(400, {"error": "Saisis la clé API Crypto.com Exchange et son secret."})
                return
            remember = payload.get("remember_credentials", False)
            if type(remember) is not bool:
                self._send_json(400, {"error": "Le choix de conservation des identifiants est invalide."})
                return
            client = self.server.cryptocom_portfolio_client or CryptoComReadOnlyPortfolioClient()
            result = _cryptocom_snapshot_payload(client.read_portfolio(payload["api_key"], payload["api_secret"]))
            store = self.server.cryptocom_credential_store
            if remember:
                if store is None or not store.available:
                    result["credential_warning"] = "Lecture réussie, mais le coffre Windows n’est pas disponible; les identifiants n’ont pas été enregistrés."
                else:
                    try:
                        store.store(payload["api_key"], payload["api_secret"])
                        result["credentials_persisted"] = True
                    except CryptoComCredentialStoreError:
                        result["credentials_persisted"] = False
                        result["credential_warning"] = "Lecture réussie, mais Windows n’a pas enregistré les identifiants."
            else:
                result["credentials_persisted"] = False
            self._send_json(200, result)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "Request JSON is invalid."})
        except TwelveDataError as exc:
            status = 503 if "optional market-data WebSocket package" in str(exc) else 400
            self._send_json(status, {"error": str(exc)})
        except CryptoComMarketDataError as exc:
            status = 503 if "optional market-data WebSocket package" in str(exc) else 400
            self._send_json(status, {"error": str(exc)})
        except CryptoComPortfolioError as exc:
            self._send_json(503, {"error": str(exc)})
        except CryptoComCredentialStoreError:
            self._send_json(503, {"error": "The Windows credential vault is unavailable or invalid."})
        except IBKRPortfolioError:
            self._send_json(503, {"error": "IBKR portfolio data failed validation; no snapshot was imported."})
        except Exception:
            # Never pass provider exceptions or request data back to the browser.
            self._send_json(503, {"error": "Read-only provider request failed; no account data was retained."})

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler contract
        if not self._local_host():
            self._send_json(403, {"error": "Local access only."})
            return
        if urlsplit(self.path).path == "/":
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self._security_headers()
            self.end_headers()
        else:
            self._send_json(404, {"error": "Not found."})

    def _method_not_allowed(self) -> None:
        self._send_json(405 if self._local_host() else 403,
                        {"error": "Method not allowed." if self._local_host() else "Local access only."})

    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_OPTIONS = _method_not_allowed


def _ibkr_snapshot_payload(snapshot: IBKRReadOnlyPortfolioSnapshot) -> dict[str, object]:
    return {
        "account": {"account_id": snapshot.account.account_id,
                    "display_name": snapshot.account.display_name,
                    "currency": snapshot.account.currency},
        "fetched_at": _iso(snapshot.fetched_at),
        "authority_status": snapshot.authority_status,
        "persistent": snapshot.persistent,
        "usable_for_risk": snapshot.usable_for_risk,
        "usable_for_execution": snapshot.usable_for_execution,
        "metrics": [{"name": item.name, "amount": str(item.amount), "currency": item.currency,
                     "provider_timestamp": _iso(item.provider_timestamp), "source": item.source,
                     "payload_sha256": item.payload_sha256} for item in snapshot.metrics],
        "positions": [{"account_id": item.account_id, "conid": item.conid, "symbol": item.symbol,
                       "asset_class": item.asset_class, "quantity": str(item.quantity),
                       "currency": item.currency,
                       "average_price": str(item.average_price) if item.average_price is not None else None,
                       "market_price": str(item.market_price) if item.market_price is not None else None,
                       "market_value": str(item.market_value) if item.market_value is not None else None,
                       "provider_timestamp": _iso(item.provider_timestamp), "source": item.source,
                       "payload_sha256": item.payload_sha256} for item in snapshot.positions],
    }


def _cryptocom_snapshot_payload(snapshot: CryptoComPortfolioSnapshot) -> dict[str, object]:
    return {
        "provider": snapshot.provider, "source": snapshot.source, "fetched_at": _iso(snapshot.fetched_at),
        "authority_status": snapshot.authority_status, "persistent": snapshot.persistent,
        "usable_for_risk": snapshot.usable_for_risk, "usable_for_execution": snapshot.usable_for_execution,
        "payload_sha256": snapshot.payload_sha256,
        "positions_payload_sha256": snapshot.positions_payload_sha256,
        "balances": [{"currency": item.currency, "available": str(item.available),
                       "total_margin": str(item.total_margin), "initial_margin": str(item.initial_margin),
                       "total_position_im": (None if item.total_position_im is None else str(item.total_position_im)),
                       "total_haircut": (None if item.total_haircut is None else str(item.total_haircut)),
                       "total_cash": str(item.total_cash), "total_borrow": str(item.total_borrow),
                       "isolated_cash": str(item.isolated_cash), "is_liquidating": item.is_liquidating,
                       "has_isolated_positions": item.has_isolated_positions,
                       "source": item.source, "payload_sha256": item.payload_sha256} for item in snapshot.balances],
        "open_orders_payload_sha256": snapshot.open_orders_payload_sha256,
        "open_orders": [{"account_id": item.account_id, "order_id": item.order_id,
                         "client_oid": item.client_oid, "instrument_name": item.instrument_name,
                         "side": item.side, "order_type": item.order_type, "status": item.status,
                         "quantity": str(item.quantity), "cumulative_quantity": str(item.cumulative_quantity),
                         "limit_price": str(item.limit_price) if item.limit_price is not None else None,
                         "created_at": _iso(item.created_at) if item.created_at is not None else None,
                         "updated_at": _iso(item.updated_at) if item.updated_at is not None else None,
                         "source": item.source, "payload_sha256": item.payload_sha256}
                        for item in snapshot.open_orders],
        "wallet_balances": [{"currency": item.currency, "account_currency": item.account_currency,
                              "quantity": str(item.quantity), "market_value": str(item.market_value),
                              "collateral_amount": str(item.collateral_amount),
                              "max_withdrawal_balance": str(item.max_withdrawal_balance),
                              "reserved_quantity": str(item.reserved_quantity), "source": item.source,
                              "payload_sha256": item.payload_sha256} for item in snapshot.wallet_balances],
        "positions": [{"instrument_name": item.instrument_name, "type": item.position_type,
                       "quantity": str(item.quantity), "cost_usd": str(item.cost_usd),
                       "open_position_pnl_usd": str(item.open_position_pnl_usd),
                       "session_pnl_usd": str(item.session_pnl_usd), "updated_at": _iso(item.updated_at),
                       "source": item.source, "payload_sha256": item.payload_sha256}
                      for item in snapshot.positions],
    }


def _iso(value) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_server(port: int = 8776, *, portfolio_ca_file: str | Path | None = None,
                portfolio_client: IBKRReadOnlyPortfolioClient | None = None,
                cryptocom_portfolio_client: CryptoComReadOnlyPortfolioClient | None = None,
                market_quote_stream: TwelveDataReadOnlyQuoteStream | None = None,
                cryptocom_market_stream: CryptoComPublicMarketStream | None = None,
                cryptocom_market_overview: CryptoComMarketOverview | None = None,
                coingecko_market_overview: CoinGeckoMarketOverview | None = None,
                cryptocom_credential_store: CredentialStore | None = None,
                bind_host: str = "127.0.0.1",
                handler_class=MarketDashboardHandler) -> MarketDashboardServer:
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ValueError("Port must be between 0 and 65535.")
    if portfolio_client is not None and portfolio_ca_file is not None:
        raise ValueError("Supply either an IBKR portfolio client or its local trusted CA file, not both.")
    server = MarketDashboardServer((bind_host, port), handler_class)
    server.daemon_threads = True
    server.market_quote_stream = market_quote_stream or TwelveDataReadOnlyQuoteStream()
    server.cryptocom_market_stream = cryptocom_market_stream or CryptoComPublicMarketStream()
    server.cryptocom_market_overview = cryptocom_market_overview or CryptoComMarketOverview()
    server.coingecko_market_overview = coingecko_market_overview or CoinGeckoMarketOverview()
    server.active_market_provider = None
    server.ibkr_portfolio_client = portfolio_client
    server.cryptocom_portfolio_client = cryptocom_portfolio_client
    server.cryptocom_credential_store = cryptocom_credential_store or WindowsCryptoComCredentialStore()
    if portfolio_ca_file is not None:
        server.ibkr_portfolio_client = IBKRReadOnlyPortfolioClient(IBKRLocalGatewayTransport(portfolio_ca_file))
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local read-only market and portfolio dashboard")
    parser.add_argument("--port", type=int, default=8776)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--ibkr-portfolio-ca-file", type=Path,
                        help="Trusted local Gateway CA bundle for explicit IBKR portfolio reads.")
    args = parser.parse_args(argv)
    try:
        server = make_server(args.port, portfolio_ca_file=args.ibkr_portfolio_ca_file)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"INVEST-AI dashboard could not start: {exc}\n")
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"INVEST-AI real market-data dashboard (read-only): {url}")
    print("No provider or account is contacted until you connect it from this local page.")
    print("No orders, transfers, withdrawals, or simulated trading are available.")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url, new=1, autoraise=False)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


__all__ = ["MarketDashboardHandler", "MarketDashboardServer", "make_server", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
