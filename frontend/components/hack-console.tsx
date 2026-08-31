"use client";

import { useReducer, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { AirlockColumn } from "./airlock-column";
import { postHack } from "@/lib/api";
import type { HackEvent, Mode, StageEvent } from "@/lib/types";

interface ColumnState {
  stages: StageEvent[];
  interrupted: boolean;
}

interface ConsoleState {
  hardened: ColumnState;
  unhardened: ColumnState;
}

type Action =
  | { type: "reset" }
  | { type: "stage"; event: StageEvent }
  | { type: "interrupt"; mode: Mode };

const EMPTY_COLUMN: ColumnState = { stages: [], interrupted: false };

function reducer(state: ConsoleState, action: Action): ConsoleState {
  switch (action.type) {
    case "reset":
      return { hardened: { ...EMPTY_COLUMN }, unhardened: { ...EMPTY_COLUMN } };
    case "stage":
      return {
        ...state,
        [action.event.mode]: {
          ...state[action.event.mode],
          stages: [...state[action.event.mode].stages, action.event],
        },
      };
    case "interrupt":
      return {
        ...state,
        [action.mode]: { ...state[action.mode], interrupted: true },
      };
    default:
      return state;
  }
}

export function HackConsole() {
  const [text, setText] = useState("");
  const [running, setRunning] = useState(false);
  const [state, dispatch] = useReducer(reducer, {
    hardened: { ...EMPTY_COLUMN },
    unhardened: { ...EMPTY_COLUMN },
  });
  const abortRef = useRef<AbortController | null>(null);

  async function handleSubmit() {
    const trimmed = text.trim();
    if (!trimmed || running) return;

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    dispatch({ type: "reset" });
    setRunning(true);

    try {
      await postHack(
        trimmed,
        (event: HackEvent) => {
          if (event.type === "stage") dispatch({ type: "stage", event });
        },
        controller.signal,
      );
    } catch (err) {
      if (!controller.signal.aborted) {
        dispatch({ type: "interrupt", mode: "hardened" });
        dispatch({ type: "interrupt", mode: "unhardened" });
        console.error(err);
      }
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex gap-2">
        <input
          className="flex-1 rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 font-mono text-sm text-zinc-100 placeholder:text-zinc-600 focus:border-zinc-500 focus:outline-none"
          placeholder="Try to hack the agent…"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              handleSubmit();
            }
          }}
          disabled={running}
        />
        <Button onClick={handleSubmit} disabled={running || !text.trim()}>
          {running ? "Running…" : "Submit"}
        </Button>
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <AirlockColumn
          mode="unhardened"
          stages={state.unhardened.stages}
          interrupted={state.unhardened.interrupted}
        />
        <AirlockColumn
          mode="hardened"
          stages={state.hardened.stages}
          interrupted={state.hardened.interrupted}
        />
      </div>
    </div>
  );
}
