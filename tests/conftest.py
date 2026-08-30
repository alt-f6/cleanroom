"""Shared fixtures. No test here should ever hit a live network call —
Perception's Gemini client and Controller/Strategy's Alpaca clients are
always mocked."""
from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture
def make_trade():
    """Builds a minimal fake Alpaca "latest trade" response for a symbol."""

    def _make(symbol: str, price: float):
        return {symbol: SimpleNamespace(price=price)}

    return _make
