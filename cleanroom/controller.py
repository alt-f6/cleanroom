"""
Plane 3 — Execution Controller. Trusted, deterministic code, zero LLM.

Never sees raw text. Only receives a TradeIntent (already produced by
Strategy from AirlockedPerception + market data) and live numeric data
fetched directly from Alpaca. Produces a ControllerVerdict.

The BLOCK/VETO decision is made here, in code — never by a model. This is
what makes "veto cannot be explained as a hallucination" a true statement.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest, StopLossRequest
from pydantic import ValidationError

from cleanroom.schemas import (
    AirlockedPerception,
    CheckResult,
    ControllerVerdict,
    Decision,
    Provenance,
    Severity,
    Side,
    TradeIntent,
)

# Fixed invariants. Changing these requires a code change, not a text input.
SYMBOL_WHITELIST = {"AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "SPY"}
MAX_DAILY_ORDERS = 5
MAX_DAILY_NOTIONAL_USD = 1000.0

logger = logging.getLogger(__name__)

# CheckResult.detail is capped at 300 chars; exception strings (especially
# HTTP error bodies) routinely exceed that, and a fail-closed check must
# never itself fail validation over the length of its own explanation.
_DETAIL_MAX = 290


def _clip(text: str) -> str:
    return text if len(text) <= _DETAIL_MAX else text[: _DETAIL_MAX - 3] + "..."


def _check_airlock_binding(intent: TradeIntent, airlocked: AirlockedPerception) -> CheckResult:
    """The intent must provably originate from the AIRLOCK payload presented
    alongside it: thesis_ref must equal that payload's airlock_id, and a
    text-derived payload must still carry UNTRUSTED_TEXT provenance — a
    payload claiming trusted provenance is spoofing, not a variation."""
    bound = intent.thesis_ref == airlocked.airlock_id
    provenance_ok = airlocked.provenance == Provenance.UNTRUSTED_TEXT
    passed = bound and provenance_ok
    if passed:
        detail = f"intent bound to airlock_id={airlocked.airlock_id}, provenance={airlocked.provenance.value}"
    elif not bound:
        detail = (
            f"thesis_ref={intent.thesis_ref!r} does not match the presented "
            f"airlock_id={airlocked.airlock_id} — intent is not derived from this payload"
        )
    else:
        detail = f"text-derived payload carries non-text provenance: {airlocked.provenance.value}"
    return CheckResult(
        name="AIRLOCK_BINDING",
        passed=passed,
        severity=Severity.BLOCK,
        detail=_clip(detail),
    )


def _airlock_anomaly_audit(airlocked: AirlockedPerception) -> CheckResult:
    """Surfaces AIRLOCK's anomaly flags in the verdict's own audit trail.
    Deliberately WARN severity and always passed: flags are visibility, not
    a detection gate — the security guarantee stays in the BLOCK invariants
    (see airlock.py's note on avoiding a detection arms race)."""
    if airlocked.anomalies:
        detail = _clip(
            f"{len(airlocked.anomalies)} AIRLOCK anomaly flag(s): "
            + "; ".join(airlocked.anomalies)
        )
    else:
        detail = "no AIRLOCK anomaly flags"
    return CheckResult(
        name="AIRLOCK_ANOMALY_AUDIT",
        passed=True,
        severity=Severity.WARN,
        detail=detail,
    )


def _check_symbol_whitelist(intent: TradeIntent) -> CheckResult:
    passed = intent.symbol in SYMBOL_WHITELIST
    return CheckResult(
        name="SYMBOL_WHITELIST",
        passed=passed,
        severity=Severity.BLOCK,
        detail=(
            f"{intent.symbol} is in the allowed universe"
            if passed
            else f"{intent.symbol} is NOT in the allowed universe {sorted(SYMBOL_WHITELIST)}"
        ),
    )


def _check_stop_direction(intent: TradeIntent, current_price: float) -> CheckResult:
    """
    The invariant we deliberately kept out of the Pydantic schema: a stop
    must actually protect the position, which requires knowing the live
    market price. BUY -> stop must be below entry. SELL -> stop must be
    above entry.
    """
    if intent.side == Side.BUY:
        passed = intent.stop_loss_price < current_price
        rule = "stop must be below current price for a BUY"
    else:
        passed = intent.stop_loss_price > current_price
        rule = "stop must be above current price for a SELL"

    return CheckResult(
        name="STOP_DIRECTION",
        passed=passed,
        severity=Severity.BLOCK,
        detail=(
            f"{rule}: stop={intent.stop_loss_price}, current={current_price} — OK"
            if passed
            else f"{rule}: stop={intent.stop_loss_price}, current={current_price} — VIOLATED"
        ),
    )


def _check_notional_sanity(intent: TradeIntent) -> CheckResult:
    """
    Independent re-check of notional sanity. Not because we distrust
    strategy.py specifically — because a single point of enforcement is
    not defense in depth. This check does not know or care that strategy
    happens to fix notional at $100; it enforces the controller's own
    ceiling regardless of what produced the intent.
    """
    value = intent.notional
    passed = math.isfinite(value) and 0 < value <= MAX_DAILY_NOTIONAL_USD
    return CheckResult(
        name="NOTIONAL_SANITY",
        passed=passed,
        severity=Severity.BLOCK,
        detail=f"notional={value} (must be finite, >0, <={MAX_DAILY_NOTIONAL_USD})",
    )


def _get_live_prices(
    symbols: set[str],
    data_client: StockHistoricalDataClient,
) -> dict[str, float]:
    """
    Batched market price lookup for a set of symbols.
    Handles partial failures gracefully: if a batch fails (e.g. invalid/delisted symbol),
    it logs a warning and falls back to individual lookups so valid symbols still resolve.
    """
    if not symbols:
        return {}

    price_map: dict[str, float] = {}

    try:
        request = StockLatestTradeRequest(symbol_or_symbols=list(symbols))
        trades = data_client.get_stock_latest_trade(request)
        for sym in symbols:
            trade = trades.get(sym)
            if trade and hasattr(trade, "price") and trade.price is not None:
                price_map[sym] = float(trade.price)
            else:
                logger.warning("No price record returned for symbol: %s", sym)
    except Exception as e:
        logger.warning(
            "Batch quote lookup failed (%s); falling back to individual symbol requests",
            e,
        )
        for sym in symbols:
            try:
                single_req = StockLatestTradeRequest(symbol_or_symbols=sym)
                single_trade = data_client.get_stock_latest_trade(single_req)
                if sym in single_trade and single_trade[sym].price is not None:
                    price_map[sym] = float(single_trade[sym].price)
            except Exception as single_err:
                logger.warning("Individual price fetch failed for %s: %s", sym, single_err)

    return price_map


def _check_daily_limits(
    intent: TradeIntent,
    trading_client: TradingClient,
    data_client: StockHistoricalDataClient,
) -> tuple[CheckResult, CheckResult]:
    """
    Evaluates daily order count and cumulative notional against Alpaca paper records.
    Uses nested=True so secondary OTO legs are nested under parent orders and not counted twice.
    """
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # nested=True keeps OTO stop legs attached under their parent order
    # instead of flattened into the top-level list, so counting below
    # doesn't double-count a parent + its own stop leg.
    request = GetOrdersRequest(
        status=QueryOrderStatus.ALL,
        after=today_start,
        nested=True,
    )
    todays_orders = trading_client.get_orders(request)

    dead_statuses = {"canceled", "rejected", "expired"}
    active_orders = [
        o for o in todays_orders
        if getattr(o.status, "value", str(o.status)).lower() not in dead_statuses
    ]

    order_count = len(active_orders)

    # Orders with no notional or fill price yet still need a market quote
    # to estimate their exposure toward today's cap.
    symbols_needing_price = {
        o.symbol for o in active_orders
        if o.symbol and (o.notional is None or float(o.notional) == 0) and not o.filled_avg_price
    }
    price_map = _get_live_prices(symbols_needing_price, data_client)

    notional_so_far = 0.0
    unpriced: list[str] = []
    for o in active_orders:
        if o.notional is not None and float(o.notional) > 0:
            notional_so_far += float(o.notional)
        elif o.qty is not None and float(o.qty) > 0:
            qty = float(o.filled_qty) if o.filled_qty and float(o.filled_qty) > 0 else float(o.qty)
            if o.filled_avg_price is not None and float(o.filled_avg_price) > 0:
                price = float(o.filled_avg_price)
            else:
                price = price_map.get(o.symbol, 0.0)
                if price == 0.0:
                    # Fail closed: an order we cannot price cannot be
                    # counted as $0 exposure — that would silently raise
                    # the effective daily cap by exactly the amount we
                    # failed to observe.
                    logger.warning("Unresolved pricing for order id=%s symbol=%s", o.id, o.symbol)
                    unpriced.append(f"id={o.id} symbol={o.symbol}")
                    continue
            notional_so_far += qty * price

    count_check = CheckResult(
        name="DAILY_ORDER_COUNT",
        passed=order_count < MAX_DAILY_ORDERS,
        severity=Severity.BLOCK,
        detail=f"{order_count}/{MAX_DAILY_ORDERS} orders placed today",
    )
    if unpriced:
        notional_check = CheckResult(
            name="DAILY_NOTIONAL_CAP",
            passed=False,
            severity=Severity.BLOCK,
            detail=_clip(
                "failing closed: could not determine exposure for open order(s) "
                + ", ".join(unpriced)
                + " — daily notional cannot be verified against the cap"
            ),
        )
    else:
        notional_check = CheckResult(
            name="DAILY_NOTIONAL_CAP",
            passed=(notional_so_far + intent.notional) <= MAX_DAILY_NOTIONAL_USD,
            severity=Severity.BLOCK,
            detail=(
                f"today so far: ${notional_so_far:.2f}, this order: ${intent.notional:.2f}, "
                f"cap: ${MAX_DAILY_NOTIONAL_USD:.2f}"
            ),
        )
    return count_check, notional_check


def _build_verdict(
    intent: TradeIntent | None,
    checks: list[CheckResult],
) -> ControllerVerdict:
    """Builds a ControllerVerdict, falling back to a hard VETO if the
    veto_is_contagious invariant ever rejects the result (i.e. a bug here)."""
    has_block_failure = any((not c.passed) and c.severity == Severity.BLOCK for c in checks)
    final = Decision.VETO if has_block_failure else Decision.PASS
    approved_notional = 0.0 if final == Decision.VETO else (intent.notional if intent else 0.0)
    reason = (
        "; ".join(c.detail for c in checks if not c.passed)
        if has_block_failure
        else "all checks passed"
    )

    try:
        return ControllerVerdict(
            intent=intent,
            checks=checks,
            final=final,
            approved_notional=approved_notional,
            reason=reason,
        )
    except ValidationError as e:
        return ControllerVerdict(
            intent=None,
            checks=checks + [CheckResult(
                name="VERDICT_INTEGRITY",
                passed=False,
                severity=Severity.BLOCK,
                detail=f"verdict construction failed, failing closed: {e}",
            )],
            final=Decision.VETO,
            approved_notional=0.0,
            reason="failing closed: verdict integrity violation",
        )


def evaluate(
    intent: TradeIntent | None,
    trading_client: TradingClient,
    data_client: StockHistoricalDataClient,
    airlocked: AirlockedPerception,
) -> ControllerVerdict:
    """
    The single entry point for Plane 3 decision-making. If intent is None
    (Strategy declined to propose a trade), returns an immediate PASS-shaped
    no-op verdict — there is nothing to veto if nothing was proposed.

    The AirlockedPerception the intent was derived from is MANDATORY. Two
    checks run unconditionally on every intent: AIRLOCK_BINDING (BLOCK —
    the intent must be bound to exactly this payload, with untrusted-text
    provenance) and AIRLOCK_ANOMALY_AUDIT (WARN — Plane 2's anomaly flags
    become part of the verdict's permanent audit trail). A caller that
    somehow presents no valid AirlockedPerception gets a fail-closed VETO,
    never a bypass: an intent whose provenance cannot be verified is
    indistinguishable from a forged one.

    Every external call (market data, order history) is fail-closed: an
    exception produces a failed BLOCK check — and therefore a VETO with
    approved_notional=0 — never a raised exception. An unreachable broker
    is indistinguishable from an unverifiable invariant, and unverifiable
    means VETO.
    """
    if intent is None:
        return ControllerVerdict(
            intent=None,
            checks=[],
            final=Decision.PASS,
            approved_notional=0.0,
            reason="no trade proposed by strategy",
        )

    checks: list[CheckResult] = []

    if not isinstance(airlocked, AirlockedPerception):
        checks.append(CheckResult(
            name="AIRLOCK_BINDING",
            passed=False,
            severity=Severity.BLOCK,
            detail=(
                "failing closed: no AirlockedPerception presented alongside the "
                "intent — provenance cannot be verified, unverifiable means VETO"
            ),
        ))
    else:
        checks.append(_check_airlock_binding(intent, airlocked))
        checks.append(_airlock_anomaly_audit(airlocked))

    checks.append(_check_symbol_whitelist(intent))

    # Only fetch the live price and evaluate stop direction if the symbol
    # is actually whitelisted — no reason to query market data (or trust
    # any downstream check) for a symbol we're going to reject anyway.
    if checks[-1].passed:
        try:
            request = StockLatestTradeRequest(symbol_or_symbols=intent.symbol)
            current_price = float(
                data_client.get_stock_latest_trade(request)[intent.symbol].price
            )
        except Exception as e:
            checks.append(CheckResult(
                name="MARKET_DATA_AVAILABILITY",
                passed=False,
                severity=Severity.BLOCK,
                detail=_clip(
                    f"failing closed: could not fetch live price for {intent.symbol}: {e}"
                ),
            ))
        else:
            checks.append(_check_stop_direction(intent, current_price))

    checks.append(_check_notional_sanity(intent))

    try:
        count_check, notional_check = _check_daily_limits(intent, trading_client, data_client)
    except Exception as e:
        checks.append(CheckResult(
            name="ORDER_HISTORY_AVAILABILITY",
            passed=False,
            severity=Severity.BLOCK,
            detail=_clip(
                f"failing closed: could not fetch today's order history: {e}"
            ),
        ))
    else:
        checks.append(count_check)
        checks.append(notional_check)

    return _build_verdict(intent, checks)


def execute(
    verdict: ControllerVerdict,
    trading_client: TradingClient,
) -> str:
    """
    Submits the order to Alpaca paper trading IF AND ONLY IF final == PASS.
    qty is taken directly from TradeIntent — strategy.py is the single
    place where notional-to-qty conversion happens, using the same price
    fetch that produced the stop-loss level. This function does not
    recompute or second-guess sizing; it only submits what was approved.
    """
    if verdict.final != Decision.PASS or verdict.intent is None:
        return "no order submitted (verdict was VETO or no intent)"

    intent = verdict.intent

    order_request = MarketOrderRequest(
        symbol=intent.symbol,
        qty=intent.qty,
        side=OrderSide.BUY if intent.side == Side.BUY else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.OTO,
        stop_loss=StopLossRequest(stop_price=intent.stop_loss_price),
    )

    order = trading_client.submit_order(order_request)
    return (
        f"order submitted: id={order.id}, symbol={intent.symbol}, "
        f"qty={intent.qty}, notional=${intent.notional:.2f}, "
        f"stop_price={intent.stop_loss_price}"
    )