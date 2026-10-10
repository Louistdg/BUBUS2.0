"""Deterministic historical strategy research; never creates trading authority."""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from statistics import median


class ResearchError(ValueError):
    pass


def _bps(value: object, label: str, default: int) -> int:
    if value in (None, ""):
        return default
    if type(value) is int and 0 <= value <= 1000:
        return value
    raise ResearchError(f"{label} doit être un nombre entier compris entre 0 et 1000 bps.")


def compare_candles(candles: object, *, commission_bps: object = None, slippage_bps: object = None,
                    test_percent: object = 30, timeframe: str = "1h",
                    evaluation_start_ms: int | None = None,
                    evaluation_end_ms: int | None = None,
                    breakout_lookback: int = 3,
                    relative_volume_multiple: Decimal | str = Decimal("2")) -> dict[str, object]:
    """Compare research rules over contiguous candles with explicit time splits.

    The capital is a normalized index (1 unit), never a user balance or order size.
    Historical breakout uses the price/volume core only; historical book data is
    unavailable, so spread and top-of-book filters are not evaluated.
    """
    if not isinstance(candles, list) or not 6 <= len(candles) <= 5000:
        raise ResearchError("Il faut entre 6 et 5 000 bougies pour le test chronologique.")
    fee = _bps(commission_bps, "Commission", 50)
    slip = _bps(slippage_bps, "Glissement", 5)
    if type(test_percent) is str and test_percent.isdecimal():
        test_percent = int(test_percent)
    if type(test_percent) is not int or not 10 <= test_percent <= 50:
        raise ResearchError("La période de test doit représenter de 10 à 50 % des bougies.")
    annualization_bars = {"5m": 365 * 24 * 12, "15m": 365 * 24 * 4,
                          "1h": 365 * 24, "4h": 365 * 6, "1D": 365}
    if not isinstance(timeframe, str) or timeframe not in annualization_bars:
        raise ResearchError("Période de bougies non prise en charge.")
    bars = []
    prior = 0
    for row in candles:
        try:
            stamp = row["timestamp"]
            values = {key: Decimal(str(row[key])) for key in ("o", "h", "l", "c", "v")}
            if type(stamp) is not int or stamp <= prior or any(not x.is_finite() for x in values.values()):
                raise ValueError()
            if min(values[k] for k in ("o", "h", "l", "c")) <= 0 or values["v"] < 0:
                raise ValueError()
            if values["h"] < max(values["o"], values["c"], values["l"]) or values["l"] > min(values["o"], values["c"]):
                raise ValueError()
        except (KeyError, TypeError, ValueError, ArithmeticError):
            raise ResearchError("Les bougies reçues sont incomplètes, désordonnées ou invalides.") from None
        prior = stamp
        bars.append({"timestamp": stamp, **values})

    test_count = max(3, len(bars) * test_percent // 100)
    split = len(bars) - test_count
    if split < 3:
        raise ResearchError("Il faut au moins trois bougies avant la période de test.")
    folds = []
    sizes = [test_count // 3 + (1 if i < test_count % 3 else 0) for i in range(3)]
    cursor = split
    for size in sizes:
        folds.append((cursor, cursor + size))
        cursor += size

    # All arithmetic is Decimal; costs are applied on every modeled fill.
    training = bars[:split]
    strategies = {
        "do_nothing": lambda history, held: False,
        "buy_hold": lambda history, held: True,
        "sma_cross": lambda history, held: len(history) >= 10 and
            sum((x["c"] for x in history[-3:]), Decimal(0)) / 3 >
            sum((x["c"] for x in history[-10:]), Decimal(0)) / 10,
        "breakout": lambda history, held: _breakout(history, held),
    }
    results = []
    for strategy_id, target in strategies.items():
        cash, units, equity, highwater, max_dd = Decimal(1), Decimal(0), Decimal(1), Decimal(1), Decimal(0)
        fees_paid, fills, invested = Decimal(0), 0, 0
        holding_bars: list[int] = []
        entry_bar_index: int | None = None
        training_target = target(training, False) if strategy_id != "buy_hold" else True
        # A training signal is observed at the last training close and fills
        # at the first test open, with its costs charged to the test interval.
        if training_target:
            first = bars[split]["o"] * (1 + Decimal(slip) / 10000)
            gross = cash / (1 + Decimal(fee) / 10000)
            units = (gross / first).quantize(Decimal("0.000000000000000001"), rounding=ROUND_DOWN)
            paid = units * first * Decimal(fee) / 10000
            cash -= units * first + paid; fees_paid += paid; fills += 1
            entry_bar_index = split
        test_curve = []
        full_history = bars[:split]
        last_equity = Decimal(1)
        for idx in range(split, len(bars)):
            bar = bars[idx]
            if idx > split:
                want = target(full_history, units > 0)
                if want and units == 0:
                    px = bar["o"] * (1 + Decimal(slip) / 10000)
                    gross = cash / (1 + Decimal(fee) / 10000)
                    units = (gross / px).quantize(Decimal("0.000000000000000001"), rounding=ROUND_DOWN)
                    cost = units * px
                    paid = cost * Decimal(fee) / 10000
                    cash -= cost + paid
                    fees_paid += paid; fills += 1
                    entry_bar_index = idx
                elif not want and units > 0:
                    px = bar["o"] * (1 - Decimal(slip) / 10000)
                    gross = units * px
                    paid = gross * Decimal(fee) / 10000
                    cash += gross - paid; fees_paid += paid; fills += 1
                    if entry_bar_index is not None:
                        holding_bars.append(max(1, idx - entry_bar_index))
                    entry_bar_index = None
                    units = Decimal(0)
            eq = cash + units * bar["c"]
            if units > 0:
                invested += 1
                # OHLCV does not identify whether a bar's high or low came first.
                # Use the adverse high-then-low path as the conservative bound.
                intrabar_peak = max(highwater, cash + units * bar["h"])
                adverse = cash + units * bar["l"]
                if intrabar_peak > 0:
                    max_dd = max(max_dd, (intrabar_peak - min(intrabar_peak, adverse)) / intrabar_peak)
                highwater = max(highwater, intrabar_peak, eq)
            else:
                highwater = max(highwater, eq)

            # Close any remaining position at the last observed close so the
            # reported test return includes a modeled exit and its costs.
            if idx == len(bars) - 1 and units > 0:
                exit_price = bar["c"] * (1 - Decimal(slip) / 10000)
                gross = units * exit_price
                paid = gross * Decimal(fee) / 10000
                cash += gross - paid
                fees_paid += paid
                fills += 1
                if entry_bar_index is not None:
                    holding_bars.append(max(1, idx - entry_bar_index + 1))
                entry_bar_index = None
                units = Decimal(0)
                eq = cash
            test_curve.append(eq)
            last_equity = eq
            full_history.append(bar)
        folds_out = []
        prev = Decimal(1)
        peak = Decimal(1)
        for number, (begin, end) in enumerate(folds, 1):
            series = test_curve[begin - split:end - split]
            fold_start = prev
            fold_peak = fold_start
            dd = Decimal(0)
            ups = downs = 0
            for item in series:
                ups += item > prev; downs += item < prev
                dd = max(dd, (fold_peak - min(fold_peak, item)) / fold_peak if fold_peak else Decimal(0))
                fold_peak = max(fold_peak, item); prev = item
            folds_out.append({"number": number, "first_timestamp": bars[begin]["timestamp"],
                "last_timestamp": bars[end-1]["timestamp"], "return_percent": _percent((prev-fold_start)/fold_start),
                "max_drawdown_percent": _percent(dd), "positive_bars": ups, "negative_bars": downs})
        returns = []
        previous = Decimal(1)
        for item in test_curve:
            returns.append(item / previous - 1 if previous else Decimal(0)); previous = item
        volatility = None
        if len(returns) >= 30:
            mean = sum(returns, Decimal(0)) / len(returns)
            variance = sum(((x-mean)**2 for x in returns), Decimal(0)) / (len(returns)-1)
            volatility = _percent((variance * Decimal(annualization_bars[timeframe])).sqrt())
        sorted_holding = sorted(holding_bars)
        average_holding = (str((Decimal(sum(holding_bars)) / len(holding_bars)).quantize(Decimal("0.01")))
                           if holding_bars else None)
        if sorted_holding:
            middle = len(sorted_holding) // 2
            median_value = (Decimal(sorted_holding[middle]) if len(sorted_holding) % 2
                            else (Decimal(sorted_holding[middle - 1] + sorted_holding[middle]) / 2))
            median_holding = str(median_value.quantize(Decimal("0.01")))
        else:
            median_holding = None
        results.append({"id": strategy_id, "test_return_percent": _percent(last_equity-1),
            "max_drawdown_percent": _percent(max_dd), "annualized_volatility_percent": volatility,
            "fees_normalized": str(fees_paid.quantize(Decimal("0.00000001"))), "fills": fills,
            "round_trips": len(holding_bars), "average_holding_bars": average_holding,
            "median_holding_bars": median_holding,
            "exposure_percent": _percent(Decimal(invested) / Decimal(test_count)), "folds": folds_out})
    return {"provider": "Crypto.com Exchange", "timeframe": timeframe, "first_timestamp": bars[0]["timestamp"],
        "last_timestamp": bars[-1]["timestamp"], "bars": len(bars), "training_bars": split,
        "test_bars": test_count, "test_percent": test_percent, "commission_bps": fee, "slippage_bps": slip,
        "fill_rule": "signal at candle close, modeled fill at next candle open",
        "notice": "Research-only historical estimate. Normalized capital index; no portfolio, signal, recommendation, or order authority.",
        "results": results, "orders_enabled": False, "execution_allowed": False}


def _breakout(history: list[dict[str, object]], held: bool) -> bool:
    if len(history) < 11: return False
    current = history[-1]
    prior = history[-11:-1]
    prior_low = min(x["l"] for x in prior)
    if held: return current["c"] >= prior_low
    return current["c"] > max(x["h"] for x in prior) and current["v"] >= sum(x["v"] for x in prior) / 10 * Decimal("1.5")


def _percent(value: Decimal) -> str:
    return str((value * 100).quantize(Decimal("0.01")))
