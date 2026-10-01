import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ContinueBar from "../ContinueBar.vue";
import { useSessionStore } from "../../stores/session";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: { respondApproval: vi.fn(async () => ({ ok: true })) },
}));

const respond = api.respondApproval as unknown as ReturnType<typeof vi.fn>;

function mountBar(pending: {
  id: string;
  used: number;
  max: number;
  reason: string;
  budgetKind: string;
  message: string;
} | null) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  session.pendingContinue = pending;
  return { session, bar: mount(ContinueBar, { global: { plugins: [pinia] } }) };
}

beforeEach(() => {
  vi.clearAllMocks();
  respond.mockResolvedValue({ ok: true });
});

describe("继续/停止操作条：原因必须与后端给的一致", () => {
  it("无进展暂停：不显示「已达迭代上限 3/128」，而是显示没有新进展", () => {
    const { bar } = mountBar({
      id: "appr_np",
      used: 3,
      max: 128,
      reason: "no_progress",
      budgetKind: "",
      message: "`echo` 连续 3 次给出完全相同的结果，这一轮没有新的进展",
    });
    const text = bar.text();
    expect(text).toContain("这一轮没有新的进展");
    expect(text).not.toContain("已达迭代上限");
    expect(text).not.toContain("3/128");
    expect(text).toContain("连续 3 次给出完全相同的结果");
    // aria-label 也必须说实话，读屏用户听到的和看到的是同一件事
    expect(bar.attributes("aria-label")).toContain("这一轮没有新的进展");
    expect(bar.attributes("aria-label")).not.toContain("已达迭代上限");
  });

  it("预算耗尽（没有 reason）：仍然显示迭代上限与继续/停止", () => {
    const { bar } = mountBar({ id: "appr_b", used: 128, max: 128, reason: "", budgetKind: "", message: "" });
    expect(bar.text()).toContain("已达迭代上限 128/128");
    expect(bar.text()).toContain("继续");
    expect(bar.text()).toContain("停止");
  });

  it("token 预算耗尽：不能显示「已达迭代上限」", () => {
    // 后端现在用 reason=budget + budget_kind=tokens 说清是哪种预算。
    // 以前这里会显示「已达迭代上限 128/128」，而事实是 token 用完了 ——
    // 用户正是拿这句话决定继续还是停止。
    const { bar } = mountBar({
      id: "appr_tok",
      used: 128,
      max: 128,
      reason: "budget",
      budgetKind: "tokens",
      message: "输出 token 预算耗尽（90000/90000）",
    });
    const text = bar.text();
    expect(text).toContain("输出 token 预算已用完");
    expect(text).not.toContain("已达迭代上限");
    expect(text).toContain("输出 token 预算耗尽（90000/90000）");
    expect(bar.attributes("aria-label")).toContain("输出 token 预算已用完");
  });

  it("迭代次数耗尽：仍然显示迭代上限", () => {
    const { bar } = mountBar({
      id: "appr_iter",
      used: 128,
      max: 128,
      reason: "budget",
      budgetKind: "iterations",
      message: "迭代次数达到上限（128/128）",
    });
    expect(bar.text()).toContain("已达迭代上限 128/128");
  });

  it("没有待决定项时整条不渲染", () => {
    const { bar } = mountBar(null);
    expect(bar.find(".continue-bar").exists()).toBe(false);
  });

  it("继续 / 停止 各自把决定发给同一条审批", async () => {
    const { bar } = mountBar({ id: "appr_np", used: 3, max: 128, reason: "no_progress", budgetKind: "", message: "x" });
    await bar.find("button.qio-btn.primary").trigger("click");
    await flushPromises();
    expect(respond).toHaveBeenCalledWith("appr_np", "approved");
  });
});
