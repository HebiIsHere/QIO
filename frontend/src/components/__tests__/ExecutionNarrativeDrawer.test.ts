/**
 * 执行叙事的抽屉契约（spec 2026-09-22 §8）：
 * 默认收起、只有用户点击才展开；折叠头如实显示运行中 / 失败。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import NarrativeStage from "../NarrativeStage.vue";
import MessageStream from "../MessageStream.vue";
import { useSessionStore, type StreamMessage } from "../../stores/session";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({
      topic_id: "t1",
      topic_name: "话题",
      anchor_fragment: null,
      messages: [],
    })),
    sendTurn: vi.fn(async () => ({ ok: true })),
  },
}));

function msg(partial: Partial<StreamMessage> & { id: string; role: StreamMessage["role"] }): StreamMessage {
  return {
    content: "",
    contentType: "text",
    createdAt: "2026-09-22T09:41:00+00:00",
    ...partial,
  } as StreamMessage;
}

function mountStage(overrides: {
  narrative?: Partial<StreamMessage>;
  calls?: StreamMessage[];
} = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const narrative = msg({
    id: "n1",
    role: "narrative",
    content: "我先确认审批请求从后端到前端的完整路径。",
    narrativeKind: "announce",
    ...overrides.narrative,
  });
  return mount(NarrativeStage, {
    props: { narrative, calls: overrides.calls ?? [] },
    global: { plugins: [pinia] },
  });
}

beforeEach(() => {
  localStorage.clear();
});

describe("叙事抽屉", () => {
  it("默认收起，点击后才展开", async () => {
    const w = mountStage({
      calls: [msg({ id: "t1", role: "tool", callId: "c1", toolName: "echo", toolStatus: "success" })],
    });
    const head = w.find(".nhead");
    expect(head.attributes("aria-expanded")).toBe("false");
    expect(w.find(".qio-narrative").attributes("data-open")).toBe("false");
    await head.trigger("click");
    expect(head.attributes("aria-expanded")).toBe("true");
    expect(w.find(".qio-narrative").attributes("data-open")).toBe("true");
    await head.trigger("click");
    expect(w.find(".qio-narrative").attributes("data-open")).toBe("false");
  });

  it("折叠头说出里面有几张卡：2 次调用 · 总耗时", () => {
    const w = mountStage({
      calls: [
        msg({ id: "t1", role: "tool", callId: "c1", toolStatus: "success", toolDurationMs: 400 }),
        msg({ id: "t2", role: "tool", callId: "c2", toolStatus: "success", toolDurationMs: 200 }),
      ],
    });
    expect(w.find(".nmeta").text()).toBe("2 次调用 · 600ms");
  });

  it("运行中的那一步不自动展开，但折叠头必须显示 1 运行中", () => {
    const w = mountStage({
      calls: [msg({ id: "t1", role: "tool", callId: "c1", toolStatus: "running" })],
    });
    expect(w.find(".qio-narrative").attributes("data-open")).toBe("false");
    const meta = w.find(".nmeta");
    expect(meta.text()).toBe("1 运行中");
    expect(meta.classes()).toContain("run");
  });

  it("失败不会被收纳藏起来：折叠头显示 1 失败", () => {
    const w = mountStage({
      calls: [
        msg({ id: "t1", role: "tool", callId: "c1", toolStatus: "success" }),
        msg({ id: "t2", role: "tool", callId: "c2", toolStatus: "failed", toolError: "未找到 old" }),
      ],
    });
    const meta = w.find(".nmeta");
    expect(meta.text()).toBe("1 失败");
    expect(meta.classes()).toContain("bad");
  });

  it("历史抽屉显示系统生成的调用摘要，没有摘要时不显示箭头", () => {
    const withCalls = mountStage({
      narrative: {
        narrativeCalls: [
          { callId: "c1", tool: "fs_read", title: "读取 approval.py", status: "success", durationMs: 210 },
          { callId: "c2", tool: "fs_write", title: "写入 narrative.py", status: "failed", error: "未找到待替换内容 old" },
        ],
      },
    });
    expect(withCalls.find(".nchev").exists()).toBe(true);
    expect(withCalls.find(".calls-note").text()).toContain("系统生成");
    expect(withCalls.findAll(".callrow")).toHaveLength(2);
    expect(withCalls.find(".nmeta").text()).toBe("1 失败");

    const withoutCalls = mountStage();
    expect(withoutCalls.find(".nchev").exists()).toBe(false);
    expect(withoutCalls.find(".nhead").classes()).toContain("static");
  });
});

describe("消息流不再有机械工具提示", () => {
  it("工具运行时不显示「正在使用工具」（过程交给叙事与工具卡）", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    const w = mount(MessageStream, { global: { plugins: [pinia] } });
    session.turnStarted();
    session.applyNarrative({
      narrative_id: "msg_1",
      turn_id: "turn_1",
      kind: "announce",
      text: "我先确认审批链路。",
    });
    session.startTool("c1", "echo", { title: "回声" }, "turn_1", {});
    session.activity = "tool";
    await w.vm.$nextTick();
    // 整体状态条在"正在使用工具"时不再出现（旧的机械提示已移除）
    expect(w.find(".typing").exists()).toBe(false);
    // 其它系统状态照旧
    session.activity = "approval";
    await w.vm.$nextTick();
    expect(w.find(".typing").text()).toContain("等待你确认");
    // 叙事已经进了消息流（store 层事实；渲染由 NarrativeStage 的用例覆盖）
    expect(session.messages.some((m) => m.role === "narrative" && m.content === "我先确认审批链路。")).toBe(true);
    w.unmount();
  });
});
