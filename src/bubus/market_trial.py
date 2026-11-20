"""A single 24-hour market observation, resumed from a persistent volume."""
import json
import math
import sqlite3
import threading
import time
from collections import defaultdict
from contextlib import closing, contextmanager

STEP = 600
SLOTS = 144
DURATION = STEP * SLOTS
KINDS = ("crypto", "actions")


def normalized(rows, kind):
    result = []
    for row in rows:
        if kind == "crypto":
            asset, price = row["id"], row["price_usd"]
            stamp, currency = row.get("provider_timestamp"), "USD"
        else:
            asset, price = row["symbol"], row.get("price")
            stamp, currency = row.get("stamp"), row.get("currency")
        if not stamp or not currency:
            continue
        try:
            price = float(price)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(price) or price <= 0:
            continue
        result.append([asset, row["symbol"], row["name"], currency, str(stamp), price])
    return result


class MarketTrial:
    def __init__(self, directory, sources, explain, clock=time.time):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "market-trial.sqlite"
        self.sources = sources
        self.explain = explain
        self.clock = clock
        self.stop = threading.Event()
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS trial (id INTEGER PRIMARY KEY, started INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS universe (
                    kind TEXT NOT NULL, asset TEXT NOT NULL, PRIMARY KEY(kind, asset));
                CREATE TABLE IF NOT EXISTS snapshots (
                    slot INTEGER, kind TEXT, payload TEXT NOT NULL, PRIMARY KEY(slot, kind));
                CREATE TABLE IF NOT EXISTS reports (
                    hour INTEGER PRIMARY KEY, note TEXT, status TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO trial VALUES (1, ?)", (int(self.clock()),))
            self.started = db.execute("SELECT started FROM trial").fetchone()[0]
            db.execute("UPDATE reports SET status='interrompu' WHERE status='en cours'")

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            yield conn

    def _run(self):
        while not self.stop.is_set():
            try:
                if self.tick():
                    return
            except sqlite3.Error:
                print("BUBUS classement : stockage momentanément indisponible.", flush=True)
            self.stop.wait(20)

    def start(self):
        threading.Thread(target=self._run, name="bubus-market-trial", daemon=True).start()

    def _collect(self, slot, kind):
        with self.db() as db:
            if db.execute("SELECT 1 FROM snapshots WHERE slot=? AND kind=?", (slot, kind)).fetchone():
                return
        try:
            rows = normalized(self.sources[kind](), kind)
        except Exception:
            return
        if not rows:
            return
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            assets = {r[0] for r in db.execute("SELECT asset FROM universe WHERE kind=?", (kind,))}
            if not assets:
                selected = rows[:30] if kind == "crypto" else rows
                assets = {r[0] for r in selected}
                db.executemany("INSERT INTO universe VALUES (?, ?)", [(kind, asset) for asset in assets])
            payload = [row for row in rows if row[0] in assets]
            if payload:
                saved = db.execute("INSERT OR IGNORE INTO snapshots VALUES (?, ?, ?)",
                                   (slot, kind, json.dumps(payload))).rowcount
                if saved and slot == 0:
                    print(f"BUBUS classement : premier relevé {kind} ({len(payload)} actifs).", flush=True)

    def tick(self):
        elapsed = max(0, int(self.clock()) - self.started)
        if elapsed < DURATION:
            for kind in KINDS:
                self._collect(elapsed // STEP, kind)
        hour = min(24, elapsed // 3600)
        if hour:
            with self.db() as db:
                claimed = db.execute("INSERT OR IGNORE INTO reports VALUES (?, NULL, 'en cours')", (hour,)).rowcount
            if claimed:
                data = self.status(include_report=False)
                note, state = None, "données insuffisantes"
                if any(data["ranking"].values()):
                    try:
                        note, state = self.explain(data), "disponible"
                    except Exception:
                        state = "OpenRouter indisponible"
                        print("BUBUS classement : OpenRouter indisponible.", flush=True)
                with self.db() as db:
                    db.execute("UPDATE reports SET note=?, status=? WHERE hour=?", (note, state, hour))
        return elapsed >= DURATION

    def status(self, *, include_report=True):
        ranking, coverage, universe = {}, {}, {}
        with self.db() as db:
            report = db.execute("SELECT hour, note, status FROM reports ORDER BY hour DESC LIMIT 1").fetchone()
            for kind in KINDS:
                prices = defaultdict(dict)
                counts = defaultdict(int)
                rows = list(db.execute("SELECT payload FROM snapshots WHERE kind=? ORDER BY slot", (kind,)))
                universe[kind] = db.execute("SELECT count(*) FROM universe WHERE kind=?", (kind,)).fetchone()[0]
                coverage[kind] = len(rows)
                for (payload,) in rows:
                    for asset, symbol, name, currency, stamp, price in json.loads(payload):
                        series = prices[(asset, symbol, name, currency)]
                        counts[(asset, symbol, name, currency)] += 1
                        series.setdefault(stamp, price)
                results = []
                for identity, series in prices.items():
                    _, symbol, name, currency = identity
                    if len(series) < 2:
                        continue
                    values = [price for _, price in sorted(series.items())]
                    change = 100 * (values[-1] / values[0] - 1)
                    drawdown = peak = 0
                    for value in values:
                        peak = max(value, peak)
                        drawdown = max(drawdown, 100 * (1 - value / peak))
                    results.append(dict(symbol=symbol, name=name, currency=currency,
                                        change=round(change, 3), drawdown=round(drawdown, 3),
                                        score=round(change - drawdown, 3), quotes=len(values),
                                        samples=counts[identity]))
                ranking[kind] = sorted(results, key=lambda r: (-r["score"], r["symbol"]))[:5]
        return {"started": self.started, "ends": self.started + DURATION, "target": SLOTS,
                "coverage": coverage, "universe": universe, "ranking": ranking,
                "report": dict(zip(("hour", "note", "status"), report)) if include_report and report else None}
