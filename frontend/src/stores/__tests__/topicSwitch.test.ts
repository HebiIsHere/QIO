/**
 * 旧「待确认话题切换」路径已退役（契约 §2.4）。
 *
 * 本轮起唯一的话题换题入口是**话题申请**（后端受控工具调用 → APPROVAL_REQUIRED
 * 的 topic_switch / topic_create → 统一审批弹窗决定）。这里钉住旧路径退役后的行为：
 *
 * - 旧 TOPIC_SWITCH_SUGGESTED 事件仍能被路由（兼容旧回放），但**不产生任何可执行状态**：
 *   不改 Anchor、不亮确认条、不打开任何独立提交入口；
 * - session 不再有 pendingSwitch / setPendingSwitch / confirmPendingSwitch /
 *   rejectPendingSwitch 这类可执行建议与独立提交方法；
 * - 客户端 API 上也不再出现旧话题切换接口的调用（api.ts 的死代码由 C 清理）；
 * - 等待状态只来自统一审批队列（见 topicApprovalState.test.ts），旧事件不会伪造它。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

import { useEventStore } from "../events";
import { useSessionStore } from "../session";
import { api } from "../../services/api";
import type { AgentEvent } from "../../services/events";

vi.mock("../../services/api", () => ({
  api: {
    // 与真实后端一致：会话上下文返回当前 Anchor 所在的话题
    getSessionContext: vi.fn(async () => ({
      topic_id: "t1",
      topic_name: "QIO 前端",
      anchor_fragment: null,
      messages: [],
    })),
    loadHistory: vi.fn(async () => ({ messages: [], anchor: null })),
    sendTurn: vi.fn(async () => ({})),
    cancelTurn: vi.fn(async () => ({ ok: true, cancelled: true })),
    // 旧话题切换接口已退役（api.ts 里只剩 @deprecated 声明，路由返回 410）：
    // 这里保留 mock 只为断言「旧路径不会再调用它们」。
    confirmTopicSwitch: vi.fn(async () => ({
      ok: true,
      topic_id: "t2",
      fragment_id: null,
      fragment_title: null,
      historic: false,
    })),
    rejectTopicSwitch: vi.fn(async () => ({ ok: true })),
  },
}));

let seq = 0;
function ev(type: string, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, id: `e-${type}-${seq}`, ts: "", data } as AgentEvent;
}

/** 按运行时对象形状读取（退役后的字段/方法必须真的不存在，而不是只是没人调用）。 */
function surface(session: ReturnType<typeof useSessionStore>): Record<string, unknown> {
  return session as unknown as Record<string, unknown>;
}

function setup() {
  setActivePinia(createPinia());
  const session = useSessionStore();
  const events = useEventStore();
  session.currentTopicId = "t1";
  session.topicName = "QIO 前端";
  return { session, events };
}

beforeEach(() => {
  localStorage.clear();
  vi.mocked(api.confirmTopicSwitch).mockClear();
  vi.mocked(api.rejectTopicSwitch).mockClear();
});

describe("旧话题切换建议路径已退役", () => {
  it("收到旧 TOPIC_SWITCH_SUGGESTED：不建立可执行状态，Anchor 原地不动", () => {
    const { session, events } = setup();

    events.route(
      ev("TOPIC_SWITCH_SUGGESTED", {
        from_topic_id: "t1",
        topic_id: "t2",
        topic_name: "顺丁橡胶降解",
        reason: "推测",
      }),
    );

    expect(session.currentTopicId).toBe("t1");
    expect(session.topicName).toBe("QIO 前端");
    // 旧状态字段整体退役（不是「留着但没更新」）
    expect(surface(session).pendingSwitch).toBeUndefined();
    expect(surface(session).pendingSwitchBusy).toBeUndefined();
    // 也不伪造新的等待状态：等待状态只来自统一审批队列
    expect(session.topicApprovalPending).toBe(false);
  });

  it("旧独立提交入口整体退役：不存在可执行的确认/拒绝方法", () => {
    const { session } = setup();

    expect(surface(session).setPendingSwitch).toBeUndefined();
    expect(surface(session).confirmPendingSwitch).toBeUndefined();
    expect(surface(session).rejectPendingSwitch).toBeUndefined();
  });

  it("旧事件与新等待状态都不会触发旧话题切换接口（api.ts 仅保留 @deprecated 声明）", () => {
    const { session, events } = setup();

    events.route(
      ev("TOPIC_SWITCH_SUGGESTED", {
        from_topic_id: "t1",
        topic_id: "t2",
        topic_name: "顺丁橡胶降解",
      }),
    );

    expect(api.confirmTopicSwitch).not.toHaveBeenCalled();
    expect(api.rejectTopicSwitch).not.toHaveBeenCalled();
    expect(session.topicApprovalPending).toBe(false);
  });

  it("重复 / 迟到的旧建议事件也不会堆积出可执行状态", () => {
    const { session, events } = setup();

    for (let i = 0; i < 3; i += 1) {
      events.route(
        ev("TOPIC_SWITCH_SUGGESTED", {
          from_topic_id: "t1",
          topic_id: "t2",
          topic_name: "顺丁橡胶降解",
        }),
      );
    }
    events.route(ev("TURN_START", { turn_id: "turn_1" }));

    expect(session.currentTopicId).toBe("t1");
    expect(surface(session).pendingSwitch).toBeUndefined();
    expect(session.topicApprovalPending).toBe(false);
  });
});
