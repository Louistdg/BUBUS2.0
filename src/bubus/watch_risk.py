"""« Ma liste » : cours et note de risque de 1 à 5 pour des cryptos et des actions.

Lecture seule. La note décrit seulement à quel point le prix a bougé ces trois
derniers mois (agitation quotidienne et plus forte baisse). Ce n'est ni un
conseil, ni une prévision.

Sources publiques sans clé :
- cryptos et actions : graphique journalier Yahoo Finance (service public non
  officiel). Une crypto « BTC_USD » est lue sous le nom Yahoo « BTC-USD ».
"""
from __future__ import annotations

import json
import math
import re
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from bubus.store import STOCK_SYMBOL, SYMBOL

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
YAHOO_SEARCH = "https://query1.finance.yahoo.com/v1/finance/search"
LABELS = {1: "Très calme", 2: "Calme", 3: "Moyen", 4: "Agité", 5: "Très agité"}
CACHE_SECONDS = 600
MAX_BODY = 2 * 1024 * 1024
_UA = "Mozilla/5.0 (compatible; BUBUS/2)"


class WatchRiskError(ValueError):
    """Message en français, montrable tel quel."""


def _level(value: float, limits: Iterable[float]) -> int:
    for index, limit in enumerate(limits, start=1):
        if value <= limit:
            return index
    return 5


def risk_profile(closes: list[float], *, kind: str) -> dict:
    """Note de 1 à 5 à partir des clôtures journalières (de la plus ancienne à la plus récente)."""
    closes = [float(c) for c in closes if c is not None and math.isfinite(float(c)) and float(c) > 0]
    if len(closes) < 20:
        raise WatchRiskError("Pas assez d’historique pour calculer le risque.")
    per_year = 365 if kind == "crypto" else 252
    week, month = (7, 30) if kind == "crypto" else (5, 21)
    recent = closes[-(month + 1):]
    returns = [math.log(b / a) for a, b in zip(recent, recent[1:])]
    mean = sum(returns) / len(returns)
    daily = math.sqrt(sum((r - mean) ** 2 for r in returns) / max(1, len(returns) - 1))
    yearly_pct = daily * math.sqrt(per_year) * 100
    window = closes[-(3 * month + 1):]
    peak, drawdown = window[0], 0.0
    for price in window:
        peak = max(peak, price)
        drawdown = max(drawdown, (peak - price) / peak)
    drawdown_pct = drawdown * 100
    score = max(_level(yearly_pct, (20, 35, 60, 90)), _level(drawdown_pct, (10, 20, 35, 50)))

    def change(steps: int) -> float | None:
        return None if len(closes) <= steps else round((closes[-1] / closes[-1 - steps] - 1) * 100, 2)

    return {
        "score": score, "label": LABELS[score],
        "daily_move_pct": round(daily * 100, 2), "yearly_volatility_pct": round(yearly_pct, 1),
        "max_drop_3m_pct": round(drawdown_pct, 1),
        "change_1d_pct": change(1), "change_1w_pct": change(week), "change_1m_pct": change(month),
        "explanation": (f"Bouge d’environ {daily * 100:.1f} % par jour en moyenne. "
                        f"Plus forte baisse sur 3 mois : −{drawdown_pct:.0f} %."),
    }


def _get_json(url: str, opener=urlopen) -> dict:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": _UA})
    with opener(request, timeout=10, context=ssl.create_default_context()) as response:
        raw = response.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        raise ValueError("réponse trop grande")
    data = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if not isinstance(data, dict):
        raise ValueError("réponse invalide")
    return data


def yahoo_daily(symbol: str, opener=urlopen) -> dict:
    """Clôtures journalières sur 6 mois d'une action (ou d'un fonds) Yahoo Finance."""
    data = _get_json(YAHOO_CHART + quote(symbol, safe="") + "?" + urlencode({"range": "6mo", "interval": "1d"}), opener)
    result = (data.get("chart") or {}).get("result") or []
    if not result or not isinstance(result[0], dict):
        raise ValueError("symbole inconnu")
    meta = result[0].get("meta") or {}
    quotes = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
    closes = [c for c in (quotes.get("close") or []) if isinstance(c, (int, float))]
    price = meta.get("regularMarketPrice")
    if isinstance(price, (int, float)) and closes:
        closes[-1] = float(price)
    return {"closes": closes, "price": closes[-1] if closes else None,
            "currency": str(meta.get("currency") or "")[:5],
            "name": str(meta.get("longName") or meta.get("shortName") or symbol)[:80],
            "source": "Yahoo Finance"}


