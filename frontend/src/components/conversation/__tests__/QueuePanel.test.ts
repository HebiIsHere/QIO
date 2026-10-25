/**
 * 入口④「排队」面板（components/conversation/QueuePanel.vue）。
 *
 * 迁移自旧的 components/QueueChip.vue 测试（2026-10-26 合并后清理 · C1）：
 * 旧 QueueChip 已删除，它的有效能力由入口④承接 —— 运行中/排队中/已取消的真实区分、
 * 「取消请求 ≠ 已取消」、取消失败就地可见可重试、本机未确认的行不能按身份取消。
 * 负向断言一条不少：失败不假装已取消、重复点击不重复提交。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import QueuePanel from "../QueuePanel.vue";
import { useSessionStore } from "../../../stores/session";

const mocks = vi.hoisted(() => ({ cancelTurn: vi.fn(async () => ({ ok: true })) }));
vi.mock("../../../services/api", () => ({ api: { cancelTurn: mocks.cancelTurn } }));

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  const w = mount(QueuePanel, { global: { plugins: [pinia] } });
  return { w, session };
}

beforeEach(() => {
  mocks.cancelTurn.mockReset();
  mocks.cancelTurn.mockResolvedValue({ ok: true });
});

describe("入口④ 排队面板的取消语义", () => {
  it("运行中的任务用「停止」，排队项用「取消排队」——对象不同、文案不同", async () => {
    const { w, session } = setup();
    session.turnQueue = {
      running: { turn_id: "t1", message: "正在回答的问题" },
      queued: [{ turn_id: "t2", message: "排队的消息" }],
      cancelled: [],
    };
    await nextTick();
    const rows = w.findAll(".row");
    expect(rows).toHaveLength(2);
    expect(rows[0].find("button").text()).toBe("停止");
    expect(rows[1].find("button").text()).toBe("取消排队");
    w.unmount();
  });

  it("点击取消后显示等待确认，期间禁止重复提交", async () => {
    const { w, session } = setup();
    let release: (v: { ok: boolean }) => void = () => {};
    mocks.cancelTurn.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );
    session.turnQueue = {
      running: { turn_id: "t1", message: "正在回答的问题" },
      queued: [],
      cancelled: [],
    };
    await nextTick();
    const btn = w.find(".row button");
    await btn.trigger("click");
    await flushPromises();
    const pending = w.find(".row button");
    expect(pending.text()).toBe("正在停止…");
    expect(pending.attributes("disabled")).toBeDefined();
    release({ ok: true });
    await flushPromises();
    // 请求成功只说明「取消请求已提交」，不能宣称已经停止
    expect(w.find(".notice").text()).toContain("以后端确认为准");
    w.unmount();
  });

  it("取消失败：错误就地可见，不假装已经取消", async () => {
    const { w, session } = setup();
    mocks.cancelTurn.mockRejectedValueOnce(new Error("500 boom"));
    session.turnQueue = {
      running: { turn_id: "t1", message: "正在回答的问题" },
      queued: [],
      cancelled: [],
    };
    await nextTick();
    await w.find(".row button").trigger("click");
    await flushPromises();
    const err = w.find(".error");
    expect(err.exists()).toBe(true);
    expect(err.text()).toContain("取消没有成功");
    expect(err.text()).toContain("未改变");
    expect(w.find(".notice").exists()).toBe(false);
    w.unmount();
  });

  it("已取消的项如实显示；本机未确认的行不能按身份取消", async () => {
    const { w, session } = setup();
    session.turnQueue = {
      running: null,
      queued: [],
      cancelled: [{ turn_id: "t9", message: "已经取消的消息" }],
    };
    session.messages = [
      { id: "local1", content: "本机刚提交、还没有回执的消息", queued: true, turnId: undefined },
    ] as never;
    await nextTick();
    const text = w.text();
    expect(text).toContain("已取消");
    expect(text).toContain("本机已提交");
    expect(text).toContain("不能按身份取消");
    expect(w.findAll(".row button.stop")).toHaveLength(0);
    w.unmount();
  });
});
