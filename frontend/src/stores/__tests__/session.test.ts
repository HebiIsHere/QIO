import { describe, expect, it, vi, afterEach } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../session";
import { useEventStore } from "../events";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({
      topic_id: "t1",
      topic_name: "话题",
      anchor_fragment: null,
      messages: [
        { id: "m1", role: "user", content: "历史问题", content_type: "text", created_at: "2026-09-13T00:00:00Z" },
      ],
    })),
    sendTurn: vi.fn(async () => ({})),
  },
}));

afterEach(() => {
  vi.useRealTimers();
  vi.mocked(api.getSessionContext).mockClear();
  vi.mocked(api.sendTurn).mockClear();
});

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { session: useSessionStore(), events: useEventStore() };
}

describe("新一轮的一轮阶段（任务03 E：不假装知道后台在做什么）", () => {
  it("TURN_START → waiting；出现增量内容 → generating；TURN_END → idle", () => {
    const { session, events } = setup();
    expect(session.turnPhase).toBe("idle");
    events.route({ type: "TURN_START", id: "e1", ts: "", data: { turn_id: "turn_1" } });
    expect(session.turnPhase).toBe("waiting");
    events.route({ type: "ASSISTANT", id: "e2", ts: "", data: { content: "开始回答" } });
    expect(session.turnPhase).toBe("generating");
    events.route({ type: "TURN_END", id: "e3", ts: "", data: { turn_id: "turn_1" } });
    expect(session.turnPhase).toBe("idle");
  });
});

describe("新消息入场标记（任务03 C：最多一次很短的入场）", () => {
  it("本次会话新产生的消息带 fresh，短暂后自动清除", () => {
    vi.useFakeTimers();
    const { session } = setup();
    session.pushUser("新消息");
    const last = session.messages[session.messages.length - 1];
    expect(session.freshIds).toContain(last.id);
    vi.advanceTimersByTime(700);
    expect(session.freshIds).not.toContain(last.id);
  });

  it("历史消息不带 fresh（返回页面不重播入场动画）", async () => {
    const { session } = setup();
    await session.loadHistory();
    expect(session.messages.length).toBe(1);
    expect(session.freshIds).toEqual([]);
  });
});

describe("逐字节奏跟随真实到达间隔（任务03 C）", () => {
  it("相邻增量间隔被记录为 paceMs（夹在 40–400ms）", () => {
    vi.useFakeTimers();
    const { session } = setup();
    session.turnRunning = true;
    session.pushAssistant("第一段", true, true);
    const msg = session.messages[session.messages.length - 1];
    expect(msg.paceMs).toBeUndefined(); // 第一次到达还没有间隔可算

    vi.advanceTimersByTime(200);
    session.pushAssistant("第一段第二段", true, true);
    expect(msg.paceMs).toBe(200);

    // 间隔过短/过长都会被夹住，避免出现「瞬显」或「一个字停一秒」
    vi.advanceTimersByTime(5);
    session.pushAssistant("第一段第二段第三段", true, true);
    expect(msg.paceMs).toBe(40);

    vi.advanceTimersByTime(5000);
    session.pushAssistant("第一段第二段第三段第四段", true, true);
    expect(msg.paceMs).toBe(400);
  });
});

