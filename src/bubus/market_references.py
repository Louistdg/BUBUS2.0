"""Repères datés pour le relevé envoyé à la discussion."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import math
import threading
import time
from urllib.parse import quote

from bubus.watch_risk import YAHOO_CHART, _get_json

BENCHMARKS = (
    ("AAPL", "Apple", "action"), ("MSFT", "Microsoft", "action"),
    ("AIR.PA", "Airbus", "action"), ("SPY", "SPDR S&P 500", "ETF"),
    ("QQQ", "Invesco QQQ", "ETF"), ("VT", "Vanguard Total World", "ETF"),
    ("^FCHI", "CAC 40", "indice"), ("^GSPC", "S&P 500", "indice"),
    ("^IXIC", "Nasdaq Composite", "indice"),
    ("BTC-USD", "Bitcoin", "crypto"), ("ETH-USD", "Ethereum", "crypto"),
)


def dated_prices(symbol, fetch=_get_json):
    url = YAHOO_CHART + quote(symbol, safe="") + "?range=2y&interval=1d"
    data = fetch(url)["chart"]["result"][0]
    meta = data["meta"]
    closes = data["indicators"]["quote"][0]["close"]
    history = {}
    for stamp, value in zip(data["timestamp"], closes):
        if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
            history[datetime.fromtimestamp(stamp, timezone.utc).date()] = value
    observed = datetime.fromtimestamp(meta["regularMarketTime"], timezone.utc)
    price = float(meta["regularMarketPrice"])
    if not math.isfinite(price) or price <= 0:
        raise ValueError("Cours indisponible")
    details = []
    for days, label in ((7, "7j"), (30, "30j"), (365, "1an")):
        target = observed.date() - timedelta(days=days)
        day = max((d for d in history if target - timedelta(days=7) <= d <= target), default=None)
        if day:
            prior = history[day]
            details.append(f"{label} {day} {prior:.7g} ({(price / prior - 1) * 100:+.2f} %)")
    return f"{price:.8g} {meta.get('currency', '')} au {observed:%Y-%m-%d %H:%M UTC} | " + " | ".join(details)


class MarketReferences:
    def __init__(self, reader=dated_prices):
        self.reader = reader
        self.cache = {}
        self.lock = threading.Lock()

    def _line(self, item):
        symbol, name, kind = item
        try:
            result = self.reader(symbol)
        except (ValueError, KeyError, IndexError, TypeError, OSError):
            result = "source indisponible"
        return f"{kind} · {name} ({symbol}) : {result}"

    def lines(self):
        with self.lock:
            now = time.monotonic()
            if now >= self.cache.get("expires", 0):
                with ThreadPoolExecutor(max_workers=6) as pool:
                    lines = list(pool.map(self._line, BENCHMARKS))
                self.cache = {"expires": now + 300, "lines": lines}
            return self.cache["lines"]

def conversion_quotes():
    """Taux indicatifs en unités de devise cible pour un euro."""
    result = {}
    for currency, symbol in (("USD", "EURUSD=X"), ("BTC", "BTC-EUR")):
        try:
            chart = _get_json(YAHOO_CHART + quote(symbol, safe="") + "?range=1d&interval=1d")
            meta = chart["chart"]["result"][0]["meta"]
            price = float(meta["regularMarketPrice"])
            if math.isfinite(price) and price > 0:
                result[currency] = {"rate": price if currency == "USD" else 1 / price,
                                    "timestamp": int(meta["regularMarketTime"])}
        except (ValueError, TypeError, IndexError, KeyError, OSError):
            continue
    return result
