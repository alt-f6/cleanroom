// Generic SSE frame parser over a fetch() Response body stream.
// EventSource can't do POST, and comment-only keep-alive lines (e.g.
// ": heartbeat") aren't JSON — both are handled here so this is reusable
// for GET /api/events later, not just POST /api/hack.

export async function parseSSEStream(
  response: Response,
  onEvent: (data: unknown) => void,
  signal?: AbortSignal,
): Promise<void> {
  if (!response.body) return;

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      if (signal?.aborted) break;
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";

      for (const frame of frames) {
        for (const line of frame.split("\n")) {
          if (!line.startsWith("data:")) continue; // skips ": heartbeat" etc.
          const payload = line.slice(5).trim();
          if (!payload) continue;
          try {
            onEvent(JSON.parse(payload));
          } catch {
            // malformed frame — skip rather than crash the stream
          }
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
