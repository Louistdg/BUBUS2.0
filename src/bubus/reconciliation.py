"""Read-only, fail-closed audit of Crypto.com portfolio reconciliation evidence.

Spot fills can be projected into signed asset legs only under strict coverage
and snapshot-linkage checks. Ambiguous conversions/flows and daily P&L remain
unsupported; arithmetic ledger matches are evidence only, never Risk or
execution authority.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from bubus.quantity_ledger import AssetLedgerError, reconcile_asset_quantities, spot_trade_postings

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_JOURNAL = re.compile(r"[0-9]{1,32}\Z")
_ASSET = re.compile(r"[A-Z0-9]{2,16}\Z")
_CATEGORIES = {
    "ONCHAIN_DEPOSIT": "deposits",
    "OFFCHAIN_DEPOSIT": "deposits",
    "DEPOSIT": "deposits",
    "ROLLBACK_WITHDRAW": "deposits",
    "ONCHAIN_WITHDRAWAL": "withdrawals",
    "OFFCHAIN_WITHDRAWAL": "withdrawals",
    "WITHDRAW": "withdrawals",
    "ROLLBACK_DEPOSIT": "withdrawals",
    "FIAT_WITHDRAWAL_CANCEL": "withdrawals",
    "AUTO_CONVERSION": "conversions",
    "MANUAL_CONVERSION": "conversions",
    "TRADING": "trades",
    "TRADE_FEE": "fees",
    "WITHDRAW_FEE": "fees",
    "LIQUIDATION_FEE": "fees",
    "SETTLEMENT_FEE": "fees",
    "MARGIN_TRADE_INTEREST": "fees",
}
_EXPECTED_FIELDS = {
    "journal_type", "journal_id", "transaction_qty", "transaction_cost",
    "realized_pnl", "order_id", "trade_id", "instrument_name", "side",
    "occurred_at", "source", "payload_sha256",
}
_OPEN_ORDER_FIELDS = {
    "account_id", "order_id", "client_oid", "instrument_name", "side", "order_type", "status",
    "quantity", "cumulative_quantity", "limit_price", "created_at", "updated_at", "source",
    "payload_sha256",
}
_OPEN_ORDER_STATUSES = {"PENDING", "NEW", "ACTIVE"}
# A journal may appear in several overlapping, independently fetched pages;
# its source payload hash can legitimately differ across those pages.
_EVENT_IDENTITY_FIELDS = _EXPECTED_FIELDS - {"payload_sha256"}


class ReconciliationEvidenceError(ValueError):
    """Untrusted or internally inconsistent locally persisted evidence."""


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ReconciliationEvidenceError("Evidence timestamp is missing.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ReconciliationEvidenceError("Evidence timestamp is malformed.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReconciliationEvidenceError("Evidence timestamp needs a timezone.")
    return parsed.astimezone(timezone.utc)


def _epoch_ns(value: datetime) -> int:
    delta = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return ((delta.days * 86_400 + delta.seconds) * 1_000_000_000
            + delta.microseconds * 1_000)


def _epoch_ms(value: datetime) -> int:
    return _epoch_ns(value) // 1_000_000


def _decimal(value: object) -> Decimal:
    if not isinstance(value, str) or not value or len(value) > 100:
        raise ReconciliationEvidenceError("Journal amount is not a decimal string.")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ReconciliationEvidenceError("Journal amount is invalid.") from None
    if not number.is_finite():
        raise ReconciliationEvidenceError("Journal amount is non-finite.")
    return number


def _fingerprint(data: object) -> str:
    return hashlib.sha256(json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _validate_open_orders(portfolio: dict) -> tuple[set[str], dict]:
    """Validate the signed open-order observation used by the latest valuation."""
    rows = portfolio.get("open_orders")
    digest = portfolio.get("open_orders_payload_sha256")
    if (not isinstance(rows, list) or len(rows) > 500 or not isinstance(digest, str)
            or _SHA.fullmatch(digest) is None):
        raise ReconciliationEvidenceError("Portfolio open-order evidence or its source hash is missing.")
    fetched_at = _timestamp(portfolio.get("fetched_at"))
    seen: set[tuple[str, str]] = set()
    order_ids: set[str] = set()
    for order in rows:
        if not isinstance(order, dict) or set(order) != _OPEN_ORDER_FIELDS:
            raise ReconciliationEvidenceError("Portfolio open-order row has an unexpected structure.")
        account_id, order_id = order["account_id"], order["order_id"]
        if (not isinstance(account_id, str) or not account_id.strip() or len(account_id) > 128
                or not isinstance(order_id, str) or not order_id.isdecimal() or len(order_id) > 32
                or (account_id, order_id) in seen
                or (order["client_oid"] is not None and (not isinstance(order["client_oid"], str)
                    or not order["client_oid"] or len(order["client_oid"]) > 128))
                or not isinstance(order["instrument_name"], str)
                or re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,15}_[A-Z]{3}", order["instrument_name"]) is None
                or order["side"] not in {"BUY", "SELL"} or order["order_type"] != "LIMIT"
                or order["status"] not in _OPEN_ORDER_STATUSES
                or order["source"] != "private/get-open-orders"
                or order["payload_sha256"] != digest):
            raise ReconciliationEvidenceError("Portfolio open-order identity or provenance is invalid.")
        quantity, cumulative = _decimal(order["quantity"]), _decimal(order["cumulative_quantity"])
        if quantity <= 0 or cumulative < 0 or cumulative >= quantity:
            raise ReconciliationEvidenceError("Portfolio open-order remaining quantity is invalid.")
        limit_price = _decimal(order["limit_price"])
        if limit_price <= 0:
            raise ReconciliationEvidenceError("Portfolio open LIMIT order lacks a valid limit price.")
        created_at = updated_at = None
        for field in ("created_at", "updated_at"):
            if order[field] is not None:
                timestamp = _timestamp(order[field])
                if timestamp > fetched_at:
                    raise ReconciliationEvidenceError("Portfolio open-order timestamp is newer than its snapshot.")
                if field == "created_at":
                    created_at = timestamp
                else:
                    updated_at = timestamp
        if created_at is not None and updated_at is not None and updated_at < created_at:
            raise ReconciliationEvidenceError("Portfolio open-order update precedes its creation.")
        seen.add((account_id, order_id))
        order_ids.add(order_id)
    return order_ids, {"count": len(rows), "payload_sha256": digest}


def validate_transaction_page(page: object) -> tuple[dict, list[dict]]:
    if not isinstance(page, dict):
        raise ReconciliationEvidenceError("Transaction page is malformed.")
    start, end, count = (page.get("start_time_ms"), page.get("end_time_ms"),
                         page.get("item_count"))
    if (type(start) is not int or type(end) is not int or type(count) is not int
            or start < 0 or start >= end or end - start > 7 * 86_400_000
            or not 0 <= count <= 100
            or not isinstance(page.get("items"), list)
            or len(page["items"]) != count
            or type(page.get("snapshot_id")) is not int or page["snapshot_id"] <= 0
            or not isinstance(page.get("payload_sha256"), str)
            or _SHA.fullmatch(page["payload_sha256"]) is None
            or not isinstance(page.get("provider_payload_sha256"), str)
            or _SHA.fullmatch(page["provider_payload_sha256"]) is None):
        raise ReconciliationEvidenceError("Transaction page metadata is inconsistent.")
    observed = _timestamp(page.get("observed_at"))
    batch = page.get("batch")
    batch_evidence = None
    if batch is not None:
        required_batch = {"batch_id", "query_start_time_ms", "query_end_time_ms", "coverage_complete",
                          "incomplete_intervals", "request_count", "page_count"}
        try:
            batch_uuid = str(uuid.UUID(batch.get("batch_id"))) if isinstance(batch, dict) else None
        except (AttributeError, TypeError, ValueError):
            batch_uuid = None
        if (not isinstance(batch, dict) or set(batch) != required_batch
                or batch_uuid != batch.get("batch_id")
                or page.get("batch_id") != batch.get("batch_id")
                or type(batch.get("query_start_time_ms")) is not int
                or type(batch.get("query_end_time_ms")) is not int
                or batch["query_start_time_ms"] < 0
                or batch["query_start_time_ms"] >= batch["query_end_time_ms"]
                or not batch["query_start_time_ms"] <= start < end <= batch["query_end_time_ms"]
                or type(batch.get("coverage_complete")) is not bool
                or not isinstance(batch.get("incomplete_intervals"), list)
                or len(batch["incomplete_intervals"]) > 64
                or type(batch.get("request_count")) is not int or not 1 <= batch["request_count"] <= 64
                or type(batch.get("page_count")) is not int or not 1 <= batch["page_count"] <= 16
                or batch["coverage_complete"] != (len(batch["incomplete_intervals"]) == 0)):
            raise ReconciliationEvidenceError("Transaction batch metadata is inconsistent.")
        for interval in batch["incomplete_intervals"]:
            if (not isinstance(interval, list) or len(interval) != 2
                    or type(interval[0]) is not int or type(interval[1]) is not int
                    or not batch["query_start_time_ms"] <= interval[0] < interval[1] <= batch["query_end_time_ms"]):
                raise ReconciliationEvidenceError("Transaction batch incomplete interval is invalid.")
        batch_evidence = {key: batch[key] for key in (
            "batch_id", "query_start_time_ms", "query_end_time_ms", "coverage_complete",
            "incomplete_intervals", "request_count", "page_count")}
    rows: list[dict] = []
    ids: set[str] = set()
    for item in page["items"]:
        if not isinstance(item, dict) or set(item) != _EXPECTED_FIELDS:
            raise ReconciliationEvidenceError("Journal has an unexpected structure.")
        kind, journal_id = item["journal_type"], item["journal_id"]
        if (not isinstance(kind, str) or not kind or len(kind) > 64
                or not isinstance(journal_id, str) or _JOURNAL.fullmatch(journal_id) is None
                or journal_id in ids
                or not isinstance(item["instrument_name"], str)
                or not 2 <= len(item["instrument_name"]) <= 64
                or item["side"] not in (None, "BUY", "SELL")
                or any(v is not None and (not isinstance(v, str) or _JOURNAL.fullmatch(v) is None)
                       for v in (item["trade_id"], item["order_id"]))
                or item["source"] != "private/get-transactions"
                or not isinstance(item["payload_sha256"], str)
                or _SHA.fullmatch(item["payload_sha256"]) is None):
            raise ReconciliationEvidenceError("Journal identity or source is inconsistent.")
        for field in ("transaction_qty", "transaction_cost", "realized_pnl"):
            _decimal(item[field])
        instant = _timestamp(item["occurred_at"])
        instant_ms = int(instant.timestamp() * 1000)
        if not start <= instant_ms < end or instant > observed:
            raise ReconciliationEvidenceError("Journal is outside its declared observation.")
        ids.add(journal_id)
        rows.append(item)
    metadata = {
        "snapshot_id": page["snapshot_id"],
        "start_time_ms": start, "end_time_ms": end, "item_count": count,
        "observed_at": page["observed_at"],
        "payload_sha256": page["payload_sha256"],
        "provider_payload_sha256": page["provider_payload_sha256"],
        "page_limit_reached": count == 100,
    }
    if batch_evidence is not None:
        metadata["batch"] = batch_evidence
    return metadata, rows


def validate_trade_page(page: object) -> tuple[dict, list[dict]]:
    if not isinstance(page, dict):
        raise ReconciliationEvidenceError("Trade page is malformed.")
    start, end, count = (page.get("start_time_ns"), page.get("end_time_ns"), page.get("item_count"))
    if (type(start) is not int or type(end) is not int or type(count) is not int
            or start < 0 or start >= end or end - start > 7 * 86_400_000_000_000
            or not 0 <= count <= 100 or not isinstance(page.get("items"), list)
            or len(page["items"]) != count or type(page.get("snapshot_id")) is not int
            or page["snapshot_id"] <= 0 or not isinstance(page.get("payload_sha256"), str)
            or _SHA.fullmatch(page["payload_sha256"]) is None
            or not isinstance(page.get("provider_payload_sha256"), str)
            or _SHA.fullmatch(page["provider_payload_sha256"]) is None):
        raise ReconciliationEvidenceError("Trade page metadata is inconsistent.")
    observed = _timestamp(page.get("observed_at"))
    batch = page.get("batch")
    batch_evidence = None
    if batch is not None:
        required_batch = {"batch_id", "query_start_time_ns", "query_end_time_ns", "coverage_complete",
                          "incomplete_intervals", "request_count", "page_count"}
        try:
            batch_uuid = str(uuid.UUID(batch.get("batch_id"))) if isinstance(batch, dict) else None
        except (AttributeError, TypeError, ValueError):
            batch_uuid = None
        if (not isinstance(batch, dict) or set(batch) != required_batch
                or batch_uuid != batch.get("batch_id") or page.get("batch_id") != batch.get("batch_id")
                or type(batch.get("query_start_time_ns")) is not int
                or type(batch.get("query_end_time_ns")) is not int
                or not batch["query_start_time_ns"] <= start < end <= batch["query_end_time_ns"]
                or type(batch.get("coverage_complete")) is not bool
                or not isinstance(batch.get("incomplete_intervals"), list)
                or len(batch["incomplete_intervals"]) > 64
                or type(batch.get("request_count")) is not int or not 1 <= batch["request_count"] <= 64
                or type(batch.get("page_count")) is not int or not 1 <= batch["page_count"] <= 64
                or batch["coverage_complete"] != (len(batch["incomplete_intervals"]) == 0)):
            raise ReconciliationEvidenceError("Trade batch metadata is inconsistent.")
        for interval in batch["incomplete_intervals"]:
            if (not isinstance(interval, list) or len(interval) != 2
                    or type(interval[0]) is not int or type(interval[1]) is not int
                    or not batch["query_start_time_ns"] <= interval[0] < interval[1]
                    <= batch["query_end_time_ns"]):
                raise ReconciliationEvidenceError("Trade batch incomplete interval is invalid.")
        batch_evidence = {key: batch[key] for key in ("batch_id", "query_start_time_ns", "query_end_time_ns",
            "coverage_complete", "incomplete_intervals", "request_count", "page_count")}
    expected = {"trade_id", "order_id", "trade_match_id", "client_oid", "instrument_name", "side",
                "taker_side", "quantity", "price", "fees", "fee_currency", "occurred_at",
                "timestamp_ns", "source", "payload_sha256"}
    rows, seen = [], set()
    for item in page["items"]:
        if not isinstance(item, dict) or set(item) != expected:
            raise ReconciliationEvidenceError("Trade row has an unexpected structure.")
        identity = (item["instrument_name"], item["trade_id"])
        if (not isinstance(item["trade_id"], str) or _JOURNAL.fullmatch(item["trade_id"]) is None
                or not isinstance(item["order_id"], str) or _JOURNAL.fullmatch(item["order_id"]) is None
                or not isinstance(item["trade_match_id"], str)
                or _JOURNAL.fullmatch(item["trade_match_id"]) is None
                or not isinstance(item["instrument_name"], str)
                or re.fullmatch(r"[A-Z0-9._-]{2,64}", item["instrument_name"]) is None
                or item["side"] not in {"BUY", "SELL"}
                or item["taker_side"] not in {None, "MAKER", "TAKER"}
                or not isinstance(item["fee_currency"], str)
                or _ASSET.fullmatch(item["fee_currency"]) is None
                or not isinstance(item["timestamp_ns"], str)
                or re.fullmatch(r"[0-9]{1,24}", item["timestamp_ns"]) is None
                or not start <= int(item["timestamp_ns"]) < end
                or item["source"] != "private/get-trades"
                or item["payload_sha256"] != page["provider_payload_sha256"]
                or identity in seen):
            raise ReconciliationEvidenceError("Trade identity or source is inconsistent.")
        client_oid = item["client_oid"]
        if client_oid is not None and (not isinstance(client_oid, str)
                or re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", client_oid) is None):
            raise ReconciliationEvidenceError("Trade client order reference is invalid.")
        occurred = _timestamp(item["occurred_at"])
        if occurred > observed or _epoch_ms(occurred) != int(item["timestamp_ns"]) // 1_000_000:
            raise ReconciliationEvidenceError("Trade timestamp is outside its declared observation.")
        quantity, price, fees = (_decimal(item[key]) for key in ("quantity", "price", "fees"))
        if quantity <= 0 or price <= 0:
            raise ReconciliationEvidenceError("Trade quantity and price must be positive.")
        seen.add(identity)
        rows.append(item)
    metadata = {"snapshot_id": page["snapshot_id"], "start_time_ns": start,
        "end_time_ns": end, "item_count": count, "observed_at": page["observed_at"],
        "payload_sha256": page["payload_sha256"],
        "provider_payload_sha256": page["provider_payload_sha256"],
        "page_limit_reached": count == 100}
    if batch_evidence is not None:
        metadata["batch"] = batch_evidence
    return metadata, rows


def validate_funding_history(snapshot: object) -> tuple[dict, list[dict]]:
    """Verify signed deposit/withdrawal history without assigning it Risk authority."""
    required = {"snapshot_id", "observed_at", "direction", "start_time_ms", "end_time_ms", "item_count",
                "items", "pages", "complete", "next_page", "request_count", "payload_sha256",
                "authority_status", "usable_for_risk", "orders_enabled"}
    if (not isinstance(snapshot, dict) or set(snapshot) != required
            or type(snapshot["snapshot_id"]) is not int or snapshot["snapshot_id"] <= 0
            or snapshot["direction"] not in {"DEPOSIT", "WITHDRAWAL"}
            or type(snapshot["start_time_ms"]) is not int or type(snapshot["end_time_ms"]) is not int
            or snapshot["start_time_ms"] < 0 or snapshot["start_time_ms"] >= snapshot["end_time_ms"]
            or snapshot["end_time_ms"] - snapshot["start_time_ms"] > 7 * 86_400_000
            or type(snapshot["item_count"]) is not int or not 0 <= snapshot["item_count"] <= 12_800
            or not isinstance(snapshot["items"], list) or not isinstance(snapshot["pages"], list)
            or not 1 <= len(snapshot["pages"]) <= 64 or type(snapshot["complete"]) is not bool
            or (snapshot["next_page"] is not None and (type(snapshot["next_page"]) is not int or snapshot["next_page"] < 1))
            or type(snapshot["request_count"]) is not int or snapshot["request_count"] != len(snapshot["pages"])
            or snapshot["authority_status"] != "OBSERVATION_ONLY"
            or snapshot["usable_for_risk"] is not False or snapshot["orders_enabled"] is not False
            or not isinstance(snapshot["payload_sha256"], str) or _SHA.fullmatch(snapshot["payload_sha256"]) is None):
        raise ReconciliationEvidenceError("Funding-history metadata or authority is invalid.")
    observed = _timestamp(snapshot["observed_at"])
    if observed.timestamp() * 1000 < snapshot["end_time_ms"]:
        raise ReconciliationEvidenceError("Funding history was observed before its requested range ended.")
    expected_evidence = {key: snapshot[key] for key in
        ("direction", "start_time_ms", "end_time_ms", "pages", "complete", "next_page", "request_count")}
    if snapshot["payload_sha256"] != _fingerprint(expected_evidence):
        raise ReconciliationEvidenceError("Funding-history payload hash does not match its pages.")
    if snapshot["complete"] != (snapshot["next_page"] is None):
        raise ReconciliationEvidenceError("Funding-history continuation status is inconsistent.")

    expected_source = ("private/get-deposit-history" if snapshot["direction"] == "DEPOSIT"
                       else "private/get-withdrawal-history")
    page_numbers, item_ids, flattened = set(), set(), []
    terminal_statuses = {"1", "2"} if snapshot["direction"] == "DEPOSIT" else {"2", "4", "5", "6"}
    terminal = True
    for page in snapshot["pages"]:
        if (not isinstance(page, dict) or set(page) != {"page", "page_size", "items", "payload_sha256"}
                or type(page["page"]) is not int or not 0 <= page["page"] < 64 or page["page"] in page_numbers
                or type(page["page_size"]) is not int or not 1 <= page["page_size"] <= 200
                or not isinstance(page["items"], list) or len(page["items"]) > page["page_size"]
                or not isinstance(page["payload_sha256"], str) or _SHA.fullmatch(page["payload_sha256"]) is None):
            raise ReconciliationEvidenceError("Funding-history page is malformed.")
        page_numbers.add(page["page"])
        for item in page["items"]:
            fields = {"direction", "transfer_id", "currency", "amount", "fee", "status", "created_at",
                      "updated_at", "source", "payload_sha256"}
            if (not isinstance(item, dict) or set(item) != fields or item["direction"] != snapshot["direction"]
                    or not isinstance(item["transfer_id"], str)
                    or re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", item["transfer_id"]) is None
                    or item["transfer_id"] in item_ids or not isinstance(item["currency"], str)
                    or _ASSET.fullmatch(item["currency"]) is None
                    or not isinstance(item["status"], str) or not item["status"].isdigit()
                    or item["status"] not in ({"0", "1", "2", "3"} if snapshot["direction"] == "DEPOSIT"
                                               else {"0", "1", "2", "3", "4", "5", "6"})
                    or item["source"] != expected_source or item["payload_sha256"] != page["payload_sha256"]
                    or not isinstance(item["payload_sha256"], str)
                    or _SHA.fullmatch(item["payload_sha256"]) is None):
                raise ReconciliationEvidenceError("Funding transfer identity or provenance is invalid.")
            created, updated = _timestamp(item["created_at"]), _timestamp(item["updated_at"])
            amount, fee = _decimal(item["amount"]), _decimal(item["fee"])
            if (created > observed or updated > observed or created.timestamp() * 1000 < snapshot["start_time_ms"]
                    or created.timestamp() * 1000 >= snapshot["end_time_ms"] or updated < created
                    or amount <= 0 or fee < 0):
                raise ReconciliationEvidenceError("Funding transfer amount or timestamps are invalid.")
            if item["status"] not in terminal_statuses:
                terminal = False
            item_ids.add(item["transfer_id"])
            flattened.append(item)
    if (page_numbers != set(range(len(page_numbers))) or flattened != snapshot["items"]
            or len(flattened) != snapshot["item_count"]):
        raise ReconciliationEvidenceError("Funding-history page ordering or item count is inconsistent.")
    last_page = snapshot["pages"][-1]
    if snapshot["complete"]:
        if last_page["items"] and len(last_page["items"]) == last_page["page_size"]:
            raise ReconciliationEvidenceError("A full final funding page cannot prove pagination completion.")
    elif (snapshot["next_page"] != len(snapshot["pages"])
            or any(len(page["items"]) != page["page_size"] for page in snapshot["pages"])):
        raise ReconciliationEvidenceError("Funding-history continuation pages are inconsistent.")
    metadata = {"snapshot_id": snapshot["snapshot_id"], "direction": snapshot["direction"],
        "start_time_ms": snapshot["start_time_ms"], "end_time_ms": snapshot["end_time_ms"],
        "observed_at": snapshot["observed_at"], "item_count": snapshot["item_count"],
        "page_count": len(snapshot["pages"]), "complete": snapshot["complete"],
        "all_transfers_terminal": terminal, "payload_sha256": snapshot["payload_sha256"]}
    return metadata, flattened


def _wallet_quantities_by_asset(observation: dict) -> dict[str, Decimal]:
    rows = observation.get("wallet_balances")
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ReconciliationEvidenceError("Portfolio wallet balance evidence is invalid.")
    quantities: dict[str, Decimal] = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        expected = {"account_currency", "collateral_amount", "currency", "market_value",
                    "max_withdrawal_balance", "payload_sha256", "quantity", "reserved_quantity", "source"}
        if (not isinstance(row, dict) or set(row) != expected
                or not isinstance(row.get("currency"), str) or _ASSET.fullmatch(row["currency"]) is None
                or not isinstance(row.get("account_currency"), str)
                or _ASSET.fullmatch(row["account_currency"]) is None
                or row.get("source") != "private/user-balance"
                or row.get("payload_sha256") != observation.get("payload_sha256")):
            raise ReconciliationEvidenceError("Portfolio wallet balance identity or provenance is invalid.")
        identity = (row["account_currency"], row["currency"])
        if identity in seen:
            raise ReconciliationEvidenceError("Portfolio wallet balance identity is duplicated.")
        seen.add(identity)
        quantity = _decimal(row["quantity"])
        reserved = _decimal(row["reserved_quantity"])
        for field in ("collateral_amount", "market_value", "max_withdrawal_balance"):
            if _decimal(row[field]) < 0:
                raise ReconciliationEvidenceError("Portfolio wallet balance amount is negative.")
        if quantity < 0 or reserved < 0 or reserved > quantity:
            raise ReconciliationEvidenceError("Portfolio wallet quantity or reservation is invalid.")
        quantities[row["currency"]] = quantities.get(row["currency"], Decimal(0)) + quantity
    return quantities


def audit_reconciliation(*, valuations: list[dict], transaction_pages: list[dict],
                         portfolio: dict | None, cash_history: dict | None,
                         trade_pages: list[dict] | None = None,
                         funding_history: list[dict] | None = None,
                         portfolio_history: list[dict] | None = None,
                         _include_daily_observation: bool = True) -> dict:
    """Audit observable evidence without guessing asset deltas, fees, or P&L.

    Raises on corrupt evidence. A fully matched, no-external-flow interval may
    expose an informational valuation delta. A fully covered UTC-day window
    may also expose a separate observation; neither is Risk authority.
    """
    if trade_pages is None:
        trade_pages = []
    if funding_history is None:
        funding_history = []
    if portfolio_history is None:
        portfolio_history = [portfolio] if portfolio is not None else []
    if (not isinstance(valuations, list) or not isinstance(transaction_pages, list)
            or not isinstance(trade_pages, list) or not isinstance(funding_history, list)
            or not isinstance(portfolio_history, list) or len(valuations) > 100
            or type(_include_daily_observation) is not bool
            or len(transaction_pages) > 50 or len(trade_pages) > 50 or len(funding_history) > 100
            or len(portfolio_history) > 1000):
        raise ReconciliationEvidenceError("Reconciliation input size is invalid.")

    reasons: list[str] = []
    warnings: list[str] = []
    contradictory: list[str] = []
    evidence: dict = {"valuations": [], "transaction_pages": [], "trade_pages": [], "funding_history": [],
                      "portfolio": None, "cash_history": None}
    counts = {key: 0 for key in ("deposits", "withdrawals", "conversions", "trades", "fees", "other")}
    examples = {key: [] for key in counts}
    valuation_pair = None
    funding_candidates_by_direction: dict[str, list[tuple[dict, list[dict]]]] = {}
    funding_by_direction = {}
    funding_event_counts = {"deposits": 0, "withdrawals": 0, "completed_deposits": 0,
                            "completed_withdrawals": 0}
    funding_coverage_snapshot_ids: dict[str, int] = {}
    valuation_reservations: dict[int, set[str]] = {}
    valuation_source_digests: dict[int, set[str]] = {}
    seen_valuation_ids: set[int] = set()
    portfolio_open_order_ids: set[str] = set()
    portfolio_open_orders_digest: str | None = None
    open_orders_bound_to_latest_valuation = False
    valuation_portfolios: dict[int, dict] = {}
    executed_trades_reconciled = False
    conversion_legs_reconciled = False
    fee_postings_reconciled = False
    per_asset_postings_reconciled = False
    asset_ledger_evidence = None
    period_pnl = None

    for snapshot in funding_history:
        metadata, rows = validate_funding_history(snapshot)
        direction = metadata["direction"]
        funding_candidates_by_direction.setdefault(direction, []).append((metadata, rows))
        evidence["funding_history"].append(metadata)

    for v in valuations:
        if (not isinstance(v, dict) or type(v.get("valuation_id")) is not int
                or v["valuation_id"] <= 0 or not isinstance(v.get("stored_payload_sha256"), str)
                or _SHA.fullmatch(v["stored_payload_sha256"]) is None
                or v.get("usable_for_risk") is not False
                or v.get("usable_for_execution") is not False
                or v.get("orders_enabled") is not False):
            raise ReconciliationEvidenceError("Valuation evidence is untrusted.")
        if v["valuation_id"] in seen_valuation_ids:
            raise ReconciliationEvidenceError("Valuation identity is duplicated.")
        seen_valuation_ids.add(v["valuation_id"])
        _timestamp(v.get("as_of"))
        source_digests = v.get("source_payload_sha256")
        reservations = v.get("order_reservations")
        if (not isinstance(source_digests, list) or not source_digests
                or len(source_digests) > 500 or any(not isinstance(value, str) or _SHA.fullmatch(value) is None
                                                    for value in source_digests)
                or len(set(source_digests)) != len(source_digests)
                or not isinstance(reservations, list) or len(reservations) > 500):
            raise ReconciliationEvidenceError("Valuation source hashes or open-order reservations are invalid.")
        reservation_ids: set[str] = set()
        for reservation in reservations:
            if (not isinstance(reservation, dict) or set(reservation) != {"order_id", "value"}
                    or not isinstance(reservation["order_id"], str) or not reservation["order_id"]
                    or reservation["order_id"] in reservation_ids):
                raise ReconciliationEvidenceError("Valuation open-order reservation identity is invalid.")
            if _decimal(reservation["value"]) <= 0:
                raise ReconciliationEvidenceError("Valuation open-order reservation must be positive.")
            reservation_ids.add(reservation["order_id"])
        valuation_reservations[v["valuation_id"]] = reservation_ids
        valuation_source_digests[v["valuation_id"]] = set(source_digests)
        evidence["valuations"].append({
            "valuation_id": v["valuation_id"], "as_of": v["as_of"],
            "base_currency": v["base_currency"], "payload_sha256": v["stored_payload_sha256"],
            "order_reservation_count": len(reservation_ids), "source_count": len(source_digests),
        })

    if len(valuations) < 2:
        reasons.append("TWO_VALUATIONS_REQUIRED")
    else:
        newer, older = valuations[0], valuations[1]
        start, end = _timestamp(older["as_of"]), _timestamp(newer["as_of"])
        if start >= end:
            contradictory.append("VALUATION_CHRONOLOGY")
        if newer["base_currency"] != older["base_currency"]:
            reasons.append("VALUATION_CURRENCY_MISMATCH")
        if start < end and newer["base_currency"] == older["base_currency"]:
            valuation_pair = {"opening_id": older["valuation_id"], "closing_id": newer["valuation_id"],
                              "start": older["as_of"], "end": newer["as_of"],
                              "base_currency": newer["base_currency"]}

    if portfolio is None:
        reasons.append("PORTFOLIO_OBSERVATION_MISSING")
    else:
        if (not isinstance(portfolio, dict) or portfolio.get("provider") != "Crypto.com Exchange"
                or portfolio.get("authority_status") != "OBSERVATION_ONLY"
                or portfolio.get("usable_for_risk") is not False
                or portfolio.get("orders_enabled") is not False
                or type(portfolio.get("snapshot_id")) is not int
                or not isinstance(portfolio.get("stored_payload_sha256"), str)
                or _SHA.fullmatch(portfolio["stored_payload_sha256"]) is None):
            raise ReconciliationEvidenceError("Portfolio observation has invalid authority or provenance.")
        _timestamp(portfolio.get("fetched_at"))
        evidence["portfolio"] = {
            "snapshot_id": portfolio["snapshot_id"],
            "fetched_at": portfolio["fetched_at"],
            "payload_sha256": portfolio["stored_payload_sha256"],
        }
        try:
            portfolio_open_order_ids, open_order_evidence = _validate_open_orders(portfolio)
            portfolio_open_orders_digest = open_order_evidence["payload_sha256"]
            evidence["portfolio"]["open_orders"] = open_order_evidence
        except ReconciliationEvidenceError:
            reasons.append("OPEN_ORDERS_OBSERVATION_INVALID")
        if portfolio.get("positions"):
            reasons.append("DERIVATIVE_POSITIONS_UNSUPPORTED")
        if not isinstance(portfolio.get("wallet_balances"), list):
            reasons.append("ITEMIZED_WALLET_MISSING")

    if portfolio is None or portfolio_open_orders_digest is None:
        reasons.append("OPEN_ORDERS_OBSERVATION_MISSING")
    elif not valuations:
        reasons.append("OPEN_ORDER_RESERVATIONS_NOT_RECONCILED")
    else:
        latest_valuation_id = valuations[0]["valuation_id"]
        for observation in portfolio_history:
            if (not isinstance(observation, dict) or observation.get("provider") != "Crypto.com Exchange"
                    or observation.get("authority_status") != "OBSERVATION_ONLY"
                    or observation.get("usable_for_risk") is not False
                    or observation.get("usable_for_execution") is not False
                    or observation.get("orders_enabled") is not False
                    or type(observation.get("snapshot_id")) is not int
                    or not isinstance(observation.get("stored_payload_sha256"), str)
                    or _SHA.fullmatch(observation["stored_payload_sha256"]) is None
                    or not isinstance(observation.get("wallet_balances"), list)
                    or not isinstance(observation.get("positions"), list)
                    or observation.get("positions")):
                raise ReconciliationEvidenceError("Historical portfolio observation is invalid.")
            observed_at = _timestamp(observation.get("fetched_at"))
            source_values = [observation.get("payload_sha256"), observation.get("positions_payload_sha256"),
                             observation.get("open_orders_payload_sha256")]
            if any(not isinstance(value, str) or _SHA.fullmatch(value) is None for value in source_values):
                raise ReconciliationEvidenceError("Historical portfolio source hashes are invalid.")
            order_ids, order_evidence = _validate_open_orders(observation)
            wallet_quantities = _wallet_quantities_by_asset(observation)
            if order_evidence["payload_sha256"] != source_values[2]:
                raise ReconciliationEvidenceError("Historical open-order evidence hash mismatch.")
            for valuation in valuations[:2]:
                valuation_at = _timestamp(valuation["as_of"])
                age = (valuation_at - observed_at).total_seconds()
                if (0 <= age <= 30
                        and set(source_values).issubset(valuation_source_digests[valuation["valuation_id"]])):
                    valuation_portfolios[valuation["valuation_id"]] = {
                        "snapshot_id": observation["snapshot_id"],
                        "open_order_ids": order_ids,
                        "fetched_at": observation["fetched_at"],
                        "source_payload_sha256": source_values,
                        "wallet_quantities": wallet_quantities,
                    }
        matched_closing = valuation_portfolios.get(latest_valuation_id)
        if matched_closing is None:
            reasons.append("PORTFOLIO_VALUATION_SOURCE_MISMATCH")
        elif matched_closing["open_order_ids"] != valuation_reservations[latest_valuation_id]:
            reasons.append("OPEN_ORDER_RESERVATIONS_MISMATCH")
        else:
            open_orders_bound_to_latest_valuation = True
            evidence["portfolio"]["valuation_source_match_snapshot_id"] = matched_closing["snapshot_id"]

    if cash_history is not None:
        if (not isinstance(cash_history, dict)
                or type(cash_history.get("snapshot_id")) is not int
                or not isinstance(cash_history.get("payload_sha256"), str)
                or _SHA.fullmatch(cash_history["payload_sha256"]) is None
                or not isinstance(cash_history.get("currency"), str)
                or _ASSET.fullmatch(cash_history["currency"]) is None
                or cash_history.get("usable_for_risk") is not False):
            raise ReconciliationEvidenceError("Cash history has invalid provenance.")
        _timestamp(cash_history.get("observed_at"))
        evidence["cash_history"] = {
            "snapshot_id": cash_history["snapshot_id"],
            "currency": cash_history["currency"],
            "payload_sha256": cash_history["payload_sha256"],
        }
        warnings.append("CASH_HISTORY_IS_CASH_ONLY")

    seen_journals: dict[str, str] = {}
    intervals: list[tuple[int, int]] = []
    transaction_batches: dict[str, dict] = {}
    for page in transaction_pages:
        metadata, rows = validate_transaction_page(page)
        evidence["transaction_pages"].append(metadata)
        intervals.append((metadata["start_time_ms"], metadata["end_time_ms"]))
        batch = metadata.get("batch")
        if batch is not None:
            batch_id = batch["batch_id"]
            batch_state = transaction_batches.setdefault(
                batch_id, {"metadata": batch, "snapshot_ids": set()})
            if batch_state["metadata"] != batch:
                contradictory.append("TRANSACTION_BATCH_METADATA_CONFLICT")
            batch_state["snapshot_ids"].add(metadata["snapshot_id"])
        if metadata["page_limit_reached"]:
            reasons.append("TRANSACTION_PAGE_AT_LIMIT")
        for item in rows:
            identity = {key: item[key] for key in _EVENT_IDENTITY_FIELDS}
            digest = _fingerprint(identity)
            prior = seen_journals.get(item["journal_id"])
            if prior is not None:
                if prior != digest:
                    contradictory.append("CONFLICTING_JOURNAL_ID")
                continue
            seen_journals[item["journal_id"]] = digest
            category = _CATEGORIES.get(item["journal_type"], "other")
            counts[category] += 1
            if len(examples[category]) < 5:
                examples[category].append(item["journal_id"])

    if not transaction_pages:
        reasons.append("TRANSACTION_HISTORY_MISSING")

    seen_trades: dict[tuple[str, str], str] = {}
    trade_rows: dict[tuple[str, str], dict] = {}
    trade_intervals: list[tuple[int, int]] = []
    trade_batches: dict[str, dict] = {}
    for page in trade_pages:
        metadata, rows = validate_trade_page(page)
        evidence["trade_pages"].append(metadata)
        trade_intervals.append((metadata["start_time_ns"], metadata["end_time_ns"]))
        batch = metadata.get("batch")
        if batch is not None:
            batch_id = batch["batch_id"]
            batch_state = trade_batches.setdefault(batch_id, {"metadata": batch, "snapshot_ids": set()})
            if batch_state["metadata"] != batch:
                contradictory.append("TRADE_BATCH_METADATA_CONFLICT")
            batch_state["snapshot_ids"].add(metadata["snapshot_id"])
        if metadata["page_limit_reached"]:
            reasons.append("TRADE_PAGE_AT_LIMIT")
        for item in rows:
            identity = (item["instrument_name"], item["trade_id"])
            digest = _fingerprint({key: value for key, value in item.items() if key != "payload_sha256"})
            previous = seen_trades.get(identity)
            if previous is not None:
                if previous != digest:
                    contradictory.append("CONFLICTING_TRADE_ID")
                continue
            seen_trades[identity] = digest
            trade_rows[identity] = item
    for batch_state in trade_batches.values():
        if len(batch_state["snapshot_ids"]) != batch_state["metadata"]["page_count"]:
            reasons.append("TRADE_BATCH_PAGE_MISSING")
    if not trade_pages:
        reasons.append("TRADE_HISTORY_MISSING")

    # Cross-check stable trade/order references only where the journal reports
    # the same execution. This does not infer asset postings or P&L.
    matched_trade_refs = 0
    unmatched_journal_trade_refs = 0
    if trade_pages:
        trade_page_windows = trade_intervals
        checked_journals: set[str] = set()
        for page in transaction_pages:
            _, journal_rows = validate_transaction_page(page)
            for item in journal_rows:
                if item["journal_type"] != "TRADING" or item["trade_id"] is None:
                    continue
                if item["journal_id"] in checked_journals:
                    continue
                checked_journals.add(item["journal_id"])
                event_ns = _epoch_ns(_timestamp(item["occurred_at"]))
                if valuation_pair and not (_epoch_ns(_timestamp(valuation_pair["start"]))
                                           <= event_ns < _epoch_ns(_timestamp(valuation_pair["end"]))):
                    continue
                if not any(start <= event_ns < end for start, end in trade_page_windows):
                    continue
                match = trade_rows.get((item["instrument_name"], item["trade_id"]))
                if match is None:
                    unmatched_journal_trade_refs += 1
                    continue
                matched_trade_refs += 1
                if ((item["order_id"] is not None and item["order_id"] != match["order_id"])
                        or (item["side"] is not None and item["side"] != match["side"])):
                    contradictory.append("TRADE_JOURNAL_IDENTITY_CONFLICT")
    if valuation_pair:
        start_time = _timestamp(valuation_pair["start"])
        end_time = _timestamp(valuation_pair["end"])
        start_ms = _epoch_ms(start_time)
        end_ms = _epoch_ms(end_time)
        funding_rows_by_direction = {}
        for direction in ("DEPOSIT", "WITHDRAWAL"):
            covering = [(metadata, rows) for metadata, rows in funding_candidates_by_direction.get(direction, [])
                        if metadata["complete"] and metadata["all_transfers_terminal"]
                        and metadata["start_time_ms"] <= start_ms and metadata["end_time_ms"] >= end_ms]
            if covering:
                selected, selected_rows = max(covering, key=lambda pair: _timestamp(pair[0]["observed_at"]))
                funding_by_direction[direction] = selected
                funding_rows_by_direction[direction] = selected_rows
                funding_coverage_snapshot_ids[direction] = selected["snapshot_id"]
        funding_coverage_verified = all(direction in funding_by_direction
                                        for direction in ("DEPOSIT", "WITHDRAWAL"))
        if funding_coverage_verified:
            for direction, rows in funding_rows_by_direction.items():
                count_key = "deposits" if direction == "DEPOSIT" else "withdrawals"
                complete_key = "completed_deposits" if direction == "DEPOSIT" else "completed_withdrawals"
                in_window = [item for item in rows
                             if start_ms <= _epoch_ms(_timestamp(item["created_at"])) < end_ms]
                funding_event_counts[count_key] += len(in_window)
                terminal_status = "1" if direction == "DEPOSIT" else "5"
                funding_event_counts[complete_key] += sum(item["status"] == terminal_status for item in in_window)
        if funding_history and not funding_coverage_verified:
            reasons.append("FUNDING_HISTORY_WINDOW_GAPS")
        coverage_end = start_ms
        gaps = []
        for page_start, page_end in sorted(intervals):
            if page_end <= coverage_end or page_start >= end_ms:
                continue
            if page_start > coverage_end:
                gaps.append({"start_ms": coverage_end, "end_ms": min(page_start, end_ms)})
            coverage_end = max(coverage_end, min(page_end, end_ms))
            if coverage_end >= end_ms:
                break
        if coverage_end < end_ms:
            gaps.append({"start_ms": coverage_end, "end_ms": end_ms})
        if gaps:
            reasons.append("TRANSACTION_WINDOW_GAPS")
        start_ns, end_ns = _epoch_ns(start_time), _epoch_ns(end_time)
        trade_coverage_end = start_ns
        trade_gaps = []
        for page_start, page_end in sorted(trade_intervals):
            if page_end <= trade_coverage_end or page_start >= end_ns:
                continue
            if page_start > trade_coverage_end:
                trade_gaps.append({"start_ns": trade_coverage_end, "end_ns": min(page_start, end_ns)})
            trade_coverage_end = max(trade_coverage_end, min(page_end, end_ns))
            if trade_coverage_end >= end_ns:
                break
        if trade_coverage_end < end_ns:
            trade_gaps.append({"start_ns": trade_coverage_end, "end_ns": end_ns})
        if trade_gaps:
            reasons.append("TRADE_WINDOW_GAPS")
        transaction_pagination_verified = any(
            state["metadata"]["coverage_complete"]
            and not state["metadata"]["incomplete_intervals"]
            and state["metadata"]["query_start_time_ms"] <= start_ms
            and state["metadata"]["query_end_time_ms"] >= end_ms
            and len(state["snapshot_ids"]) == state["metadata"]["page_count"]
            for state in transaction_batches.values())
        trade_pagination_verified = any(
            state["metadata"]["coverage_complete"]
            and not state["metadata"]["incomplete_intervals"]
            and state["metadata"]["query_start_time_ns"] <= start_ns
            and state["metadata"]["query_end_time_ns"] >= end_ns
            and len(state["snapshot_ids"]) == state["metadata"]["page_count"]
            for state in trade_batches.values())
        journal_trade_keys: set[tuple[str, str]] = set()
        journal_trade_missing_id = False
        window_journal_types: dict[str, str] = {}
        for transaction_page in transaction_pages:
            _, page_rows = validate_transaction_page(transaction_page)
            for item in page_rows:
                occurred_ns = _epoch_ns(_timestamp(item["occurred_at"]))
                if not start_ns <= occurred_ns < end_ns:
                    continue
                window_journal_types[item["journal_id"]] = item["journal_type"]
                if item["journal_type"] != "TRADING":
                    continue
                if item["trade_id"] is None:
                    journal_trade_missing_id = True
                else:
                    journal_trade_keys.add((item["instrument_name"], item["trade_id"]))
        window_trade_keys = {
            identity for identity, item in trade_rows.items()
            if start_ns <= int(item["timestamp_ns"]) < end_ns
        }
        executed_trades_reconciled = (
            transaction_pagination_verified and trade_pagination_verified
            and not gaps and not trade_gaps and not journal_trade_missing_id
            and journal_trade_keys == window_trade_keys and unmatched_journal_trade_refs == 0
        )
        window_types = set(window_journal_types.values())
        no_unknown_window_events = all(_CATEGORIES.get(kind, "other") != "other" for kind in window_types)
        conversion_legs_reconciled = (
            transaction_pagination_verified and not gaps and no_unknown_window_events
            and not window_types.intersection({"AUTO_CONVERSION", "MANUAL_CONVERSION"})
        )
        fee_postings_reconciled = (
            executed_trades_reconciled and funding_coverage_verified
            and not window_types and not window_trade_keys
            and funding_event_counts["deposits"] == 0 and funding_event_counts["withdrawals"] == 0
        )
        opening_portfolio = valuation_portfolios.get(valuations[1]["valuation_id"])
        closing_portfolio = valuation_portfolios.get(valuations[0]["valuation_id"])
        snapshots_bound = (opening_portfolio is not None and closing_portfolio is not None
            and opening_portfolio["snapshot_id"] != closing_portfolio["snapshot_id"]
            and opening_portfolio["wallet_quantities"] and closing_portfolio["wallet_quantities"])
        no_external_flows = (funding_coverage_verified
            and funding_event_counts["deposits"] == 0 and funding_event_counts["withdrawals"] == 0)
        evidence_complete_for_asset_ledger = (snapshots_bound and transaction_pagination_verified and not gaps
            and trade_pagination_verified and not trade_gaps and no_external_flows)
        if (evidence_complete_for_asset_ledger and not window_trade_keys and not window_types
                and opening_portfolio["wallet_quantities"] == closing_portfolio["wallet_quantities"]):
            per_asset_postings_reconciled = True
            fee_postings_reconciled = True
        elif (evidence_complete_for_asset_ledger and window_trade_keys
              and window_types == {"TRADING"}):
            window_trades = [item for item in trade_rows.values()
                if start_ns <= int(item["timestamp_ns"]) < end_ns]
            trade_postings = spot_trade_postings([{
                "trade_id": item["trade_id"], "instrument_name": item["instrument_name"],
                "side": item["side"], "quantity": item["quantity"], "price": item["price"],
                "fees": item["fees"], "fee_currency": item["fee_currency"],
                "source": item["source"], "payload_sha256": item["payload_sha256"],
            } for item in window_trades])
            asset_ledger_evidence = reconcile_asset_quantities(
                opening={asset: format(quantity, "f")
                         for asset, quantity in opening_portfolio["wallet_quantities"].items()},
                closing={asset: format(quantity, "f")
                         for asset, quantity in closing_portfolio["wallet_quantities"].items()},
                postings=trade_postings,
            )
            per_asset_postings_reconciled = asset_ledger_evidence["status"] == "MATCHED_ARITHMETIC_ONLY"
            fee_postings_reconciled = per_asset_postings_reconciled
        if (per_asset_postings_reconciled and fee_postings_reconciled
                and conversion_legs_reconciled and no_external_flows
                and snapshots_bound and not contradictory
                and newer["base_currency"] == older["base_currency"]):
            opening_equity = _decimal(older["total_equity"])
            closing_equity = _decimal(newer["total_equity"])
            if opening_equity < 0 or closing_equity < 0:
                raise ReconciliationEvidenceError("Valuation equity cannot be negative.")
            period_pnl = {
                "amount": format(closing_equity - opening_equity, "f"),
                "currency": newer["base_currency"],
                "start": older["as_of"], "end": newer["as_of"],
                "opening_valuation_id": older["valuation_id"],
                "closing_valuation_id": newer["valuation_id"],
                "opening_valuation_sha256": older["stored_payload_sha256"],
                "closing_valuation_sha256": newer["stored_payload_sha256"],
                "authority_status": "RECONCILED_PERIOD_OBSERVATION_ONLY",
                "daily_pnl": False,
            }
    else:
        gaps = []
        trade_gaps = []
        funding_coverage_verified = False
        transaction_pagination_verified = False
        trade_pagination_verified = False

    # Spot trade legs are only mapped in the tightly covered branch above.
    # Funding, conversions, other journal categories, and daily Risk P&L remain
    # blocked until each event type has explicit signed asset semantics.
    if unmatched_journal_trade_refs:
        reasons.append("TRANSACTION_TRADING_JOURNAL_UNMATCHED")
    if not executed_trades_reconciled:
        reasons.append("EXECUTED_TRADES_NOT_FULLY_RECONCILED")
    if not per_asset_postings_reconciled:
        reasons.append("PER_ASSET_POSTINGS_MISSING")
    if not conversion_legs_reconciled:
        reasons.append("CONVERSION_LEGS_UNMATCHED")
    if not fee_postings_reconciled:
        reasons.append("FEE_CURRENCY_AND_POSTINGS_MISSING")
    if len(valuations) >= 2:
        opening = valuation_portfolios.get(valuations[1]["valuation_id"])
        closing = valuation_portfolios.get(valuations[0]["valuation_id"])
        if (opening is None or closing is None or opening["snapshot_id"] == closing["snapshot_id"]):
            reasons.append("OPENING_CLOSING_BALANCES_UNPROVEN")
    if not (transaction_pagination_verified and trade_pagination_verified):
        reasons.append("COMPLETE_PROVIDER_PAGINATION_UNPROVEN")
    if not funding_coverage_verified:
        reasons.append("DEPOSIT_WITHDRAWAL_PROOF_MISSING")
    reasons.append("PNL_NOT_COMPUTED")
    if contradictory:
        reasons.extend(contradictory)
    reasons = list(dict.fromkeys(reasons))
    report = {
        "status": "BLOCKED_CONTRADICTORY" if contradictory else "BLOCKED_INCOMPLETE",
        "authority_status": "RECONCILIATION_EVIDENCE_ONLY",
        "usable_for_pnl": False, "usable_for_risk": False,
        "usable_for_execution": False, "orders_enabled": False,
        "valuation_pair": valuation_pair,
        "period_pnl": period_pnl,
        "daily_pnl_observation": None,
        "evidence": evidence, "event_counts": counts,
        "warnings": warnings,
        "cash_history_used_as_equity": False,
        "funding_history_counts": funding_event_counts,
        "funding_history_coverage_verified": funding_coverage_verified,
        "funding_history_coverage_snapshot_ids": funding_coverage_snapshot_ids,
        "provider_pagination_verified": transaction_pagination_verified and trade_pagination_verified,
        "open_order_count": len(portfolio_open_order_ids),
        "open_orders_bound_to_latest_valuation": open_orders_bound_to_latest_valuation,
        "executed_trades_reconciled": executed_trades_reconciled,
        "conversion_legs_reconciled": conversion_legs_reconciled,
        "fee_postings_reconciled": fee_postings_reconciled,
        "per_asset_postings_reconciled": per_asset_postings_reconciled,
        "asset_ledger": (None if asset_ledger_evidence is None else {
            "status": asset_ledger_evidence["status"],
            "posting_count": asset_ledger_evidence["posting_count"],
            "residual_quantities": asset_ledger_evidence["residual_quantities"],
            "evidence_sha256": asset_ledger_evidence["evidence_sha256"],
            "authority_status": asset_ledger_evidence["authority_status"],
        }),
        "executed_trade_count": len(trade_rows), "matched_trade_journal_references": matched_trade_refs,
        "unmatched_journal_trade_references": unmatched_journal_trade_refs,
        "example_journal_ids": examples, "distinct_journals": len(seen_journals),
        "coverage_gaps": gaps, "trade_coverage_gaps": trade_gaps, "reasons": reasons,
        "pnl": None, "net_deposits": None, "net_withdrawals": None,
        "net_conversions": None, "net_fees": None,
    }
    if _include_daily_observation and period_pnl is not None and portfolio is not None:
        # Risk's daily loss window is anchored to UTC midnight. Use an opening
        # valuation at or immediately after that boundary and the latest
        # valuation; a prior-day tick must never leak into today's P&L.
        close_time = _timestamp(valuations[0]["as_of"])
        day_start = close_time.replace(hour=0, minute=0, second=0, microsecond=0)
        opening_candidates = []
        for candidate in valuations[1:]:
            candidate_time = _timestamp(candidate["as_of"])
            delay = (candidate_time - day_start).total_seconds()
            if 0 <= delay <= 30:
                opening_candidates.append((delay, candidate))
        if opening_candidates:
            opening = min(opening_candidates, key=lambda item: item[0])[1]
            daily_report = audit_reconciliation(
                valuations=[valuations[0], opening],
                transaction_pages=transaction_pages,
                portfolio=portfolio,
                cash_history=cash_history,
                trade_pages=trade_pages,
                funding_history=funding_history,
                portfolio_history=portfolio_history,
                _include_daily_observation=False,
            )
            daily_period = daily_report.get("period_pnl")
            if daily_period is not None and daily_period.get("daily_pnl") is False:
                report["daily_pnl_observation"] = {
                    **daily_period,
                    "authority_status": "RECONCILED_UTC_DAY_OBSERVATION_ONLY",
                    "daily_pnl": True,
                    "usable_for_risk": False,
                    "usable_for_execution": False,
                    "orders_enabled": False,
                    "reconciliation_sha256": daily_report["evidence_sha256"],
                }
    report["evidence_sha256"] = _fingerprint(report)
    return report


__all__ = ["ReconciliationEvidenceError", "audit_reconciliation", "validate_transaction_page"]
