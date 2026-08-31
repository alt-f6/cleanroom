import type { Outcome } from "./types";

export type OutcomeTone = "pass" | "captured" | "veto-hard" | "veto-soft" | "neutral";

const TONE_BY_OUTCOME: Record<Outcome, OutcomeTone> = {
  PASS: "pass",
  NO_ORDER: "pass",
  CAPTURED: "captured",
  VETO: "veto-hard",
  PERCEPTION_VETO: "veto-soft",
  STRATEGY_VETO: "veto-soft",
  INFRA_ERROR: "neutral",
  NAIVE_ERROR: "neutral",
};

export function outcomeTone(outcome: Outcome): OutcomeTone {
  return TONE_BY_OUTCOME[outcome] ?? "neutral";
}

export const TONE_BADGE_CLASSES: Record<OutcomeTone, string> = {
  pass: "border-emerald-500/60 bg-emerald-950/40 text-emerald-400",
  captured:
    "animate-pulse border-red-500 bg-red-600 text-white shadow-[0_0_12px_rgba(239,68,68,0.6)]",
  "veto-hard": "border-red-400/60 bg-red-950/40 text-red-400",
  "veto-soft": "border-amber-500/60 bg-amber-950/40 text-amber-400",
  neutral: "border-zinc-600/60 bg-zinc-900/60 text-zinc-400",
};
