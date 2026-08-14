import { describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({
      topic_id: "",
      topic_name: null,
      anchor_fragment: null,
      messages: [],
    })),
    sendTurn: vi.fn(async () => ({})),
  },
}));

describe("events store 路由", () => {
  it("ASSISTANT 事件 → 追加 interim 助手消息", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    events.route({
      type: "ASSISTANT",
      id: "e1",
      ts: "2026-08-10T00:00:00Z",
      data: { content: "我先查一下仓库", interim: true },
    });
    const last = session.messages[session.messages.length - 1];
    expect(last?.role).toBe("assistant");
    expect(last?.content).toBe("我先查一下仓库");
    expect(last?.interim).toBe(true);
  });

  it("ASSISTANT 空内容不追加消息", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    events.route({
      type: "ASSISTANT",
      id: "e2",
      ts: "2026-08-10T00:00:00Z",
      data: { content: "  " },
    });
    expect(session.messages.length).toBe(0);
  });

  it("TOOL_END 携带 presentation → 工具消息保存呈现", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    events.route({
      type: "TOOL_END",
      id: "e3",
      ts: "2026-08-10T00:00:00Z",
      data: {
        tool: "memory_search",
        ok: true,
        error: null,
        content_preview: "raw",
        presentation: { title: "检索记忆", status: "ok", summary: "命中 3 条" },
      },
    });
    const last = session.messages[session.messages.length - 1];
    expect(last?.role).toBe("tool");
    expect(last?.presentation).toEqual({ title: "检索记忆", status: "ok", summary: "命中 3 条" });
  });

  it("TOOL_END 无 presentation → 工具消息 presentation 为 null", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    events.route({
      type: "TOOL_END",
      id: "e4",
      ts: "2026-08-10T00:00:00Z",
      data: { tool: "echo", ok: true, error: null, content_preview: "hi" },
    });
    const last = session.messages[session.messages.length - 1];
    expect(last?.presentation).toBeNull();
  });
});
