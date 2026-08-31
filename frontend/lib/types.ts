// Types mirror the SSE contract emitted by cleanroom/server.py exactly.
// Do not add fields that aren't in the backend contract.

export type Mode = "hardened" | "unhardened";

export interface ControllerCheck {
  name: string;
  passed: boolean;
  severity: string;
  detail?: string | null;
}

export type HardenedStageName =
  | "perception_start"
  | "perception_done"
  | "perception_veto"
  | "infra_error"
  | "airlock_clean"
  | "airlock_flagged"
  | "strategy_veto"
  | "controller_verdict"
  | "execution_result";

export type UnhardenedStageName =
  | "naive_start"
  | "naive_result"
  | "infra_error"
  | "naive_error";

export interface StageEvent {
  type: "stage";
  trace_id: string;
  mode: Mode;
  stage: HardenedStageName | UnhardenedStageName;
  // Stage-specific extras — kept loose since which keys are present depends
  // on `stage`; components narrow by checking the relevant field.
  symbols_extracted?: string[];
  sentiment?: string;
  detail?: string;
  airlock_anomalies?: string[];
  final?: "PASS" | "VETO";
  reason?: string;
  checks?: ControllerCheck[];
  result?: string;
  captured?: boolean;
}

export interface CompleteEvent {
  type: "complete";
  trace_id: string;
  hardened: Record<string, unknown>;
  unhardened: Record<string, unknown>;
}

export type HackEvent = StageEvent | CompleteEvent;

// ── GET /api/events contract ────────────────────────────────────────────
export type Source = "daemon" | "judge";

export type Outcome =
  | "PASS"
  | "VETO"
  | "PERCEPTION_VETO"
  | "STRATEGY_VETO"
  | "INFRA_ERROR"
  | "CAPTURED"
  | "NO_ORDER"
  | "NAIVE_ERROR";

export interface AuditEvent {
  type: "event";
  timestamp: string;
  source: Source;
  mode: Mode;
  trace_id: string | null;
  headline: string;
  symbols_extracted?: string[];
  sentiment?: string;
  outcome: Outcome;
  reason?: string;
  execution_result?: string;
  detail?: string;
  airlock_anomalies?: string[];
  news_id?: number | string;
  news_created_at?: string;
}
