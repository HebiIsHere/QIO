/**
 * F05 反例（acc-c）：排队消息不得改变活动 turn 的归属。
 *
 * 真实事件链：A 正在跑 → 发送 B（后端受理并进入队列）→ A 继续说明/调用工具/给出回答。
 * 期望：A 的过程区仍在运行、A 的回答仍归 A；B 是独立的排队轮（等待开始）。
 *
 * jsdom 拿不到虚拟列表容器尺寸，这里用最小替身让每个 turn 都真实渲染，
 * 断言的是 MessageStream 的分组与 TurnProcess 的状态（不是 tanstack 的虚拟化）。
 */
import { describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import MessageStream from "../MessageStream.vue";
import { useSessionStore } from "../../stores/session";
import { useEventStore } from "../../stores/events";

vi.mock("@tanstack/vue-virtual", () => ({
  useVirtualizer: (options: { value?: { count?: number } }) => {
    const count = () => options?.value?.count ?? 0;
    const api = {
      getTotalSize: () => count() * 120,
      getVirtualItems: () =>
        Array.from({ length: count() }, (_, index) => ({
          index,
          start: index * 120,
          key: index,
          size: 120,
        })),
      measureElement: () => {},
    };
    return { __v_isRef: true, value: api };
  },
}));

const sendTurn = vi.hoisted(() => vi.fn());

vi.mock("../../services/api", () => ({
  api: {
    sendTurn,
    getSessionContext: vi.fn(async () => ({
      topic_id: "t1",
      topic_name: "话题",
      anchor_fragment: null,
      messages: [],
    })),
  },
}));

async function settle() {
  await nextTick();
  await nextTick();
  await nextTick();
}

describe("F05：排队 turn 不改变活动 turn 归属", () => {
  it("A 执行中 B 排队：A 的过程区继续运行，A 的回答留在 A 这一轮", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    const events = useEventStore();
    sendTurn.mockReset();
    sendTurn.mockResolvedValueOnce({ ok: true, turn_id: "turn_a" });
    sendTurn.mockResolvedValueOnce({ ok: true, turn_id: "turn_b" });

    const w = mount(MessageStream, { global: { plugins: [pinia] } });
    await nextTick();

    // A：发送 → 受理 → TURN_START
    await session.send("问题A");
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "turn_a", revision: 1 } });
    events.route({
      type: "ASSISTANT",
      id: "2",
      ts: "",
      data: { turn_id: "turn_a", content: "A 的过程说明", interim: true, streaming: true, delta_id: "dl_a1", seq: 1, stage_id: "st_a1" },
    });
    events.route({ type: "TOOL_START", id: "3", ts: "", data: { turn_id: "turn_a", call_id: "c_a1", tool: "fs_read" } });
    events.route({ type: "TOOL_END", id: "4", ts: "", data: { turn_id: "turn_a", call_id: "c_a1", tool: "fs_read", ok: true, content_preview: "ok" } });

    // A 还在跑：发送 B → 后端受理为排队项
    await session.send("问题B");
    expect(session.activeTurnId).toBe("turn_a");
    expect(session.isQueuedTurn("turn_b")).toBe(true);

    // A 在 B 排队之后继续说明并给出正式回答
    events.route({
      type: "ASSISTANT",
      id: "5",
      ts: "",
      data: { turn_id: "turn_a", content: "A 的最终回答", interim: false, streaming: false, delta_id: "dl_a2", seq: 1 },
    });
    await settle();

    const turns = w.findAll(".turn");
    expect(turns.length).toBe(2);
    const [turnA, turnB] = turns;

    // A 仍是运行中的那一轮（排队不改变活动 turn）
    expect(turnA.find('[data-test="turn-process"]').attributes("data-state")).toBe("running");
    // B 是排队轮：等待开始，不是运行中
    expect(turnB.find('[data-test="turn-process"]').attributes("data-state")).toBe("waiting");

    // A 的内容（过程说明 / 工具 / 最终回答）全部留在 A
    expect(turnA.text()).toContain("A 的过程说明");
    expect(turnA.text()).toContain("A 的最终回答");
    // A 的工具调用事实也留在 A 的状态行（历史抽屉收起时工具卡不进 DOM）
    expect(turnA.text()).toContain("1 次调用");
    // B 轮里只有 B 的用户消息，不能被 A 的回答吸收
    expect(turnB.text()).toContain("问题B");
    expect(turnB.text()).not.toContain("A 的最终回答");
    expect(turnB.text()).not.toContain("A 的过程说明");
    expect(turnB.text()).not.toContain("1 次调用");

    w.unmount();
  });
});

