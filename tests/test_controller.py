from unittest.mock import MagicMock

from cleanroom.controller import MAX_DAILY_NOTIONAL_USD, evaluate, execute
from cleanroom.schemas import Decision, OrderType, Side, TradeIntent


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


def _clients(current_price: float = 150.0, todays_orders=None):
    trading_client = MagicMock()
    trading_client.get_orders.return_value = todays_orders or []

    data_client = MagicMock()
    data_client.get_stock_latest_trade.return_value = {"AAPL": MagicMock(price=current_price)}
    return trading_client, data_client


def test_no_intent_is_an_immediate_pass():
    trading_client, data_client = _clients()
    verdict = evaluate(None, trading_client, data_client)
    assert verdict.final == Decision.PASS
    assert verdict.approved_notional == 0.0
    trading_client.get_orders.assert_not_called()


def test_symbol_outside_whitelist_is_vetoed():
    trading_client, data_client = _clients()
    verdict = evaluate(_intent(symbol="GME", stop_loss_price=1.0), trading_client, data_client)
    assert verdict.final == Decision.VETO
    assert verdict.approved_notional == 0.0
    assert any(c.name == "SYMBOL_WHITELIST" and not c.passed for c in verdict.checks)
    # Rejected before any market-data check is attempted for a symbol we won't trade.
    data_client.get_stock_latest_trade.assert_not_called()


def test_stop_on_wrong_side_of_price_is_vetoed():
    trading_client, data_client = _clients(current_price=150.0)
    intent = _intent(side=Side.BUY, stop_loss_price=160.0)  # stop above entry for a BUY
    verdict = evaluate(intent, trading_client, data_client)
    assert verdict.final == Decision.VETO
    assert any(c.name == "STOP_DIRECTION" and not c.passed for c in verdict.checks)


def test_notional_over_cap_is_vetoed():
    trading_client, data_client = _clients()
    intent = _intent(notional=MAX_DAILY_NOTIONAL_USD + 1, qty=100)
    verdict = evaluate(intent, trading_client, data_client)
    assert verdict.final == Decision.VETO
    assert any(c.name == "NOTIONAL_SANITY" and not c.passed for c in verdict.checks)


def test_daily_order_count_cap_is_vetoed():
    order = MagicMock(status=MagicMock(value="filled"), symbol="AAPL", notional=100.0,
                       qty=None, filled_qty=None, filled_avg_price=100.0, id="o1")
    trading_client, data_client = _clients(todays_orders=[order] * 5)
    verdict = evaluate(_intent(), trading_client, data_client)
    assert verdict.final == Decision.VETO
    assert any(c.name == "DAILY_ORDER_COUNT" and not c.passed for c in verdict.checks)


def test_all_checks_pass_yields_pass_and_approved_notional():
    trading_client, data_client = _clients()
    intent = _intent()
    verdict = evaluate(intent, trading_client, data_client)
    assert verdict.final == Decision.PASS
    assert verdict.approved_notional == intent.notional
    assert all(c.passed for c in verdict.checks)


def test_execute_submits_order_only_on_pass():
    trading_client, data_client = _clients()
    intent = _intent()
    verdict = evaluate(intent, trading_client, data_client)

    trading_client.submit_order.return_value = MagicMock(id="order-123")
    result = execute(verdict, trading_client)

    trading_client.submit_order.assert_called_once()
    assert "order-123" in result


def test_execute_is_a_noop_on_veto():
    trading_client, data_client = _clients()
    verdict = evaluate(_intent(symbol="GME", stop_loss_price=1.0), trading_client, data_client)

    result = execute(verdict, trading_client)

    trading_client.submit_order.assert_not_called()
    assert "no order submitted" in result
