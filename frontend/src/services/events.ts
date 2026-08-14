/**
 * SSE event protocol client.
 * Wire format: event: <TYPE>\ndata: {json}\n\n
 */
import { BACKEND_BASE } from "./events_const";

export interface AgentEvent {
  type: string;
  id: string;
  ts: string;
  data: Record<string, unknown>;
}

export const EVENT_TYPES = [
  "CAPABILITY",
  "FALLBACK",
  "MEMORY_INJECT",
  "APPROVAL_REQUIRED",
  "APPROVAL_RESULT",
  "CREDENTIAL_STATUS",
  "SUBAGENT_STATUS",
  "USAGE",
  "TURN_START",
  "TURN_END",
  "TOOL_START",
  "TOOL_END",
  "ASSISTANT",
  "ANCHOR",
  "WARNING",
  "ERROR",
] as const;

export type EventType = (typeof EVENT_TYPES)[number];

export function connectEvents(onEvent: (event: AgentEvent) => void): EventSource {
  const es = new EventSource(`${BACKEND_BASE}/api/events`);
  for (const type of EVENT_TYPES) {
    es.addEventListener(type, (raw) => {
      const msg = raw as MessageEvent;
      try {
        onEvent(JSON.parse(msg.data) as AgentEvent);
      } catch {
        // ignore malformed frames
      }
    });
  }
  return es;
}

export async function publishTestEvent(type: EventType, data?: unknown): Promise<void> {
  await fetch(`${BACKEND_BASE}/api/events/test?event_type=${type}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data ?? {}),
  });
}
