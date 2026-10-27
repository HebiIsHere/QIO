/**
 * uf2 · F08：审批到期状态必须随时间推进（B 组回归）。
 *
 * 已复现缺陷：申请打开时还有 1 秒有效期；1.5 秒后没有任何新事件，批准按钮仍在、
 * 过期说明不显示（computed 里的 Date.now() 不会自行驱动更新）。
 *
 * 验收：
 * - 打开时未过期、阅读期间自然过期：批准入口移除/禁用 + 真实过期说明；
 * - 切换申请：时钟按新申请的期限重建；
 * - 让位 / 后台恢复：不依赖下一次事件，恢复可见时立即核对实际期限；
 * - 关闭 / 卸载：清理定时器，不留空转时钟；
 * - 期限只读：绝不改写 expiresAt，提交层与后端拦截保留。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

const mocks = vi.hoisted(() => ({ request: vi.fn(), respondApproval: vi.fn() }));
vi.mock("../../../services/api", () => ({
  request: mocks.request,
  api: { respondApproval: mocks.respondApproval },
}));

import UnifiedApprovalModal from "../UnifiedApprovalModal.vue";
import { useApprovalsStore } from "../../../stores/approvals";
import { useUnifiedApprovalsStore } from "../../../stores/unifiedApprovals";
import { useModalArbiterStore } from "../../../stores/modalArbiter";

const T0 = new Date("2026-10-27T01:00:00.000Z").getTime();

let wrapper: VueWrapper | null = null;

function setup() {
  setActivePinia(createPinia());
  return {
    unified: useUnifiedApprovalsStore(),
    approvals: useApprovalsStore(),
    arbiter: useModalArbiterStore(),
  };
}

function mountModal() {
  wrapper = mount(UnifiedApprovalModal, { attachTo: document.body });
  return wrapper;
}

/** 只推进定时器与 Vue 更新（假时钟下不能用 flushPromises 之类的宏任务等待）。 */
async function tick(ms: number) {
  await vi.advanceTimersByTimeAsync(ms);
  await nextTick();
  await nextTick();
}

function enqueueWithExpiry(
  approvals: ReturnType<typeof useApprovalsStore>,
  id: string,
  expiresAtMs: number,
) {
  approvals.enqueue(
    id,
    "tool_execution",
    { description: "执行命令 " + id },
    { autoOpen: true, meta: { expiresAt: new Date(expiresAtMs).toISOString() } },
  );
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(T0));
  mocks.request.mockReset();
  mocks.respondApproval.mockReset();
  mocks.request.mockResolvedValue({ ok: true, decision: "approve" });
  mocks.respondApproval.mockResolvedValue({ ok: true });
  document.body.innerHTML = "";
  try {
    localStorage.clear();
  } catch {
    // 存储不可用时只影响跨用例的暂缓键
  }
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  document.body.innerHTML = "";
  vi.useRealTimers();
});

describe("uf2-F08 打开时未过期，阅读期间自然过期", () => {
  it("到点后（无新事件）移除批准入口、显示真实过期说明；期限不被改写、提交仍被拦", async () => {
    const { unified, approvals } = setup();
    const expiresAt = new Date(T0 + 1500).toISOString();
    enqueueWithExpiry(approvals, "appr_live", T0 + 1500);
    unified.open("tool:appr_live");
    const w = mountModal();
    await tick(0);

    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(true);
    expect(w.find('[data-test="unified-approval-expired"]').exists()).toBe(false);

    await tick(1400);
    // 还没到点：仍然可以批准
    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(true);
    expect(w.find('[data-test="unified-approval-expired"]').exists()).toBe(false);

    await tick(200);
    // 自然过期：不依赖任何后端事件
    expect(w.find('[data-test="unified-approval-expired"]').exists()).toBe(true);
    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(false);
    // 拒绝仍可用：到期只关闭「批准」
    expect(w.find('[data-test="unified-approval-reject"]').exists()).toBe(true);
    // 期限只读：不改写真实期限
    expect(approvals.queue[0]?.expiresAt).toBe(expiresAt);

    const ok = await unified.submit("approve");
    expect(ok).toBe(false);
    expect(unified.submitError).toContain("到期");
    expect(mocks.respondApproval).not.toHaveBeenCalled();
  });
});

