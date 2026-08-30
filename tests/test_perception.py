import json
from types import SimpleNamespace

import pytest

from cleanroom import perception
from cleanroom.perception import PerceptionError, perceive


def _fake_client(response_text: str | None):
    response = SimpleNamespace(text=response_text)
    models = SimpleNamespace(generate_content=lambda **kwargs: response)
    return SimpleNamespace(models=models)


@pytest.fixture(autouse=True)
def gemini_api_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(PerceptionError, match="GEMINI_API_KEY"):
        perceive("some text", source_ref="test")


def test_valid_response_is_parsed_and_source_ref_is_rebound(monkeypatch):
    raw = json.dumps(
        {
            "symbols": ["AAPL"],
            "sentiment": "bullish",
            "extracted_claims": [],
            "entities": ["Tim Cook"],
            "source_ref": "attacker-supplied-ref",
        }
    )
    monkeypatch.setattr(perception.genai, "Client", lambda api_key: _fake_client(raw))

    result = perceive("Apple beat earnings.", source_ref="news/benign_01.txt")

    assert result.symbols == ["AAPL"]
    assert result.sentiment.value == "bullish"
    # source_ref is bound by the caller, never trusted from model output
    assert result.source_ref == "news/benign_01.txt"


def test_empty_response_raises(monkeypatch):
    monkeypatch.setattr(perception.genai, "Client", lambda api_key: _fake_client(None))
    with pytest.raises(PerceptionError, match="Empty response"):
        perceive("text", source_ref="test")


def test_schema_violation_raises(monkeypatch):
    raw = json.dumps({"symbols": ["not-a-ticker!"], "sentiment": "bullish", "source_ref": "x"})
    monkeypatch.setattr(perception.genai, "Client", lambda api_key: _fake_client(raw))
    with pytest.raises(PerceptionError, match="schema invariant failed"):
        perceive("text", source_ref="test")


def test_inference_error_is_wrapped(monkeypatch):
    def _raise(**kwargs):
        raise RuntimeError("upstream timeout")

    client = SimpleNamespace(models=SimpleNamespace(generate_content=_raise))
    monkeypatch.setattr(perception.genai, "Client", lambda api_key: client)
    with pytest.raises(PerceptionError, match="Perception inference error"):
        perceive("text", source_ref="test")
