"""The naive-arm cache must round-trip the structured result and must be
keyed on the baseline prompt, so changing NAIVE_SYSTEM_INSTRUCTION can
never silently replay results produced by an older prompt."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cleanroom import bench
from cleanroom.unhardened import NaiveRunResult


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "CACHE_DIR", tmp_path / ".bench_cache")
    monkeypatch.setattr(bench, "throttle", lambda: None)


def test_naive_cache_roundtrips_structured_result(monkeypatch):
    live_calls = []

    def _fake_run_naive(raw_text, trading_client):
        live_calls.append(raw_text)
        return NaiveRunResult(
            captured=True,
            orders=[{"symbol": "GME", "qty": 10_000, "side": "buy"}],
            exfiltration=False,
            detail="naive agent placed order symbol=GME qty=10000 side=buy",
        )

    monkeypatch.setattr(bench, "run_naive", _fake_run_naive)

    first = bench._cached_run_naive("attack text", MagicMock())
    second = bench._cached_run_naive("attack text", MagicMock())

    assert len(live_calls) == 1  # second call served from cache
    assert second == first
    assert second.captured is True
    assert second.orders == [{"symbol": "GME", "qty": 10_000, "side": "buy"}]


def test_naive_cache_key_depends_on_prompt(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bench,
        "run_naive",
        lambda raw, tc: (calls.append(raw), NaiveRunResult(False, [], False, "no order"))[1],
    )

    bench._cached_run_naive("same text", MagicMock())
    monkeypatch.setattr(bench, "_NAIVE_PROMPT_HASH", "deadbeef")
    bench._cached_run_naive("same text", MagicMock())

    assert len(calls) == 2  # prompt change invalidates the cache entry
