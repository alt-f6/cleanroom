"""
cleanroom/server.py — Phase 4 UI backend.

FastAPI service exposing the hardened/unhardened pipeline to the Next.js
Mission Control UI over two endpoints:

  GET  /api/events   SSE stream of every event in audit.jsonl (daemon +
                      judge), normalized via cleanroom.audit. Backfills
                      existing history on connect, then polls and tails
                      new lines.

  POST /api/hack      Judge-submitted text. Runs BOTH the hardened pipeline
                      (Perception -> AIRLOCK -> Strategy -> Controller) and
                      the naive unhardened baseline against the same input,
                      streaming stage-by-stage progress over SSE, then
                      appends both final results to audit.jsonl (tagged
                      source="judge", shared trace_id) so /api/events
                      broadcasts them to every other connected client too.

Design choices made wiring this up (see chat for the full reasoning):

  - daemon.py, bench.py, controller.py, perception.py, strategy.py,
    unhardened.py are UNMODIFIED. This module only imports from them.

  - The unhardened arm reuses bench.py's `_FakeTradingClient` (in-memory,
    always-empty order history, never touches the real broker) — a
    "CAPTURED" result here is provably a sandboxed hijack, never a real
    paper-account submission. This was an explicit decision: overpowered
    attacks (oversized notional, garbage tickers) would otherwise 422 out
    of a real broker's margin checks and corrupt the live account's order
    history for subsequent judge attempts.

  - The hardened arm defaults to a LOCAL dry-run trading client
    (_DryRunTradingClient below) unless CLEANROOM_LIVE=1 is set, mirroring
    daemon.py's own --live flag semantics. This duplicates ~10 lines of
    daemon.py's _NoOpTradingClient rather than importing that private name
    across modules — keeps this file's behavior legible without coupling
    to daemon.py's internals staying stable.

  - Market data (price lookups) for the hardened arm is ALWAYS real Alpaca
    data; live vs. dry-run affects order submission only — same rule
    daemon.py already follows for its own --live flag.

  - Both arms call the SAME process-global throttle() from
    cleanroom.rate_limit, exactly like bench.py and daemon.py, so this
    server paces itself against the same per-project Gemini quota. It does
    NOT coordinate with a separately-running `cleanroom daemon` process —
    throttle() is per-process state, not cross-process. This is why the
    daemon must be stopped before a live judge demo.

  - On a Gemini infra error (429/5xx), this server does NOT apply
    bench.py/daemon.py's blocking retry-with-backoff (up to ~65s) — fine
    for an unattended batch/poll job, unacceptable to make a judge sit
    through live on stage. It streams an infra_error stage immediately and
    stops that arm; the judge can just resubmit.

Run with: uvicorn cleanroom.server:app --reload --port 8000
Requires fastapi + uvicorn — see requirements-server.txt (not merged into
requirements.txt, to keep the Phase 1-3 dependency set untouched).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from cleanroom import audit
from cleanroom.airlock import process as airlock_process
from cleanroom.bench import _FakeTradingClient  # explicit, authorized reuse
from cleanroom.controller import evaluate, execute
from cleanroom.perception import PerceptionError, perceive
from cleanroom.rate_limit import is_infra_error, throttle
from cleanroom.strategy import StrategyError, decide
from cleanroom.unhardened import run_naive

load_dotenv()

AUDIT_LOG_PATH = Path(os.environ.get("CLEANROOM_AUDIT_LOG", "audit.jsonl"))
UI_ORIGIN = os.environ.get("CLEANROOM_UI_ORIGIN", "http://localhost:3000")
LIVE = os.environ.get("CLEANROOM_LIVE") == "1"

POLL_INTERVAL_SECONDS = 1.0
HEARTBEAT_INTERVAL_SECONDS = 15.0
MAX_HACK_TEXT_LENGTH = 5000
HEADLINE_PREVIEW_LENGTH = 100

app = FastAPI(title="CLEANROOM Mission Control API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[UI_ORIGIN],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


# ─────────────────────────────────────────────────────────────────────────────
# Dry-run trading client for the HARDENED arm's judge-triggered demo runs.
# See module docstring for why this duplicates daemon.py's
# _NoOpTradingClient rather than importing it.
# ─────────────────────────────────────────────────────────────────────────────
class _DryRunTradingClient:
    def __init__(self) -> None:
        self.submitted_orders: list = []

    def get_orders(self, request):
        return []

    def submit_order(self, order_request):
        order = SimpleNamespace(id=f"judge-dry-run-{len(self.submitted_orders):04d}")
        self.submitted_orders.append(order_request)
        return order


def _hardened_clients() -> tuple[Any, Any]:
    from alpaca.data.historical import StockHistoricalDataClient

    api_key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret:
        raise HTTPException(
            status_code=500,
            detail="ALPACA_API_KEY / ALPACA_SECRET_KEY not set in environment",
        )

    # Market data is always real, regardless of LIVE — same rule daemon.py
    # follows: read-only, no execution risk either way.
    data_client = StockHistoricalDataClient(api_key, secret)

    if LIVE:
        from alpaca.trading.client import TradingClient

        trading_client = TradingClient(api_key, secret, paper=True)
    else:
        trading_client = _DryRunTradingClient()

    return trading_client, data_client


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, default=str)}\n\n"


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/events
# ─────────────────────────────────────────────────────────────────────────────
async def _events_stream() -> AsyncIterator[str]:
    # All audit-file reads run in a worker thread: they are synchronous
    # disk I/O, and with many SSE clients connected each blocking read on
    # the event loop would stall every other stream's heartbeat.
    backfill = await asyncio.to_thread(audit.read_all, AUDIT_LOG_PATH)
    for event in backfill:
        yield _sse({"type": "event", **event})

    # Offset tracked in RAW line units (audit.raw_line_count), matching
    # what tail_new() indexes into — NOT len(backfill), which counts only
    # successfully-parsed events. A blank or malformed line would desync
    # those two counts and cause tail_new() to replay or skip a line.
    line_count = await asyncio.to_thread(audit.raw_line_count, AUDIT_LOG_PATH)
    elapsed_since_heartbeat = 0.0

    while True:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        elapsed_since_heartbeat += POLL_INTERVAL_SECONDS

        new_events, line_count = await asyncio.to_thread(
            audit.tail_new, AUDIT_LOG_PATH, line_count
        )
        for event in new_events:
            yield _sse({"type": "event", **event})
            elapsed_since_heartbeat = 0.0

        if elapsed_since_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
            yield ": heartbeat\n\n"
            elapsed_since_heartbeat = 0.0


@app.get("/api/events")
async def get_events() -> StreamingResponse:
    return StreamingResponse(_events_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/hack
# ─────────────────────────────────────────────────────────────────────────────
class HackRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_HACK_TEXT_LENGTH)


async def _hack_stream(raw_text: str) -> AsyncIterator[str]:
    trace_id = audit.new_trace_id()
    source_ref = f"judge-input:{trace_id}"
    headline = raw_text.strip()[:HEADLINE_PREVIEW_LENGTH]

    # ---------------- HARDENED ARM ----------------
    # Every synchronous pipeline stage below (throttle's rate-limit sleep,
    # perceive/run_naive Gemini calls, decide/evaluate/execute Alpaca calls,
    # audit-file writes) runs via asyncio.to_thread — nothing blocking may
    # touch the event loop thread, or every other SSE client's polling and
    # heartbeats would freeze for the duration of each network call.
    yield _sse({"type": "stage", "trace_id": trace_id, "mode": "hardened", "stage": "perception_start"})

    await asyncio.to_thread(throttle)
    hardened_summary: dict[str, Any] = {"headline": headline}
    try:
        perception_output = await asyncio.to_thread(perceive, raw_text, source_ref=source_ref)
    except PerceptionError as e:
        msg = str(e)
        infra = is_infra_error(msg)
        hardened_summary.update({"outcome": "INFRA_ERROR" if infra else "PERCEPTION_VETO", "detail": msg})
        yield _sse({
            "type": "stage", "trace_id": trace_id, "mode": "hardened",
            "stage": "infra_error" if infra else "perception_veto", "detail": msg,
        })
    else:
        hardened_summary["symbols_extracted"] = perception_output.symbols
        hardened_summary["sentiment"] = perception_output.sentiment.value
        yield _sse({
            "type": "stage", "trace_id": trace_id, "mode": "hardened", "stage": "perception_done",
            "symbols_extracted": perception_output.symbols, "sentiment": perception_output.sentiment.value,
        })

        airlocked = await asyncio.to_thread(airlock_process, perception_output)
        if airlocked.anomalies:
            hardened_summary["airlock_anomalies"] = airlocked.anomalies
        yield _sse({
            "type": "stage", "trace_id": trace_id, "mode": "hardened",
            "stage": "airlock_flagged" if airlocked.anomalies else "airlock_clean",
            "airlock_anomalies": airlocked.anomalies,
        })

        trading_client, data_client = _hardened_clients()

        try:
            intent = await asyncio.to_thread(decide, airlocked, data_client)
        except StrategyError as e:
            hardened_summary.update({"outcome": "STRATEGY_VETO", "detail": str(e)})
            yield _sse({
                "type": "stage", "trace_id": trace_id, "mode": "hardened",
                "stage": "strategy_veto", "detail": str(e),
            })
        else:
            verdict = await asyncio.to_thread(
                evaluate, intent, trading_client, data_client, airlocked=airlocked
            )
            result_str = await asyncio.to_thread(execute, verdict, trading_client)

            hardened_summary.update({
                "outcome": verdict.final.value,
                "reason": verdict.reason,
                "execution_result": result_str,
            })
            yield _sse({
                "type": "stage", "trace_id": trace_id, "mode": "hardened", "stage": "controller_verdict",
                "final": verdict.final.value, "reason": verdict.reason,
                "checks": [c.model_dump(mode="json") for c in verdict.checks],
            })
            yield _sse({
                "type": "stage", "trace_id": trace_id, "mode": "hardened",
                "stage": "execution_result", "result": result_str,
            })

    # ---------------- UNHARDENED ARM ----------------
    yield _sse({"type": "stage", "trace_id": trace_id, "mode": "unhardened", "stage": "naive_start"})

    await asyncio.to_thread(throttle)
    unhardened_summary: dict[str, Any] = {"headline": headline}
    fake_client = _FakeTradingClient()
    try:
        naive_result = await asyncio.to_thread(run_naive, raw_text, fake_client)
    except Exception as e:
        msg = str(e)
        infra = is_infra_error(msg)
        unhardened_summary.update({"outcome": "INFRA_ERROR" if infra else "NAIVE_ERROR", "detail": msg})
        yield _sse({
            "type": "stage", "trace_id": trace_id, "mode": "unhardened",
            "stage": "infra_error" if infra else "naive_error", "detail": msg,
        })
    else:
        # Structured tool-dispatch fact from NaiveRunResult — never a
        # substring match on the prose detail.
        captured = naive_result.captured
        unhardened_summary.update({
            "outcome": "CAPTURED" if captured else "NO_ORDER",
            "detail": naive_result.detail,
        })
        if naive_result.orders:
            unhardened_summary["orders"] = naive_result.orders
        yield _sse({
            "type": "stage", "trace_id": trace_id, "mode": "unhardened", "stage": "naive_result",
            "captured": captured, "result": naive_result.detail,
        })

    # ---------------- PERSIST + FINAL ----------------
    await asyncio.to_thread(
        audit.record_judge_attack,
        hardened_summary, unhardened_summary, trace_id=trace_id, audit_path=AUDIT_LOG_PATH,
    )

    yield _sse({
        "type": "complete", "trace_id": trace_id,
        "hardened": hardened_summary, "unhardened": unhardened_summary,
    })


@app.post("/api/hack")
async def post_hack(req: HackRequest) -> StreamingResponse:
    return StreamingResponse(_hack_stream(req.text), media_type="text/event-stream", headers=_SSE_HEADERS)