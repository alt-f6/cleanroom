"""
Plane 3 (partial) — Strategy. Deterministic stub for Phase 1.

Takes AirlockedPerception (untrusted-origin, tagged) plus live market data
(trusted, fetched directly from Alpaca) and proposes a TradeIntent — or
None if no trade is warranted.

Hard rule, deliberately inflexible for Phase 1: text can influence ONLY the
side (via sentiment) and WHETHER we trade at all (via symbol count / neutral
check). Text can never influence notional or stop distance — those are
fixed constants and a market-price-derived calculation, respectively. This
closes the "manipulate sizing" attack class structurally, not by detection:
there is no code path from extracted_claims to position size.

A real (learned/tuned) strategy is Phase 2 work. This stub exists only to
close the end-to-end path for Phase 1's tracer bullet.
"""
from __future__ import annotations

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

from cleanroom.schemas import AirlockedPerception, OrderType, Sentiment, Side, TradeIntent

# Fixed by design — not derived from text, not configurable per-request.
# Changing these requires a code change and a new commit, not a text input.
FIXED_NOTIONAL_USD = 600.0
STOP_LOSS_PCT = 0.02  # 2% away from current price, opposite the trade direction


class StrategyError(Exception):
    """Raised when strategy cannot safely evaluate an AirlockedPerception
    (e.g. market data unavailable). Callers must not fall back to guessing
    a price — no trade is safer than a trade sized on unknown data."""


def _get_current_price(symbol: str, data_client: StockHistoricalDataClient) -> float:
    """
    Fetches the latest trade price directly from Alpaca — this value's
    provenance is MARKET_DATA, never UNTRUSTED_TEXT. It is never read from
    anything Perception produced.
    """
    request = StockLatestTradeRequest(symbol_or_symbols=symbol)
    trade = data_client.get_stock_latest_trade(request)
    try:
        return float(trade[symbol].price)
    except (KeyError, AttributeError) as e:
        raise StrategyError(f"could not fetch current price for {symbol}: {e}") from e


def decide(
    airlocked: AirlockedPerception,
    data_client: StockHistoricalDataClient,
) -> TradeIntent | None:
    payload = airlocked.payload

    if len(payload.symbols) != 1:
        return None
    if payload.sentiment == Sentiment.NEUTRAL:
        return None

    symbol = payload.symbols[0]
    side = Side.BUY if payload.sentiment == Sentiment.BULLISH else Side.SELL

    current_price = _get_current_price(symbol, data_client)

    # Target notional is fixed, never text-derived. Convert to a whole
    # share count NOW, in the same function that fetched the price used
    # for the stop calculation below — one price fetch, one conversion,
    # no chance for qty and notional to be computed against different
    # prices at different points in the pipeline.
    qty = int(FIXED_NOTIONAL_USD // current_price)
    if qty < 1:
        # Target notional doesn't cover a single share at the live price.
        # No trade, rather than silently rounding up past the target.
        return None

    actual_notional = qty * current_price

    if side == Side.BUY:
        stop_loss_price = round(current_price * (1 - STOP_LOSS_PCT), 2)
    else:
        stop_loss_price = round(current_price * (1 + STOP_LOSS_PCT), 2)

    return TradeIntent(
        symbol=symbol,
        side=side,
        qty=qty,
        notional=round(actual_notional, 2),
        order_type=OrderType.MARKET,
        limit_price=None,
        stop_loss_price=stop_loss_price,
        thesis_ref=airlocked.airlock_id,
    )