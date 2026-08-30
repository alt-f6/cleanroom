"""
Plane 2 — AIRLOCK. Deterministic gateway, zero LLM calls.

Takes a schema-validated PerceptionOutput (already produced and validated
by Plane 1) and produces an AirlockedPerception: the same data, tagged with
provenance and an identity, plus non-fatal anomaly flags for anything that
looks suspicious but is not this layer's job to block.

AIRLOCK never makes a PASS/VETO decision. That is the Execution Controller's
job (Plane 3), where market data is available to evaluate the anomalies
against reality. AIRLOCK's only responsibilities: normalize, tag, quarantine
(flag) — never decide.
"""
from __future__ import annotations

import re

from cleanroom.schemas import (
    AirlockedPerception, 
    ExtractedClaim,
    PerceptionOutput,
    Provenance
)

# Keyword markers associated with known injection patterns from the attack
# corpus (direct instruction, fake authority). This is NOT a detector we
# rely on for security — it's a visibility flag for the demo and for
# anyone reading the log. The actual security guarantee lives in the
# tool-boundary (Perception has no tools) and in the Controller's
# invariants, not in this keyword list. Do not extend this into a
# detection arms race; that is explicitly out of scope (see spec §1).
_INJECTION_MARKERS = (
    "system:",
    "system override",
    "ignore previous instructions",
    "ignore all previous instructions",
    "disregard prior instructions",
    "compliance requires",
    "compliance-mandated",
    "you must buy",
    "you must sell",
    "as an ai",
)

# A pct_move claim beyond this magnitude is treated as an outlier worth
# flagging (e.g. "stock will rise 300%") — not blocked here, just surfaced.
_PCT_MOVE_ANOMALY_THRESHOLD = 100.0

def _flag_claim_anomalies(claims: list[ExtractedClaim]) -> list[str]:
    flags: list[str] = []
    for c in claims:
        if c.claim_type.value == "pct_move" and c.numeric_value is not None:
            if abs(c.numeric_value) > _PCT_MOVE_ANOMALY_THRESHOLD:
                flags.append(
                    f"outlier pct_move claim: {c.numeric_value}% "
                    f"(exceeds ±{_PCT_MOVE_ANOMALY_THRESHOLD}%) — {c.text!r}"
                )
    return flags

def _flag_symbol_entity_mismatch(payload: PerceptionOutput) -> list[str]:
    flags: list[str] = []
    if not payload.symbols and payload.entities:
        flags.append(
            "no symbols extracted despite non-empty entities - "
            "content may be off-topic or non-actionable"
        )
    return flags

def _flag_injection_markers(payload: PerceptionOutput) -> list[str]:
    """
    Scan the TEXT FIELDS Perception already extracted (claims, entities,
    source_ref) for known injection-pattern keywords. We deliberately do
    NOT re-read raw untrusted text here — AIRLOCK only ever sees what
    Plane 1 already turned into structure. This keeps the trust boundary
    intact: AIRLOCK's input is PerceptionOutput, never the original file.
    """
    flags: list[str] = []
    haystacks = [c.text for c in payload.extracted_claims] + list(payload.entities)
    lowered = " ".join(haystacks).lower()
    for marker in _INJECTION_MARKERS:
        if marker in lowered:
            flags.append(f"injection-pattern marker present in extracted text: {marker!r}")
    return flags


def process(payload: PerceptionOutput) -> AirlockedPerception:
    """
    The single AIRLOCK entry point. Pure function: same input always
    produces the same anomaly flags (aside from the fresh id/timestamp).
    """
    anomalies: list[str] = []
    anomalies.extend(_flag_claim_anomalies(payload.extracted_claims))
    anomalies.extend(_flag_symbol_entity_mismatch(payload))
    anomalies.extend(_flag_injection_markers(payload))

    return AirlockedPerception(
        payload=payload,
        provenance=Provenance.UNTRUSTED_TEXT,
        anomalies=anomalies,
    )