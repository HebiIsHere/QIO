/**
 * 【独立验收 D · 第八轮】N2：重新确认后仍显示「已保存、任务已暂停」，把等待确认说成成功。
 *
 * 触发（契约 §2 / 本轮反例 N2）：
 *   确认任务 A 的说明 → 保存因范围增加任务 B 被拒 → 补取新说明等待再次确认。
 * 基线现状：确认框在 confirmImpact 正常返回后就宣布「已确认：改动已经保存生效；受影响的任务已暂停」，
 *   而真实结果是「仍待确认」——用户以为改动已经生效。
 * 正确行为：由真实结果区分成功 / 仍待确认 / 用户取消 / 检查失败 / 保存失败，只在真正生效时显示成功事实。
 *
 * 证据分层：②真实组件 DOM + 真实 store/服务层（传输为假后端）。
 * 本文件在 b3245e5 上必须失败（红）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import ImpactConfirmDialog from "../components/interactive/ImpactConfirmDialog.vue";
import { useInteractiveStore } from "../stores/interactive";
import { installFakeBackend, type FakeBackend } from "./support/fakeBackend";
import { card, state } from "./support/harness";

let backend: FakeBackend | null = null;
let wrapper: VueWrapper | null = null;

const TASK_A = {
  intentId: "A",
  title: "任务-A",
  materials: ["c1"],
  consequence: "暂停并保留进度",
};
const TASK_B = {
  intentId: "B",
  title: "任务-B（范围增加后新出现的运行中任务）",
  materials: ["c1"],
  consequence: "也会被这次改动暂停",
};

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  backend = installFakeBackend((req) => {
    if (req.method === "PUT" && req.url.endsWith("/state")) {
      return {
        status: 409,
        body: {
          detail: {
            error: "impact_confirmation_required",
            reason: "这次保存会修改执行中任务依赖的材料",
            affectedTasks: [TASK_B],
          },
        },
      };
    }
    if (req.method === "POST" && req.url.endsWith("/impact-check")) {
      return {
        status: 200,
        body: { ok: true, checkId: "chk_b", stateVersion: 3, affected: [TASK_B], impactConfirmationRequired: true },
      };
    }
    if (req.method === "GET" && req.url.endsWith("/visible-range")) {
      return {
        status: 200,
        body: { visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 } },
      };
    }
    if (req.method === "GET" && req.url.endsWith("/intents")) {
      return { status: 200, body: { intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } } };
    }
    return undefined;
  });
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  backend?.restore();
  backend = null;
  vi.restoreAllMocks();
});

describe("N2 重新确认仍待确认时，不许显示成功事实", () => {
  it("补取新说明后仍是「等待确认」，界面不得出现「已确认 / 已保存 / 已暂停」", async () => {
    const store = useInteractiveStore();
    store.board = state(3, [card("c1", "改动后的正文")]);
    // 用户对任务 A 的说明点过一次「继续」，服务端按真实范围重核后要求按 B 重新确认
    store.pendingImpact = { affected: [TASK_A], previewRev: 0, stateVersion: 3, checkId: "chk_a" };

    wrapper = mount(ImpactConfirmDialog, { attachTo: document.body });
    await wrapper.vm.$nextTick();
    await wrapper.find('[data-im="impact-continue"]').trigger("click");
    await flushPromises();
    await wrapper.vm.$nextTick();

    // 真实结果：仍然有待确认的影响说明（并没有生效）
    expect(store.pendingImpact, "服务端仍要求再次确认").not.toBeNull();
    const text = wrapper.text();
    expect(text, "界面必须继续显示待确认的新范围").toContain("任务-B");
    expect(
      /已确认|已保存|已暂停/.test(text),
      "确认返回后界面宣称「已确认 / 已保存 / 已暂停」，但真实结果仍是等待确认（N2）",
    ).toBe(false);
    expect(wrapper.find('[data-im="impact-dialog"]').exists()).toBe(true);
  });
});
