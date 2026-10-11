/**
 * 【独立验收 D · 第八轮】N3：剩余撤回弹窗的「继续 / 取消」都必须真实有效。
 *
 * 触发（契约 §2/§5 / 本轮反例 N3）：
 *   演示任务两张结果卡片 → 其中一张与自己的注释连接 → 让任务失败 → 安全部分撤回，
 *   其余进入 pendingDecision。
 * 基线现状：弹窗里「继续」和「取消」都只是 dismiss() —— 继续不执行任何撤回、取消也关不掉提示。
 * 正确行为：继续必须真的调用带 decisionIds 的撤回执行并取得真实结果（经 N4 保护）；
 *   取消 / Escape 只结束本次提示、保留板面、不执行撤回、能关掉提示，多任务按 intentId 处理。
 *
 * 证据分层：②真实组件 DOM + 真实 store/服务层（传输为假后端，断言真实请求体）。
 * 本文件在 b3245e5 上必须失败（红）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import ImpactConfirmDialog from "../components/interactive/ImpactConfirmDialog.vue";
import { useInteractiveStore } from "../stores/interactive";
import { installFakeBackend, type FakeBackend } from "./support/fakeBackend";
import { intent } from "./support/harness";

let backend: FakeBackend | null = null;
let wrapper: VueWrapper | null = null;

/** 失败任务：两张结果卡片里，c2 与用户自己的注释相连 → 需要用户决定是否撤回。 */
function failedIntentWithPending(): never {
  return {
    ...(intent("i9", "failed") as Record<string, unknown>),
    title: "会失败的任务",
    revert: {
      reverted: ["卡片 c1"],
      kept: [],
      pendingDecision: [{ id: "c2", reason: "撤回会影响别的工作", impact: "它是某条关系的一端" }],
      reasonText: "任务失败，已撤回 1 项",
    },
  } as never;
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  backend = installFakeBackend((req) => {
    if (req.method === "POST" && req.url.includes("/demo/advance")) {
      return {
        status: 200,
        body: {
          ok: true,
          intent: { ...(failedIntentWithPending() as Record<string, unknown>), revert: { reverted: ["卡片 c1", "卡片 c2"], kept: [], pendingDecision: [], reasonText: "已按你的决定撤回其余部分" } },
          revert: { reverted: ["卡片 c1", "卡片 c2"], kept: [], pendingDecision: [], reasonText: "已按你的决定撤回其余部分" },
        },
      };
    }
    if (req.method === "GET" && req.url.endsWith("/intents")) {
      return { status: 200, body: { intents: [failedIntentWithPending()], conflicts: [], batchAvailable: false, recovery: { paused: [] } } };
    }
    if (req.method === "GET" && req.url.endsWith("/visible-range")) {
      return { status: 200, body: { visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 } } };
    }
    if (req.method === "GET" && req.url.endsWith("/state")) {
      return {
        status: 200,
        body: {
          board: { id: "board_default", title: "默认板面" },
          state: { boardId: "board_default", seq: 3, updatedAt: "t", cards: [], groups: [], links: [], selection: [] },
          seq: 3,
          baseline: null,
          submissions: [],
          drafts: { drafts: {}, updatedAt: "t", rev: 1 },
        },
      };
    }
    return undefined;
  });
});

function mountRevertDialog(): VueWrapper {
  const store = useInteractiveStore();
  store.intents = [failedIntentWithPending()];
  wrapper = mount(ImpactConfirmDialog, { attachTo: document.body });
  return wrapper;
}

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  backend?.restore();
  backend = null;
  vi.restoreAllMocks();
});

describe("N3 剩余撤回弹窗", () => {
  it("「继续」必须真的发出带 decisionIds 的撤回执行请求", async () => {
    const w = mountRevertDialog();
    await w.vm.$nextTick();
    expect(w.find('[data-im="impact-dialog"]').attributes("data-impact-mode")).toBe("revert");

    await w.find('[data-im="impact-continue"]').trigger("click");
    await flushPromises();

    const calls = backend!.requests.filter((r) => r.method === "POST" && r.url.includes("/demo/advance"));
    expect(calls.length, "「继续」没有发出任何撤回执行请求（N3）").toBeGreaterThan(0);
    const call = calls.find((c) => c.url.includes("/intents/i9/"))!;
    expect(call, "撤回执行请求没有指向这条任务").toBeTruthy();
    expect(call.body?.outcome, "撤回执行请求的结果类型不对").toBe("revert_rest");
    expect(
      call.body?.decisionIds,
      "撤回执行请求必须只带这次明确展示给用户的决定项（N3：decisionIds）",
    ).toEqual(["c2"]);
  });

  it("「取消」只结束提示：不执行撤回，并且真的能关掉提示", async () => {
    const w = mountRevertDialog();
    await w.vm.$nextTick();
    await w.find('[data-im="impact-cancel"]').trigger("click");
    await flushPromises();
    await w.vm.$nextTick();

    expect(
      backend!.requests.filter((r) => r.method === "POST" && r.url.includes("/demo/advance")).length,
      "「取消」也执行了撤回",
    ).toBe(0);
    expect(w.find('[data-im="impact-dialog"]').exists(), "「取消」关不掉提示（N3）").toBe(false);
  });

  it("Escape 与「取消」同义：关掉提示、不执行撤回", async () => {
    const w = mountRevertDialog();
    await w.vm.$nextTick();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await flushPromises();
    await w.vm.$nextTick();

    expect(
      backend!.requests.filter((r) => r.method === "POST" && r.url.includes("/demo/advance")).length,
    ).toBe(0);
    expect(w.find('[data-im="impact-dialog"]').exists(), "Escape 关不掉提示（N3）").toBe(false);
  });
});