def yahoo_crypto_symbol(symbol: str) -> str:
    """« BTC_USD » (format BUBUS) devient « BTC-USD » (format Yahoo). USDT et USDC sont lus en USD."""
    base, _, quote_currency = symbol.rpartition("_")
    if not base or quote_currency not in {"USD", "USDT", "USDC", "EUR"}:
        raise WatchRiskError("Cette crypto n’a pas de cours en USD ou en EUR.")
    return f"{base}-{'EUR' if quote_currency == 'EUR' else 'USD'}"


def yahoo_crypto_daily(symbol: str, opener=urlopen) -> dict:
    return yahoo_daily(yahoo_crypto_symbol(symbol), opener)


def yahoo_search(text: str, opener=urlopen) -> list[dict]:
    data = _get_json(YAHOO_SEARCH + "?" + urlencode({"q": text, "quotesCount": 8, "newsCount": 0}), opener)
    found = []
    for item in data.get("quotes") or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper()
        if item.get("quoteType") == "CRYPTOCURRENCY":
            base, _, quote_currency = symbol.rpartition("-")
            pair = f"{base}_{quote_currency}"
            if quote_currency in {"USD", "EUR"} and SYMBOL.fullmatch(pair):
                found.append({"kind": "crypto", "symbol": pair,
                              "name": str(item.get("shortname") or base)[:80].removesuffix(f" {quote_currency}"),
                              "place": "Crypto"})
            continue
        if item.get("quoteType") not in {"EQUITY", "ETF", "INDEX", "MUTUALFUND"}:
            continue
        if not STOCK_SYMBOL.fullmatch(symbol):
            continue
        found.append({"kind": "action", "symbol": symbol,
                      "name": str(item.get("longname") or item.get("shortname") or symbol)[:80],
                      "place": str(item.get("exchDisp") or "")[:40]})
    return found[:6]


class WatchRisk:
    def __init__(self, *, crypto_daily: Callable[[str], dict] = yahoo_crypto_daily,
                 stock_daily: Callable[[str], dict] = yahoo_daily,
                 searcher: Callable[[str], list[dict]] = yahoo_search,
                 clock: Callable[[], float] = time.monotonic):
        self.crypto_daily = crypto_daily
        self.stock_daily = stock_daily
        self.searcher = searcher
        self.clock = clock
        self._cache: dict[tuple[str, str], tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def _fetch(self, kind: str, symbol: str, name: str) -> dict:
        if kind == "crypto":
            data = self.crypto_daily(symbol)
        else:
            data = self.stock_daily(symbol)
        profile = risk_profile(data["closes"], kind=kind)
        return {"kind": kind, "symbol": symbol, "name": name or data["name"], "price": data["price"],
                "currency": data["currency"], "source": data["source"], **profile}

    def describe(self, kind: str, symbol: str, name: str = "") -> dict:
        key = (kind, symbol)
        with self._lock:
            cached = self._cache.get(key)
            if cached and self.clock() - cached[0] < CACHE_SECONDS:
                return dict(cached[1], name=name or cached[1]["name"])
        try:
            result = self._fetch(kind, symbol, name)
        except WatchRiskError as exc:
            return {"kind": kind, "symbol": symbol, "name": name or symbol, "error": str(exc)}
        except Exception:
            return {"kind": kind, "symbol": symbol, "name": name or symbol,
                    "error": "Cours indisponible pour le moment. Réessaie dans quelques minutes."}
        with self._lock:
            if len(self._cache) > 500:
                self._cache.clear()
            self._cache[key] = (self.clock(), result)
        return result

    def describe_all(self, items: list[dict]) -> list[dict]:
        if not items:
            return []
        with ThreadPoolExecutor(max_workers=min(8, len(items))) as pool:
            return list(pool.map(lambda item: self.describe(item["kind"], item["symbol"], item.get("name", "")), items))

    def search(self, text: str) -> list[dict]:
        text = " ".join(str(text).split())[:40]
        if len(text) < 2:
            return []
        try:
            return self.searcher(text)
        except Exception:
            raise WatchRiskError("La recherche ne répond pas. Réessaie dans un moment.") from None