describe("uf2-F08 时钟的清理与重建", () => {
  it("切换申请：按新申请的期限重建；切回后仍按各自真实期限判断", async () => {
    const { unified, approvals } = setup();
    enqueueWithExpiry(approvals, "appr_long", T0 + 3000);
    enqueueWithExpiry(approvals, "appr_short", T0 + 900);
    unified.open("tool:appr_long");
    const w = mountModal();
    await tick(500);
    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(true);

    // 切到短期限的一条：时钟改成 900ms 的期限
    unified.switchTo("tool:appr_short");
    await tick(0);
    expect(w.find('[data-test="unified-approval-expired"]').exists()).toBe(false);
    await tick(500); // t≈1000 > 900
    expect(w.find('[data-test="unified-approval-expired"]').exists()).toBe(true);
    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(false);

    // 切回长期限的一条：它还没到期，批准入口恢复
    unified.switchTo("tool:appr_long");
    await tick(0);
    expect(w.find('[data-test="unified-approval-expired"]').exists()).toBe(false);
    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(true);

    await tick(2100); // t≈3100 > 3000
    expect(w.find('[data-test="unified-approval-expired"]').exists()).toBe(true);
    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(false);
  });

  it("关闭 / 卸载：清理到期定时器，不留下空转时钟", async () => {
    const { unified, approvals } = setup();
    enqueueWithExpiry(approvals, "appr_long", T0 + 60_000);
    unified.open("tool:appr_long");
    const w = mountModal();
    await tick(0);
    expect(vi.getTimerCount()).toBeGreaterThan(0);

    unified.closePanel();
    await tick(0);
    expect(vi.getTimerCount()).toBe(0);

    // 重新打开后按时钟工作，卸载时同样清理
    unified.open("tool:appr_long");
    await tick(0);
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    w.unmount();
    wrapper = null;
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe("uf2-F08 让位与后台恢复：恢复可见时立即核对实际期限", () => {
  it("被更高优先级盖住期间到期：恢复可见后立即显示过期（不依赖下一次事件）", async () => {
    const { unified, approvals, arbiter } = setup();
    enqueueWithExpiry(approvals, "appr_live", T0 + 1000);
    unified.open("tool:appr_live");
    const w = mountModal();
    await tick(0);

    arbiter.open({ id: "impact-1", kind: "impact" });
    await tick(0);
    expect((w.find(".ua-mask").element as HTMLElement).style.display).toBe("none");

    // 让位期间时间流逝（不推进定时器，模拟图层不可见时不空转）
    vi.setSystemTime(new Date(T0 + 5000));
    expect(w.find('[data-test="unified-approval-expired"]').exists()).toBe(false);

    arbiter.close("impact-1");
    await tick(0);
    expect((w.find(".ua-mask").element as HTMLElement).style.display).toBe("");
    expect(w.find('[data-test="unified-approval-expired"]').exists()).toBe(true);
    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(false);
  });

  it("窗口回到前台：立即按真实时间核对期限（后台节流/定时器没跑的兜底）", async () => {
    const { unified, approvals } = setup();
    enqueueWithExpiry(approvals, "appr_live", T0 + 1000);
    unified.open("tool:appr_live");
    const w = mountModal();
    await tick(0);
    expect(w.find('[data-test="unified-approval-expired"]').exists()).toBe(false);

    // 模拟后台：系统时间已经走过期限，但定时器还没跑过
    vi.setSystemTime(new Date(T0 + 3000));
    document.dispatchEvent(new Event("visibilitychange"));
    await tick(0);

    expect(w.find('[data-test="unified-approval-expired"]').exists()).toBe(true);
    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(false);
  });
});
