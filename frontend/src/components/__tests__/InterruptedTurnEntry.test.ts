/**
 * 「上次退出时没有执行的消息」入口：真实可用、措辞不模糊、不抢焦点。
 *
 * 这些用例守的是用户实际看到的东西：原文与时间要显示出来、继续/忽略要真的调接口、
 * 后端 409（已经处理过）要给可读反馈、Esc 只能收起自己这一块。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import InterruptedTurnEntry from "../InterruptedTurnEntry.vue";
import { useSessionStore } from "../../stores/session";
import { api } from "../../services/api";
import source from "../InterruptedTurnEntry.vue?raw";

vi.mock("../../services/api", () => ({
  ApiError: class ApiError extends Error {
    constructor(
      readonly status: number,
      readonly path: string,
      detail: string,
    ) {
      super(detail);
      this.name = "ApiError";
    }
  },
  api: {
    getRuntimeState: vi.fn(async () => ({
      instance_id: "i",
      revision: 1,
      turn_queue: { instance_id: "i", revision: 1, running: null, queued: [], cancelled: [] },
      approvals: [],
      interrupted_approvals: [],
      interrupted_turns: [],
      tasks: [],
      tools: [],
    })),
    resendInterruptedTurn: vi.fn(),
    dismissInterruptedTurn: vi.fn(),
    getDevTasks: vi.fn(async () => ({ tasks: [] })),
  },
}));

const resend = vi.mocked(api.resendInterruptedTurn);
const dismiss = vi.mocked(api.dismissInterruptedTurn);

const ITEM = {
  turn_id: "turn_a",
  message: "帮我把发布闸门跑一遍，再核对一下 sidecar 是不是最新的",
  topic_id: "topic_1",
  status: "interrupted",
  reason: "queued_at_restart",
  reason_text: "这条消息当时还在排队，进程退出后没有开始执行",
  created_at: "2026-10-01T10:00:00+00:00",
  ended_at: "2026-10-01T10:05:00+00:00",
};
const ITEM_2 = {
  ...ITEM,
  turn_id: "turn_b",
  message: "再看一下 cross_topic 的召回",
  reason: "running_at_restart",
  reason_text: "这条消息执行到一半，进程退出后没有完成",
};

function mountEntry(items: unknown[] = []) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  session.interruptedTurns = items as never;
  const w = mount(InterruptedTurnEntry, { attachTo: document.body, global: { plugins: [pinia] } });
  return { w, session };
}

beforeEach(() => {
  vi.clearAllMocks();
  resend.mockResolvedValue({ ok: true, recovered_turn_id: "turn_a", turn_id: "turn_new", status: "queued" } as never);
  dismiss.mockResolvedValue({ ok: true, dismissed: "turn_a" } as never);
  document.body.innerHTML = "";
});

describe("入口本身", () => {
  it("没有未执行的消息时什么都不渲染", () => {
    const { w } = mountEntry([]);
    expect(w.find(".interrupted-turns").exists()).toBe(false);
  });

  it("收起时就把话说清楚：几条、什么时候没执行 —— 不用「有新任务」这种模糊措辞", () => {
    const { w } = mountEntry([ITEM]);
    const entry = w.find(".entry");
    expect(entry.exists()).toBe(true);
    expect(entry.text()).toContain("有 1 条消息在上次退出时没有执行");
    expect(entry.text()).not.toContain("有新任务");
    expect(entry.attributes("aria-expanded")).toBe("false");
    expect(entry.attributes("aria-label")).toContain("展开可以继续发送或者忽略");
    // 原生按钮 = 键盘可达（Tab / Enter / Space 都走浏览器默认行为）
    expect(entry.element.tagName).toBe("BUTTON");
    expect(entry.attributes("type")).toBe("button");
  });

  it("展开后给出原文、原因与「何时被中断」，以及继续/忽略两个动作", async () => {
    const { w } = mountEntry([ITEM]);
    await w.find(".entry").trigger("click");

    const panel = w.find(".panel");
    expect(panel.exists()).toBe(true);
    expect(panel.attributes("role")).toBe("region");
    // 原文在 DOM 里是完整的（视觉上两行截断，读屏软件读全文）
    expect(panel.find(".message").text()).toBe(ITEM.message);
    expect(panel.text()).toContain("这条消息当时还在排队");
    expect(panel.find(".when").text()).toMatch(
      /(今天 \d{2}:\d{2}|\d{1,2} 月 \d{1,2} 日 \d{2}:\d{2})/,
    );
    expect(panel.find("button.btn-resume").text()).toContain("继续发送这条");
    expect(panel.find("button.btn-dismiss").text()).toContain("忽略");
    expect(w.find(".entry").attributes("aria-expanded")).toBe("true");
  });

  it("多条时列出全部，并提供一次「全部忽略」", async () => {
    const { w } = mountEntry([ITEM, ITEM_2]);
    expect(w.find(".entry").text()).toContain("有 2 条消息在上次退出时没有执行");
    await w.find(".entry").trigger("click");

    expect(w.findAll(".item")).toHaveLength(2);
    const bulk = w.find("button.btn-dismiss-all");
    expect(bulk.exists()).toBe(true);
    expect(bulk.text()).toContain("全部忽略（2 条）");
  });

  it("一条时不出现「全部忽略」（没意义的多余操作）", async () => {
    const { w } = mountEntry([ITEM]);
    await w.find(".entry").trigger("click");
    expect(w.find("button.btn-dismiss-all").exists()).toBe(false);
  });
});

describe("继续 / 忽略真的落到接口上", () => {
  it("点「继续发送这条」→ 调 resend，成功后入口消失并留下结果说明", async () => {
    const { w, session } = mountEntry([ITEM]);
    await w.find(".entry").trigger("click");
    await w.find("button.btn-resume").trigger("click");
    await flushPromises();

    expect(resend).toHaveBeenCalledWith("turn_a");
    expect(session.interruptedTurns).toHaveLength(0);
    expect(w.find(".entry").exists()).toBe(false);
    expect(w.find("p.notice").text()).toContain("重新排队");
  });

  it("请求在飞的时候按钮禁用并显示「提交中…」（重复点击不会发第二次）", async () => {
    let release: (v: unknown) => void = () => {};
    resend.mockImplementationOnce(() => new Promise((r) => { release = r; }) as never);
    const { w } = mountEntry([ITEM]);
    await w.find(".entry").trigger("click");

    await w.find("button.btn-resume").trigger("click");
    await flushPromises();
    const btn = w.find("button.btn-resume");
    expect(btn.text()).toContain("提交中…");
    expect(btn.attributes("disabled")).toBeDefined();

    // 再点一次：不会产生第二个请求
    await btn.trigger("click");
    expect(resend).toHaveBeenCalledTimes(1);

    release({ ok: true, recovered_turn_id: "turn_a", turn_id: "turn_new", status: "queued" });
    await flushPromises();
  });

  it("后端 409（已经被处理过）→ 列表清空后反馈仍然留在屏幕上，不是静默失败", async () => {
    resend.mockRejectedValueOnce(Object.assign(new Error("409"), { status: 409 }) as never);
    const { w, session } = mountEntry([ITEM]);
    await w.find(".entry").trigger("click");
    await w.find("button.btn-resume").trigger("click");
    await flushPromises();

    // 后端说"这条已经不在未执行状态"：入口本身没有了……
    expect(session.interruptedTurns).toHaveLength(0);
    expect(w.find(".entry").exists()).toBe(false);
    // ……但用户点完必须看到结果
    const notice = w.find("p.notice");
    expect(notice.exists()).toBe(true);
    expect(notice.attributes("role")).toBe("status");
    expect(notice.text()).toContain("已经被处理过");
  });

  it("网络失败 → 入口保留，并说明可以重试", async () => {
    resend.mockRejectedValueOnce(new Error("fetch failed") as never);
    const { w, session } = mountEntry([ITEM]);
    await w.find(".entry").trigger("click");
    await w.find("button.btn-resume").trigger("click");
    await flushPromises();

    expect(session.interruptedTurns).toHaveLength(1);
    expect(w.find("p.notice").text()).toContain("可以重试");
  });

  it("点「忽略」→ 调 dismiss，成功后入口消失并留下结果说明", async () => {
    const { w } = mountEntry([ITEM]);
    await w.find(".entry").trigger("click");
    await w.find("button.btn-dismiss").trigger("click");
    await flushPromises();

    expect(dismiss).toHaveBeenCalledWith("turn_a");
    expect(w.find(".entry").exists()).toBe(false);
    expect(w.find("p.notice").text()).toContain("已忽略");
  });
});

describe("无障碍与「不抢焦点」", () => {
  it("出现与展开都不会抢走用户当前的焦点", async () => {
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();

    const { w } = mountEntry([ITEM]);
    await flushPromises();
    expect(document.activeElement).toBe(input);

    await w.find(".entry").trigger("click");
    await flushPromises();
    expect(document.activeElement).toBe(input);
    // 渲染出来的 DOM 里不许有 autofocus 这类主动抢焦点的东西
    expect(w.html()).not.toContain("autofocus");
  });

  it("Esc 只收起这一块，不动任何记录", async () => {
    const { w, session } = mountEntry([ITEM]);
    await w.find(".entry").trigger("click");
    expect(w.find(".panel").exists()).toBe(true);

    await w.find(".interrupted-turns").trigger("keydown", { key: "Escape" });
    expect(w.find(".panel").exists()).toBe(false);
    expect(session.interruptedTurns).toHaveLength(1);
    expect(w.find(".entry").attributes("aria-expanded")).toBe("false");
  });

  it("键盘焦点样式与颜色令牌：有 :focus-visible，且不写死色值", () => {
    expect(source).toContain(":focus-visible");
    const style = source.slice(source.indexOf("<style"));
    const hex = style.match(/#[0-9a-fA-F]{3,8}\b/g) ?? [];
    expect(hex).toEqual([]);
  });
});
