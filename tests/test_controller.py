from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from cleanroom.controller import MAX_DAILY_NOTIONAL_USD, evaluate, execute
from cleanroom.schemas import (
    AirlockedPerception,
    Decision,
    OrderType,
    PerceptionOutput,
    Provenance,
    Sentiment,
    Side,
    TradeIntent,
)


def _airlocked(anomalies=None, provenance=Provenance.UNTRUSTED_TEXT) -> AirlockedPerception:
    payload = PerceptionOutput(symbols=["AAPL"], sentiment=Sentiment.BULLISH, source_ref="test")
    return AirlockedPerception(payload=payload, anomalies=anomalies or [], provenance=provenance)


def _intent(**overrides) -> TradeIntent:
    defaults = dict(
        symbol="AAPL",
        side=Side.BUY,
        qty=1,
        notional=150.0,
        order_type=OrderType.MARKET,
        stop_loss_price=145.0,
        thesis_ref="test",
    )
    defaults.update(overrides)
    return TradeIntent(**defaults)


def _bound_pair(**intent_overrides) -> tuple[AirlockedPerception, TradeIntent]:
    """An AirlockedPerception and a TradeIntent correctly bound to it —
    the shape every legitimate Strategy-produced intent has."""
    airlocked = _airlocked()
    intent_overrides.setdefault("thesis_ref", airlocked.airlock_id)
    return airlocked, _intent(**intent_overrides)


def _clients(current_price: float = 150.0, todays_orders=None):
    trading_client = MagicMock()
    trading_client.get_orders.return_value = todays_orders or []

    data_client = MagicMock()
    data_client.get_stock_latest_trade.return_value = {"AAPL": MagicMock(price=current_price)}
    return trading_client, data_client


def test_no_intent_is_an_immediate_pass():
    trading_client, data_client = _clients()
    verdict = evaluate(None, trading_client, data_client, airlocked=_airlocked())
    assert verdict.final == Decision.PASS
    assert verdict.approved_notional == 0.0
    trading_client.get_orders.assert_not_called()


def test_symbol_outside_whitelist_is_vetoed():
    trading_client, data_client = _clients()
    airlocked, intent = _bound_pair(symbol="GME", stop_loss_price=1.0)
    verdict = evaluate(intent, trading_client, data_client, airlocked=airlocked)
    assert verdict.final == Decision.VETO
    assert verdict.approved_notional == 0.0
    assert any(c.name == "SYMBOL_WHITELIST" and not c.passed for c in verdict.checks)
    # Rejected before any market-data check is attempted for a symbol we won't trade.
    data_client.get_stock_latest_trade.assert_not_called()


def test_stop_on_wrong_side_of_price_is_vetoed():
    trading_client, data_client = _clients(current_price=150.0)
    airlocked, intent = _bound_pair(side=Side.BUY, stop_loss_price=160.0)  # stop above entry for a BUY
    verdict = evaluate(intent, trading_client, data_client, airlocked=airlocked)
    assert verdict.final == Decision.VETO
    assert any(c.name == "STOP_DIRECTION" and not c.passed for c in verdict.checks)


def test_notional_over_cap_is_vetoed():
    trading_client, data_client = _clients()
    airlocked, intent = _bound_pair(notional=MAX_DAILY_NOTIONAL_USD + 1, qty=100)
    verdict = evaluate(intent, trading_client, data_client, airlocked=airlocked)
    assert verdict.final == Decision.VETO
    assert any(c.name == "NOTIONAL_SANITY" and not c.passed for c in verdict.checks)


def test_daily_order_count_cap_is_vetoed():
    order = MagicMock(status=MagicMock(value="filled"), symbol="AAPL", notional=100.0,
                       qty=None, filled_qty=None, filled_avg_price=100.0, id="o1")
    trading_client, data_client = _clients(todays_orders=[order] * 5)
    airlocked, intent = _bound_pair()
    verdict = evaluate(intent, trading_client, data_client, airlocked=airlocked)
    assert verdict.final == Decision.VETO
    assert any(c.name == "DAILY_ORDER_COUNT" and not c.passed for c in verdict.checks)


def test_all_checks_pass_yields_pass_and_approved_notional():
    trading_client, data_client = _clients()
    airlocked, intent = _bound_pair()
    verdict = evaluate(intent, trading_client, data_client, airlocked=airlocked)
    assert verdict.final == Decision.PASS
    assert verdict.approved_notional == intent.notional
    assert all(c.passed for c in verdict.checks)


def test_execute_submits_order_only_on_pass():
    trading_client, data_client = _clients()
    airlocked, intent = _bound_pair()
    verdict = evaluate(intent, trading_client, data_client, airlocked=airlocked)

    trading_client.submit_order.return_value = MagicMock(id="order-123")
    result = execute(verdict, trading_client)

    trading_client.submit_order.assert_called_once()
    assert "order-123" in result


