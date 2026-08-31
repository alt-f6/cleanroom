import { parseSSEStream } from "./sse";
import type { AuditEvent, HackEvent } from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 15000;

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

export type ConnectionStatus = "connecting" | "live" | "reconnecting";

export async function postHack(
  text: string,
  onEvent: (event: HackEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API_BASE}/api/hack`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
    signal,
  });

  if (!response.ok) {
    throw new Error(`POST /api/hack failed: ${response.status}`);
  }

  await parseSSEStream(response, (data) => onEvent(data as HackEvent), signal);
}

// GET /api/events — long-lived SSE tail with reconnect. Runs until `signal`
// aborts; a dropped connection (network blip, backend restart) triggers an
// exponential-backoff retry rather than giving up.
export async function subscribeEvents(
  onEvent: (event: AuditEvent) => void,
  onStatusChange: (status: ConnectionStatus) => void,
  signal: AbortSignal,
): Promise<void> {
  let attempt = 0;

  while (!signal.aborted) {
    onStatusChange(attempt === 0 ? "connecting" : "reconnecting");
    try {
      const response = await fetch(`${API_BASE}/api/events`, { signal });
      if (!response.ok) throw new Error(`GET /api/events failed: ${response.status}`);

      attempt = 0;
      onStatusChange("live");
      await parseSSEStream(response, (data) => onEvent(data as AuditEvent), signal);

      if (signal.aborted) return;
      // Stream ended without the signal aborting — backend closed the
      // connection. Treat like a drop and reconnect.
      throw new Error("event stream ended unexpectedly");
    } catch (err) {
      if (signal.aborted) return;
      attempt += 1;
      const delay = Math.min(RECONNECT_BASE_MS * 2 ** (attempt - 1), RECONNECT_MAX_MS);
      await sleep(delay, signal);
    }
  }
}
