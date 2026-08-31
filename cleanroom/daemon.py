"""
CLEANROOM daemon — autonomous news-driven operation.

Polls Alpaca's news feed on an interval, runs every new article through the
full hardened pipeline (Perception -> AIRLOCK -> Strategy -> Controller),
and appends one JSON line per article to an audit log. This is what closes
Phase 3's DoD: new journal entries appear over time with no manual `run`
invocation.

Live vs. dry-run is a narrower distinction than it might look:
  - News (NewsClient) and market prices (StockHistoricalDataClient) are
    ALWAYS fetched live from Alpaca, in both modes. Both are read-only and
    carry no execution risk, and faking them would undermine the whole
    "genuinely autonomous on a live feed" claim.
  - --live controls ONLY whether an approved order actually reaches
    submit_order. By default (dry run) the daemon can be left running for
    hours without ever touching the paper account's daily order count or
    notional cap — safe to leave on overnight, safe to run again right
    before a demo without polluting the numbers you'd show on camera.
    Pass --live only for the take you intend to record.

Unverified assumption, flagged explicitly: the exact NewsClient/NewsRequest
field names and response shape below (article.id, .created_at, .headline,
.summary) are based on alpaca-py's public examples, not tested against a
live key from this environment (no network path to Alpaca here). Run one
short poll manually first and adjust field names if your installed
alpaca-py version differs.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from cleanroom.airlock import process
from cleanroom.controller import evaluate, execute
from cleanroom.perception import PerceptionError, perceive
from cleanroom.rate_limit import MAX_INFRA_RETRIES, is_infra_error, parse_retry_delay, throttle
from cleanroom.strategy import decide

STATE_FILE_DEFAULT = Path(".cleanroom_daemon_state.json")
AUDIT_LOG_DEFAULT = Path("audit.jsonl")


class _NoOpTradingClient:
    """Default (dry-run) execution sink. get_orders always returns an empty
    history — correct for dry-run, since nothing real has ever happened on
    this path — and submit_order records the attempt locally without ever
    calling the real broker. Swapped for a real TradingClient only when
    --live is passed."""

    def __init__(self):
        self.submitted_orders: list = []

    def get_orders(self, request):
        return []

    def submit_order(self, order_request):
        order = SimpleNamespace(id=f"dry-run-{len(self.submitted_orders):04d}")
        self.submitted_orders.append(order_request)
        return order


def _load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"last_seen_at": None, "last_seen_ids": []}


def _save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state), encoding="utf-8")


def _append_audit(path: Path, event: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")


def _get_field(article, name: str, default=None):
    """Alpaca's news response shape varies: some SDK configurations return
    typed objects with attributes (article.headline), others return plain
    dicts (article["headline"]) — this tolerates either without the caller
    needing to know which."""
    if isinstance(article, dict):
        return article.get(name, default)
    return getattr(article, name, default)


def _fetch_new_articles(news_client, symbols: list[str], state: dict) -> list:
    """Fetches news since the last seen timestamp and filters out any
    article id already recorded at that exact timestamp boundary (two
    articles can share a created_at down to the second). Returns articles
    sorted oldest-first so state advances monotonically as they're
    processed one at a time.

    Confirmed shape (alpaca-py, non-raw_data mode): get_news() returns a
    NewsSet whose actual payload lives at response.data["news"] — a plain
    list of dicts with real datetime objects for created_at, not strings.
    """
    from alpaca.data.requests import NewsRequest

    kwargs: dict = {"symbols": ",".join(symbols), "limit": 50}
    if state.get("last_seen_at"):
        # Stored as an ISO string in state (JSON has no datetime type);
        # parse back to a real datetime before handing it to NewsRequest.
        kwargs["start"] = datetime.fromisoformat(state["last_seen_at"])

    response = news_client.get_news(NewsRequest(**kwargs))

    if hasattr(response, "data") and isinstance(response.data, dict):
        articles = response.data.get("news", [])
    elif isinstance(response, dict):
        articles = response.get("news", [])
    else:
        articles = getattr(response, "news", response)

    seen_ids = set(state.get("last_seen_ids", []))
    fresh = [a for a in articles if _get_field(a, "id") not in seen_ids]
    return sorted(fresh, key=lambda a: _get_field(a, "created_at"))


def _perceive_with_retry(raw_text: str, source_ref: str):
    """Same bounded-retry shape as bench.py's cached wrapper, minus the
    cache — a live feed sees genuinely new content each time, so there is
    nothing to cache. Raises PerceptionError (infra or real) after
    exhausting retries; caller logs and moves to the next article rather
    than crashing the daemon."""
    last_error: PerceptionError | None = None
    for attempt in range(MAX_INFRA_RETRIES + 1):
        throttle()
        try:
            return perceive(raw_text, source_ref=source_ref)
        except PerceptionError as e:
            msg = str(e)
            if is_infra_error(msg) and attempt < MAX_INFRA_RETRIES:
                time.sleep(parse_retry_delay(msg))
                last_error = e
                continue
            raise
    raise last_error  # pragma: no cover


def _process_article(article, trading_client, data_client, audit_path: Path) -> dict:
    """Runs one article through the full hardened pipeline and appends a
    structured event to the audit log. Never raises — every failure mode
    (infra error, Perception VETO, Strategy decline, Controller VETO) is
    captured as an event rather than propagated, since one bad article
    must not take the daemon down."""
    headline = _get_field(article, "headline", "") or ""
    summary = _get_field(article, "summary", "") or ""
    raw_text = f"{headline}\n\n{summary}".strip()
    source_ref = f"alpaca-news:{_get_field(article, 'id', 'unknown')}"

    event: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "news_id": _get_field(article, "id"),
        "news_created_at": _get_field(article, "created_at"),
        "headline": headline,
    }

    try:
        perception_output = _perceive_with_retry(raw_text, source_ref)
    except PerceptionError as e:
        event["outcome"] = "INFRA_ERROR" if is_infra_error(str(e)) else "PERCEPTION_VETO"
        event["detail"] = str(e)
        _append_audit(audit_path, event)
        return event

    airlocked = process(perception_output)
    event["symbols_extracted"] = perception_output.symbols
    event["sentiment"] = perception_output.sentiment.value
    if airlocked.anomalies:
        event["airlock_anomalies"] = airlocked.anomalies

    try:
        intent = decide(airlocked, data_client)
    except Exception as e:
        event["outcome"] = "STRATEGY_VETO"
        event["detail"] = str(e)
        _append_audit(audit_path, event)
        return event

    verdict = evaluate(intent, trading_client, data_client)
    result_str = execute(verdict, trading_client)

    event["outcome"] = verdict.final.value
    event["reason"] = verdict.reason
    event["execution_result"] = result_str
    _append_audit(audit_path, event)
    return event


def run_daemon(
    interval_seconds: int,
    live: bool,
    symbols: list[str],
    audit_path: Path = AUDIT_LOG_DEFAULT,
    state_path: Path = STATE_FILE_DEFAULT,
    log: Callable[[str], None] = print,
) -> None:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.historical.news import NewsClient
    from alpaca.trading.client import TradingClient

    api_key = os.environ["ALPACA_API_KEY"]
    secret = os.environ["ALPACA_SECRET_KEY"]

    # Always real — read-only, no execution risk regardless of --live.
    data_client = StockHistoricalDataClient(api_key, secret)
    news_client = NewsClient(api_key, secret)

    trading_client = TradingClient(api_key, secret, paper=True) if live else _NoOpTradingClient()

    state = _load_state(state_path)
    if state.get("last_seen_at") is None:
        # First run, no prior state: start the clock at "now" rather than
        # fetching an unfiltered backlog of up to 50 historical articles —
        # otherwise the first poll would burn a large chunk of the daily
        # RPM budget on news that has nothing to do with a live demo.
        state["last_seen_at"] = datetime.now(timezone.utc).isoformat()
        _save_state(state_path, state)
        log("first run — starting from now, not backfilling historical articles")

    mode = "LIVE (orders reach the real paper broker)" if live else "DRY-RUN (no order ever reaches the broker)"
    log(f"CLEANROOM daemon starting — mode={mode}, interval={interval_seconds}s, symbols={symbols}")
    log(f"Audit log: {audit_path}  |  State file: {state_path}")

    try:
        while True:
            try:
                fresh = _fetch_new_articles(news_client, symbols, state)
            except Exception as e:
                log(f"[news fetch failed, will retry next cycle] {e}")
                fresh = []

            if not fresh:
                now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
                log(f"[{now_str} UTC] polled, no new articles since {state.get('last_seen_at')}")

            for article in fresh:
                event = _process_article(article, trading_client, data_client, audit_path)

                created_at = _get_field(article, "created_at")
                if created_at is not None:
                    state["last_seen_at"] = (
                        created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)
                    )
                ids = [i for i in state.get("last_seen_ids", []) if i == _get_field(article, "id")]
                ids.append(_get_field(article, "id"))
                state["last_seen_ids"] = list(dict.fromkeys(ids))[-50:]
                _save_state(state_path, state)

                log(f"[{event.get('news_created_at')}] {event['outcome']:<16} "
                    f"{event.get('headline', '')[:70]}")

            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        log("")
        log("daemon stopped (Ctrl+C) — audit log and state are saved through the last processed article")