def test_market_data_failure_fails_closed():
    trading_client, data_client = _clients()
    data_client.get_stock_latest_trade.side_effect = RuntimeError("connection reset by peer")
    airlocked, intent = _bound_pair()
    verdict = evaluate(intent, trading_client, data_client, airlocked=airlocked)
    assert verdict.final == Decision.VETO
    assert verdict.approved_notional == 0.0
    assert any(c.name == "MARKET_DATA_AVAILABILITY" and not c.passed for c in verdict.checks)


def test_order_history_failure_fails_closed():
    trading_client, data_client = _clients()
    trading_client.get_orders.side_effect = RuntimeError("HTTP 500 from broker")
    airlocked, intent = _bound_pair()
    verdict = evaluate(intent, trading_client, data_client, airlocked=airlocked)
    assert verdict.final == Decision.VETO
    assert verdict.approved_notional == 0.0
    assert any(c.name == "ORDER_HISTORY_AVAILABILITY" and not c.passed for c in verdict.checks)


def test_unpriceable_active_order_fails_closed():
    # An open order with qty but no notional, no fill price, and no quote
    # available must NOT be counted as $0 exposure toward the daily cap.
    order = MagicMock(status=MagicMock(value="accepted"), symbol="MSFT", notional=None,
                       qty=2.0, filled_qty=None, filled_avg_price=None, id="o1")
    trading_client, data_client = _clients(todays_orders=[order])
    airlocked, intent = _bound_pair()
    verdict = evaluate(intent, trading_client, data_client, airlocked=airlocked)
    assert verdict.final == Decision.VETO
    assert any(c.name == "DAILY_NOTIONAL_CAP" and not c.passed for c in verdict.checks)


def test_trade_intent_is_frozen():
    intent = _intent()
    with pytest.raises(ValidationError):
        intent.notional = 999_999.0


def test_airlock_binding_mismatch_is_vetoed():
    trading_client, data_client = _clients()
    airlocked = _airlocked()
    intent = _intent(thesis_ref="someone-elses-airlock-id")
    verdict = evaluate(intent, trading_client, data_client, airlocked=airlocked)
    assert verdict.final == Decision.VETO
    assert any(c.name == "AIRLOCK_BINDING" and not c.passed for c in verdict.checks)


def test_airlock_binding_match_passes():
    trading_client, data_client = _clients()
    airlocked, intent = _bound_pair()
    verdict = evaluate(intent, trading_client, data_client, airlocked=airlocked)
    assert verdict.final == Decision.PASS
    assert any(c.name == "AIRLOCK_BINDING" and c.passed for c in verdict.checks)


def test_missing_airlocked_fails_closed_with_veto():
    # airlocked is a mandatory parameter; a caller that somehow presents
    # None must get a fail-closed VETO, never a binding-check bypass.
    trading_client, data_client = _clients()
    verdict = evaluate(_intent(), trading_client, data_client, airlocked=None)
    assert verdict.final == Decision.VETO
    assert verdict.approved_notional == 0.0
    assert any(c.name == "AIRLOCK_BINDING" and not c.passed for c in verdict.checks)


def test_airlocked_is_positionally_mandatory():
    trading_client, data_client = _clients()
    with pytest.raises(TypeError):
        evaluate(_intent(), trading_client, data_client)


def test_spoofed_trusted_provenance_is_vetoed():
    # A text-derived payload claiming MARKET_DATA provenance is spoofing,
    # not a variation — the binding check must veto it even when the
    # thesis_ref matches the presented airlock_id.
    trading_client, data_client = _clients()
    airlocked = _airlocked(provenance=Provenance.MARKET_DATA)
    intent = _intent(thesis_ref=airlocked.airlock_id)
    verdict = evaluate(intent, trading_client, data_client, airlocked=airlocked)
    assert verdict.final == Decision.VETO
    assert any(c.name == "AIRLOCK_BINDING" and not c.passed for c in verdict.checks)


def test_airlock_anomalies_are_recorded_in_verdict():
    trading_client, data_client = _clients()
    airlocked = _airlocked(anomalies=["injection-pattern marker present in extracted text: 'system:'"])
    intent = _intent(thesis_ref=airlocked.airlock_id)
    verdict = evaluate(intent, trading_client, data_client, airlocked=airlocked)
    # Anomaly flags are an audit record, not a detection gate — they must
    # appear in the verdict but must not flip the decision by themselves.
    assert verdict.final == Decision.PASS
    audit = next(c for c in verdict.checks if c.name == "AIRLOCK_ANOMALY_AUDIT")
    assert "injection-pattern" in audit.detail


def test_execute_is_a_noop_on_veto():
    trading_client, data_client = _clients()
    airlocked, intent = _bound_pair(symbol="GME", stop_loss_price=1.0)
    verdict = evaluate(intent, trading_client, data_client, airlocked=airlocked)

    result = execute(verdict, trading_client)

    trading_client.submit_order.assert_not_called()
    assert "no order submitted" in result
