"use client";

import { useEffect, useReducer, useRef } from "react";
import { subscribeEvents, type ConnectionStatus } from "./api";
import type { AuditEvent, Mode } from "./types";

// A row is either a standalone daemon event (trace_id === null) or a
// hardened/unhardened pair sharing one trace_id from a judge attack. Pairs
// are updated in place as their second arm arrives, not duplicated.
export interface EventRow {
  key: string;
  trace_id: string | null;
  single?: AuditEvent;
  hardened?: AuditEvent;
  unhardened?: AuditEvent;
}

interface AuditState {
  rows: EventRow[];
  totalEvents: number;
  totalAttacks: number;
}

type Action = { type: "event"; event: AuditEvent };

const INITIAL_STATE: AuditState = { rows: [], totalEvents: 0, totalAttacks: 0 };

function reducer(state: AuditState, action: Action): AuditState {
  const ev = action.event;

  if (!ev.trace_id) {
    const row: EventRow = { key: `${ev.timestamp}-${state.totalEvents}`, trace_id: null, single: ev };
    return {
      rows: [row, ...state.rows],
      totalEvents: state.totalEvents + 1,
      totalAttacks: state.totalAttacks,
    };
  }

  const idx = state.rows.findIndex((r) => r.trace_id === ev.trace_id);
  const modeKey: Mode = ev.mode;

  if (idx === -1) {
    const row: EventRow = { key: ev.trace_id, trace_id: ev.trace_id, [modeKey]: ev };
    return {
      rows: [row, ...state.rows],
      totalEvents: state.totalEvents + 1,
      totalAttacks: state.totalAttacks + 1,
    };
  }

  const rows = [...state.rows];
  rows[idx] = { ...rows[idx], [modeKey]: ev };
  return { rows, totalEvents: state.totalEvents + 1, totalAttacks: state.totalAttacks };
}

export function useAuditEvents() {
  const [state, dispatch] = useReducer(reducer, INITIAL_STATE);
  const statusRef = useRef<ConnectionStatus>("connecting");
  const [, forceRender] = useReducer((c: number) => c + 1, 0);

  useEffect(() => {
    const controller = new AbortController();

    subscribeEvents(
      (event) => dispatch({ type: "event", event }),
      (status) => {
        statusRef.current = status;
        forceRender();
      },
      controller.signal,
    );

    return () => controller.abort();
  }, []);

  return { rows: state.rows, totalEvents: state.totalEvents, totalAttacks: state.totalAttacks, status: statusRef.current };
}
