"""_process_article must never let an exception escape and kill the
polling loop — every failure mode becomes an audit event instead."""
from __future__ import annotations

import json
from datetime import datetime, timezone
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


# ─────────────────────────────────────────────────────────────────────────────
# Dedup / state persistence
# ─────────────────────────────────────────────────────────────────────────────
def test_advance_state_preserves_prior_seen_ids():
    # Regression: an earlier version dropped every previously seen id when
    # recording a new one, so any already-processed article could be
    # re-fetched and re-traded on the next poll.
    state = {"last_seen_at": None, "last_seen_ids": ["a1", "a2"]}
    daemon._advance_state(state, "a3", datetime(2026, 9, 4, tzinfo=timezone.utc))
    assert state["last_seen_ids"] == ["a1", "a2", "a3"]


def test_advance_state_deduplicates_and_bounds_seen_ids():
    state = {"last_seen_at": None, "last_seen_ids": []}
    for i in range(daemon.SEEN_IDS_MAX + 25):
        daemon._advance_state(state, f"id-{i}", None)
        daemon._advance_state(state, f"id-{i}", None)  # repeat must not duplicate
    ids = state["last_seen_ids"]
    assert len(ids) == daemon.SEEN_IDS_MAX
    assert len(ids) == len(set(ids))
    assert ids[-1] == f"id-{daemon.SEEN_IDS_MAX + 24}"  # newest retained, oldest evicted


def test_last_seen_at_advances_monotonically():
    later = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    earlier = datetime(2026, 9, 4, 11, 0, 0, tzinfo=timezone.utc)
    state = {"last_seen_at": later.isoformat(), "last_seen_ids": []}

    daemon._advance_state(state, "old", earlier)  # older article must not rewind
    assert state["last_seen_at"] == later.isoformat()

    daemon._advance_state(state, "same", later)  # identical timestamp: cursor stays put
    assert state["last_seen_at"] == later.isoformat()

    newest = datetime(2026, 9, 4, 13, 0, 0, tzinfo=timezone.utc)
    daemon._advance_state(state, "new", newest)
    assert state["last_seen_at"] == newest.isoformat()


def test_state_roundtrip_and_corrupt_file_recovery(tmp_path):
    path = tmp_path / "state.json"
    state = {"last_seen_at": "2026-09-04T12:00:00+00:00", "last_seen_ids": ["a1", "a2"]}
    daemon._save_state(path, state)
    assert daemon._load_state(path) == state
    assert not path.with_suffix(path.suffix + ".tmp").exists()  # atomic temp cleaned up

    path.write_text("{ truncated garbage", encoding="utf-8")
    assert daemon._load_state(path) == {"last_seen_at": None, "last_seen_ids": []}


def test_fetch_filters_seen_and_in_batch_duplicate_ids():
    ts = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    articles = [
        {"id": "seen-before", "created_at": ts},
        {"id": "fresh-1", "created_at": ts},
        {"id": "fresh-1", "created_at": ts},  # duplicate within the same batch
        {"id": "fresh-2", "created_at": ts},
    ]
    news_client = MagicMock()
    news_client.get_news.return_value = {"news": articles}
    state = {"last_seen_at": ts.isoformat(), "last_seen_ids": ["seen-before"]}

    fresh = daemon._fetch_new_articles(news_client, ["AAPL"], state)

    assert [a["id"] for a in fresh] == ["fresh-1", "fresh-2"]


def test_restart_never_reprocesses_boundary_articles(tmp_path):
    # Simulates a restart: articles sharing last_seen_at's exact timestamp
    # are re-fetched (inclusive start) but must be filtered out by id.
    ts = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    path = tmp_path / "state.json"
    state = {"last_seen_at": None, "last_seen_ids": []}
    for aid in ("a1", "a2"):
        daemon._advance_state(state, aid, ts)
        daemon._save_state(path, state)

    reloaded = daemon._load_state(path)
    news_client = MagicMock()
    news_client.get_news.return_value = {
        "news": [{"id": "a1", "created_at": ts}, {"id": "a2", "created_at": ts}]
    }
    assert daemon._fetch_new_articles(news_client, ["AAPL"], reloaded) == []
