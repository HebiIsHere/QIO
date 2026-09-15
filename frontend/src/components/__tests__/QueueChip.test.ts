import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import QueueChip from "../QueueChip.vue";
import { useSessionStore } from "../../stores/session";

const mocks = vi.hoisted(() => ({ cancelTurn: vi.fn(async () => ({ ok: true })) }));
vi.mock("../../services/api", () => ({ api: { cancelTurn: mocks.cancelTurn } }));

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  const w = mount(QueueChip, { global: { plugins: [pinia] } });
  return { w, session };
}

beforeEach(() => {
  mocks.cancelTurn.mockReset();
  mocks.cancelTurn.mockResolvedValue({ ok: true });
});

describe("QueueChip 取消语义（任务01 D）", () => {
  it("运行中的任务用「停止」，排队项用「取消排队」——对象不同、文案不同", async () => {
    const { w, session } = setup();
    session.turnQueue = {
      running: { turn_id: "t1", message: "正在回答的问题" },
      queued: [{ turn_id: "t2", message: "排队的消息" }],
      cancelled: [],
    };
    await nextTick();
    await w.find(".chip").trigger("click");
    await flushPromises();
    const rows = w.findAll(".row");
    expect(rows[0].find(".qbtn").text()).toBe("停止");
    expect(rows[1].find(".qbtn").text()).toBe("取消排队");
  });

  it("点击取消后显示等待确认，期间禁止重复提交", async () => {
    const { w, session } = setup();
    let release: (v: { ok: boolean }) => void = () => {};
    mocks.cancelTurn.mockImplementationOnce(() => new Promise((r) => { release = r; }));
    session.turnQueue = {
      running: { turn_id: "t1", message: "正在回答的问题" },
      queued: [],
      cancelled: [],
    };
    await nextTick();
    await w.find(".chip").trigger("click");
    await flushPromises();
    await w.find(".row .qbtn").trigger("click");
    await flushPromises();
    expect(w.find(".row .qbtn").text()).toBe("正在停止…");
    expect(w.find(".row .qbtn").attributes("disabled")).toBeDefined();
    release({ ok: true });
    await flushPromises();
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
    await w.find(".chip").trigger("click");
    await flushPromises();
    await w.find(".row .qbtn").trigger("click");
    await flushPromises();
    const err = w.find(".cancel-err");
    expect(err.exists()).toBe(true);
    expect(err.text()).toContain("取消失败");
    expect(err.text()).toContain("未改变");
    w.unmount();
  });
});
