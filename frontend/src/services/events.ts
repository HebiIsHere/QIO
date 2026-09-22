/**
 * SSE 事件协议客户端。
 *
 * 认证：EventSource 不能自定义 header，所以先换一张一次性、短 TTL、
 * scope=events 的 ticket（POST /api/events/ticket）；主会话令牌不进 URL。
 *
 * 重放：重连时带上最后一个已处理的 event_id（标准 Last-Event-ID），服务端只补发
 * 之后的事件；客户端另有一张有界去重表（防重复消费历史事件）。
 */
import { authHeaders, resolveBackend } from "./backend";

export interface AgentEvent {
  type: string;
  id: string;
  ts: string;
  data: Record<string, unknown>;
}

export const EVENT_TYPES = [
  "CAPABILITY",
  "FALLBACK",
  "APPROVAL_REQUIRED",
  "APPROVAL_RESULT",
  "CREDENTIAL_STATUS",
  "SUBAGENT_STATUS",
  "TOOL_CREATE_STATUS",
  "KNOWLEDGE_CANDIDATE",
  "USAGE",
  "TURN_START",
  "TURN_END",
  "TURN_QUEUE",
  "TOOL_START",
  "TOOL_END",
  // 模型自主决定的过程说明（announce / progress / warning / result）
  "NARRATIVE",
  "ASSISTANT",
  "ANCHOR",
  "TOPIC_SWITCH_SUGGESTED",
  "WARNING",
  "ERROR",
  // 服务端告知「这条事件流可能不完整」：客户端据此重新拉取权威快照
  "RESYNC",
] as const;

export type EventType = (typeof EVENT_TYPES)[number];

/** 去重表长度：只保留最近的事件 id（够覆盖任何合理的重连窗口）。 */
export const DEDUP_LIMIT = 1000;

export interface EventStreamHandle {
  onopen: (() => void) | null;
  onerror: ((detail?: unknown) => void) | null;
  close(): void;
}

async function fetchEventsTicket(base: string, token: string): Promise<string> {
  const resp = await fetch(`${base}/api/events/ticket`, {
    method: "POST",
    headers: authHeaders(token),
  });
  if (!resp.ok) throw new Error(`events ticket -> ${resp.status}`);
  const data = (await resp.json()) as { ticket?: string };
  return String(data.ticket ?? "");
}

export function connectEvents(onEvent: (event: AgentEvent) => void): EventStreamHandle {
  const seen = new Set<string>();
  const order: string[] = [];
  let lastEventId: string | null = null;
  let source: EventSource | null = null;
  let closed = false;
  let attempt = 0;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  const handle: EventStreamHandle = {
    onopen: null,
    onerror: null,
    close() {
      closed = true;
      if (retryTimer) clearTimeout(retryTimer);
      retryTimer = null;
      source?.close();
      source = null;
    },
  };

  function remember(id: string) {
    if (!id) return;
    seen.add(id);
    order.push(id);
    while (order.length > DEDUP_LIMIT) {
      const old = order.shift();
      if (old) seen.delete(old);
    }
  }

  function scheduleReconnect() {
    if (closed || retryTimer) return;
    source?.close();
    source = null;
    // ticket 是一次性的 → 必须自己重连（EventSource 自带的自动重连会复用已消费的 URL）
    const delay = Math.min(15000, 1000 * 2 ** attempt) + Math.floor(Math.random() * 250);
    attempt += 1;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      void open();
    }, delay);
  }

  async function open(): Promise<void> {
    if (closed) return;
    let url: string;
    try {
      const { base, token } = await resolveBackend();
      const ticket = token ? await fetchEventsTicket(base, token) : "";
      const params = new URLSearchParams();
      if (ticket) params.set("ticket", ticket);
      if (lastEventId) params.set("last_event_id", lastEventId);
      const qs = params.toString();
      url = `${base}/api/events${qs ? `?${qs}` : ""}`;
    } catch (err) {
      handle.onerror?.(err);
      scheduleReconnect();
      return;
    }
    if (closed) return;
    source = new EventSource(url);
    source.onopen = () => {
      attempt = 0;
      handle.onopen?.();
    };
    source.onerror = (err) => {
      handle.onerror?.(err);
      scheduleReconnect();
    };
    for (const type of EVENT_TYPES) {
      source.addEventListener(type, (raw) => {
        const msg = raw as MessageEvent;
        try {
          const parsed = JSON.parse(msg.data) as AgentEvent;
          const id = msg.lastEventId || parsed.id || "";
          if (id && seen.has(id)) return; // 已处理过：重连重放不重复消费
          remember(id);
          if (id) lastEventId = id;
          onEvent(parsed);
        } catch {
          // ignore malformed frames
        }
      });
    }
  }

  void open();
  return handle;
}

export async function publishTestEvent(type: EventType, data?: unknown): Promise<void> {
  const { base, token } = await resolveBackend();
  await fetch(`${base}/api/events/test?event_type=${type}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(token) },
    body: JSON.stringify(data ?? {}),
  });
}