describe("F05：无 turn_id 的旧历史兼容规则（groupTurns）", () => {
  function msg(over: Partial<import("../../stores/session").StreamMessage> & { id: string }): import("../../stores/session").StreamMessage {
    return { role: "assistant", content: "", contentType: "text", createdAt: "2026-10-09T00:00:00Z", ...over };
  }
  const opts = (over: { turnRunning?: boolean; activeTurnId?: string | null } = {}) => ({
    turnRunning: false,
    activeTurnId: null as string | null,
    stagesFor: () => [],
    factsFor: () => null,
    ...over,
  });

  it("旧历史整体按位置分组：user/system 开新轮，其余挂在当前轮", async () => {
    const { groupTurns } = await import("../../stores/session");
    const turns = groupTurns(
      [
        msg({ id: "u1", role: "user", content: "问题一" }),
        msg({ id: "a1", content: "回答一" }),
        msg({ id: "s1", role: "system", content: "系统" }),
        msg({ id: "u2", role: "user", content: "问题二" }),
      ],
      opts(),
    );
    expect(turns.length).toBe(3);
    expect(turns[0]!.items.map((m) => m.id)).toEqual(["u1", "a1"]);
    expect(turns[1]!.items.map((m) => m.id)).toEqual(["s1"]);
    expect(turns[2]!.items.map((m) => m.id)).toEqual(["u2"]);
  });

  it("无 turn_id 的实时事件优先挂到 active turn，不挂到排队的最后一轮", async () => {
    const { groupTurns } = await import("../../stores/session");
    const turns = groupTurns(
      [
        msg({ id: "u1", role: "user", content: "A", turnId: "turn_a" }),
        msg({ id: "a1", content: "A 说明", turnId: "turn_a", interim: true }),
        msg({ id: "u2", role: "user", content: "B", turnId: "turn_b", queued: true }),
        // 旧后端：这一条没有 turn_id，但 active turn 是 turn_a
        msg({ id: "a2", content: "A 的后续" }),
      ],
      opts({ turnRunning: true, activeTurnId: "turn_a" }),
    );
    expect(turns.length).toBe(2);
    expect(turns[0]!.items.map((m) => m.id)).toEqual(["u1", "a1", "a2"]);
    expect(turns[0]!.running).toBe(true);
    expect(turns[1]!.items.map((m) => m.id)).toEqual(["u2"]);
    expect(turns[1]!.queued).toBe(true);
    expect(turns[1]!.running).toBe(false);
  });

  it("乐观消息的临时 turn_id 不吞掉活动轮的后续内容", async () => {
    const { groupTurns } = await import("../../stores/session");
    const turns = groupTurns(
      [
        msg({ id: "u1", role: "user", content: "A", turnId: "turn_a" }),
        // 刚发出、还没拿到受理回执：乐观消息暂时带着 active 的 turn_id
        msg({ id: "u2", role: "user", content: "B", turnId: "turn_a", queued: true }),
        msg({ id: "a1", content: "A 的后续", turnId: "turn_a" }),
      ],
      opts({ turnRunning: true, activeTurnId: "turn_a" }),
    );
    expect(turns.length).toBe(2);
    expect(turns[0]!.turnId).toBe("turn_a");
    expect(turns[0]!.items.map((m) => m.id)).toEqual(["u1", "a1"]);
    expect(turns[1]!.items.map((m) => m.id)).toEqual(["u2"]);
    expect(turns[1]!.queued).toBe(true);
  });
});