describe("起点切换后的可见消息同步（任务 05：显示必须跟真实起点一致）", () => {
  it("切到另一个话题后重新拉取消息，界面不再停在旧话题的对话上", async () => {
    const { session } = setup();
    const api = (await import("../../services/api")).api as unknown as {
      getSessionContext: ReturnType<typeof vi.fn>;
    };
    await session.loadHistory();
    expect(session.currentTopicId).toBe("t1");
    expect(session.messages.length).toBe(1);

    api.getSessionContext.mockResolvedValueOnce({
      topic_id: "t2",
      topic_name: "空话题",
      anchor_fragment: null,
      messages: [],
    });
    await session.setAnchorAndSync("t2", null, "空话题", undefined, false);

    expect(session.currentTopicId).toBe("t2");
    expect(session.messages.length).toBe(0); // 不再显示 t1 的历史
  });

  it("锚点没有真正变化时不做多余重载", async () => {
    const { session } = setup();
    const api = (await import("../../services/api")).api as unknown as {
      getSessionContext: ReturnType<typeof vi.fn>;
    };
    await session.loadHistory();
    const callsBefore = api.getSessionContext.mock.calls.length;
    await session.setAnchorAndSync("t1", null, "话题", undefined, false);
    expect(api.getSessionContext.mock.calls.length).toBe(callsBefore);
  });

  it("SSE ANCHOR 换话题且没有任务在跑时同样对齐消息", async () => {
    const { session, events } = setup();
    const api = (await import("../../services/api")).api as unknown as {
      getSessionContext: ReturnType<typeof vi.fn>;
    };
    await session.loadHistory();
    api.getSessionContext.mockResolvedValueOnce({
      topic_id: "t9",
      topic_name: "另一个话题",
      anchor_fragment: null,
      messages: [{ id: "m9", role: "assistant", content: "新话题的回答", content_type: "text", created_at: "" }],
    });
    events.route({ type: "ANCHOR", id: "e-anchor", ts: "", data: { topic_id: "t9", topic_name: "另一个话题", fragment_id: null, historic: false } });
    await new Promise((r) => setTimeout(r, 0));
    await Promise.resolve();
    expect(session.currentTopicId).toBe("t9");
    expect(session.messages.length).toBe(1);
    expect(session.messages[0].content).toBe("新话题的回答");
  });
});

describe("本机发送被拒绝（P0：没发出去的消息不留后遗症）", () => {
  it("被拒绝时发出「已拒绝」信号并撤掉乐观消息", async () => {
    const { session } = setup();
    const api = (await import("../../services/api")).api as unknown as {
      sendTurn: ReturnType<typeof vi.fn>;
    };
    api.sendTurn.mockRejectedValueOnce(new Error("network down"));
    expect(session.sendRejectedSeq).toBe(0);

    const ok = await session.send("这条会失败");

    expect(ok).toBe(false);
    expect(session.sendRejectedSeq).toBe(1);
    expect(session.lastError).toContain("network down");
    expect(session.messages.some((m) => m.content === "这条会失败")).toBe(false);
  });

  it("发送成功不发「已拒绝」信号", async () => {
    const { session } = setup();
    const ok = await session.send("正常发送");
    expect(ok).toBe(true);
    expect(session.sendRejectedSeq).toBe(0);
    expect(session.localSendSeq).toBe(1);
  });
});


describe("历史读取状态（读不到 ≠ 没有历史）", () => {
  it("成功：loading → ready", async () => {
    const { session } = setup();
    await session.loadHistory();
    expect(session.history.status).toBe("ready");
    expect(session.messages.length).toBe(1);
  });

  it("失败：标记 error 且保留已经拿到的消息，不清成空历史", async () => {
    const { session } = setup();
    await session.loadHistory();
    const before = session.messages.length;
    vi.mocked(api.getSessionContext).mockRejectedValueOnce(new Error("boom"));
    await session.loadHistory();
    expect(session.history.status).toBe("error");
    expect(session.history.error).toContain("boom");
    expect(session.messages.length).toBe(before);
  });

  it("重试成功：error → ready", async () => {
    const { session } = setup();
    vi.mocked(api.getSessionContext).mockRejectedValueOnce(new Error("boom"));
    await session.loadHistory();
    expect(session.history.status).toBe("error");
    await session.retryHistory();
    expect(session.history.status).toBe("ready");
  });
});

describe("受理即拿到 turn_id（停止按钮不必等 SSE）", () => {
  it("send() 用 POST 返回的 turn_id 立即可取消", async () => {
    const { session } = setup();
    vi.mocked(api.sendTurn).mockResolvedValueOnce({
      ok: true,
      accepted: true,
      turn_id: "turn_abc",
      status: "accepted",
      topic_id: null,
    });
    const ok = await session.send("你好");
    expect(ok).toBe(true);
    expect(session.activeTurnId).toBe("turn_abc");
    expect(session.turnRunning).toBe(true);
  });
});
