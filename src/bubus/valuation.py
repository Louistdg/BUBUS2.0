"""Valorisation indicative des avoirs Revolut X, en euros."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from threading import Lock
import time
from urllib.parse import quote

from bubus import revolutx
from bubus.watch_risk import YAHOO_CHART, _get_json

_lock = Lock()
_cached = None
_expires = 0


def positive(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number > 0 else None
    except InvalidOperation:
        return None


def yahoo(symbol):
    try:
        data = _get_json(YAHOO_CHART + quote(symbol, safe="") + "?range=1d&interval=1d")
        meta = data["chart"]["result"][0]["meta"]
        return positive(meta["regularMarketPrice"]), int(meta["regularMarketTime"]), meta["currency"]
    except (KeyError, IndexError, OSError, ValueError, TypeError):
        return None, None, None


_stock_lock = Lock()
_stock_cache = []
_stock_expiry = 0
STOCKS = {"AAPL": "Apple", "MSFT": "Microsoft", "AIR.PA": "Airbus",
          "NVDA": "Nvidia", "OR.PA": "L’Oréal", "MC.PA": "LVMH"}


def stock_market():
    global _stock_cache, _stock_expiry
    with _stock_lock:
        if time.monotonic() >= _stock_expiry:
            with ThreadPoolExecutor(max_workers=6) as pool:
                quotes = pool.map(yahoo, STOCKS)
                _stock_cache = [
                    dict(symbol=symbol, name=name, price=str(price) if price else None,
                         currency=currency, stamp=stamp)
                    for (symbol, name), (price, stamp, currency) in zip(STOCKS.items(), quotes)
                ]
            _stock_expiry = time.monotonic() + 55
        return _stock_cache


def valuation():
    global _cached, _expires
    with _lock:
        if _cached and time.monotonic() < _expires:
            return _cached
        wallet = revolutx.snapshot()
        if not wallet["connected"]:
            return wallet
        with ThreadPoolExecutor(max_workers=5) as pool:
            symbols = ["EURUSD=X", "GBPEUR=X", "AAPL", "MSFT", "AIR.PA"]
            jobs = {s: pool.submit(yahoo, s) for s in symbols}
            try:
                ticks = _get_json("https://revx.revolut.com/api/1.0/public/tickers?region=EEA")
                ticktime = int(ticks["metadata"]["timestamp"]) // 1000
                mids = {r["symbol"]: positive(r.get("mid")) for r in ticks["data"]}
            except (KeyError, TypeError, OSError, ValueError):
                mids, ticktime = {}, None
            quotes = {s: job.result() for s, job in jobs.items()}
        eurusd, fxstamp, _ = quotes["EURUSD=X"]
        gbpeur, gbpstamp, _ = quotes["GBPEUR=X"]
        total = Decimal(0)
        missing = []
        market = []
        for row in wallet["balances"]:
            c = row["currency"]
            value = None
            rate, stamp, source = None, None, ""
            if c == "EUR":
                rate, source = Decimal(1), "Euro"
            elif c == "USD" and eurusd:
                rate, stamp, source = 1 / eurusd, fxstamp, "Yahoo Finance · change"
            elif c == "GBP" and gbpeur:
                rate, stamp, source = gbpeur, gbpstamp, "Yahoo Finance · change"
            elif mids.get(c + "/EUR"):
                rate, stamp, source = mids[c + "/EUR"], ticktime, "Revolut X"
            elif mids.get(c + "/USD") and eurusd:
                rate, stamp, source = mids[c + "/USD"] / eurusd, min(ticktime,fxstamp), "Revolut X · change Yahoo"
            try:
                qty = Decimal(row["total"])
                if qty.is_finite():
                    value = qty * rate if rate else (Decimal(0) if qty == 0 else None)
            except InvalidOperation:
                pass
            if value is None:
                missing.append(c)
            else:
                total += value
            row["eur"] = str(value) if value is not None else None
            if rate and c != "EUR":
                market.append(dict(symbol=c, price=str(rate), currency="EUR", stamp=stamp, source=source))
        for s in symbols[2:]:
            price, stamp, currency = quotes[s]
            market.append(dict(symbol=s, price=str(price) if price else None, currency=currency, stamp=stamp, source="Yahoo Finance · action"))
        wallet.update(total_eur=None if missing else str(total), missing=missing, market=market)
        _cached, _expires = wallet, time.monotonic()+20
        return wallet
