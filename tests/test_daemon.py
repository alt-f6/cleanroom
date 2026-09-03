"""_process_article must never let an exception escape and kill the
polling loop — every failure mode becomes an audit event instead."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from cleanroom import daemon
from cleanroom.schemas import PerceptionOutput, Sentiment

_ARTICLE = {
    "id": "a1",
    "created_at": "2026-09-04T00:00:00Z",
    "headline": "AAPL beats on earnings",
    "summary": "Strong quarter.",
}


def _clients(submit_side_effect=None):
    data_client = MagicMock()
    data_client.get_stock_latest_trade.return_value = {"AAPL": SimpleNamespace(price=150.0)}
    trading_client = MagicMock()
    trading_client.get_orders.return_value = []
    if submit_side_effect is not None:
        trading_client.submit_order.side_effect = submit_side_effect
    else:
        trading_client.submit_order.return_value = SimpleNamespace(id="ok-1")
    return trading_client, data_client


def test_process_article_survives_broker_submit_failure(tmp_path, monkeypatch):
    payload = PerceptionOutput(symbols=["AAPL"], sentiment=Sentiment.BULLISH, source_ref="t")
    monkeypatch.setattr(daemon, "_perceive_with_retry", lambda raw, ref: payload)
    trading_client, data_client = _clients(submit_side_effect=RuntimeError("broker 503"))
    audit_path = tmp_path / "audit.jsonl"

    event = daemon._process_article(_ARTICLE, trading_client, data_client, audit_path)

    assert event["outcome"] == "EXECUTION_ERROR"
    assert "broker 503" in event["detail"]
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["outcome"] == "EXECUTION_ERROR"


def test_process_article_survives_arbitrary_pipeline_failure(tmp_path, monkeypatch):
    def _boom(raw, ref):
        raise TypeError("unexpected shape from upstream")

    monkeypatch.setattr(daemon, "_perceive_with_retry", _boom)
    trading_client, data_client = _clients()
    audit_path = tmp_path / "audit.jsonl"

    event = daemon._process_article(_ARTICLE, trading_client, data_client, audit_path)

    assert event["outcome"] == "PIPELINE_ERROR"
    assert "unexpected shape" in event["detail"]
