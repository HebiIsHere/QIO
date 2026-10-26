/**
 * D 组独立验收 · F01：恢复材料影响确认流程（页面级）。
 *
 * 用户可见契约（任务书 F01）：
 * - 改动会碰到正在执行任务依赖的材料时，保存前必须在页面中央出现**真实的确认界面**，
 *   写明受影响的任务与材料；等待决定期间板面不保存；
 * - 「确认」= 保存并暂停相关任务，「取消」= 这次改动不生效、任务继续；
 * - 界面同一时刻只有一个阻塞确认面（不与统一审批层叠出第二个弹窗）。
 *
 * 这些用例按行为写，不按基线实现写：基线 InteractiveView 没有挂载任何影响确认界面，
 * 所以 store.pendingImpact 有值时页面上找不到可操作的确认框（应当红）。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { addCard, emptyState } from "../../interactive/board";
import type { Intent } from "../../interactive/types";
import {
  mountInteractivePage,
  resetServices,
  settlePage,
  stubServices,
  type PageHarness,
} from "./acceptance-i2v2-page-harness";

const STAMP = "2026-10-25T00:00:00.000Z";

function runningIntent(id = "i_run"): Intent {
  return {
    id,
    boardId: "board_t",
    submissionId: null,
    title: "整理发布材料",
    summary: "",
    status: "running",
    preview: null,
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: ["m1"],
    progress: { done: 0, total: 1, text: "" },
    reason: "",
    demo: false,
    createdAt: STAMP,
    updatedAt: STAMP,
  } as unknown as Intent;
}

let harness: PageHarness | null = null;

beforeEach(() => {
  resetServices();
});

afterEach(() => {
  try {
    harness?.wrapper.unmount();
  } catch {
    /* 已经卸载 */
  }
  harness = null;
  resetServices();
  document.body.innerHTML = "";
});

/** 造出「候选需要影响确认」的真实 store 状态，再触发保存。 */
async function openImpactViaSave(services: ReturnType<typeof stubServices>) {
  const board = addCard(emptyState("board_t"), { kind: "file", content: "发布材料", meta: { name: "a.pdf" } });
  const m1 = board.cards[0].id;
  services.checkMaterialImpact.mockResolvedValue({
    ok: true,
    checkId: "chk_1",
    stateVersion: 0,
    impactConfirmationRequired: true,
    affected: [
      {
        intentId: "i_run",
        title: "整理发布材料",
        status: "running",
        materials: ["a.pdf"],
        consequence: "这个任务依赖的材料被改动，继续保存会让它按旧材料执行。",
      },
    ],
  } as never);
  harness = mountInteractivePage();
  await settlePage(harness.wrapper);
  harness.store.intents = [runningIntent()];
  harness.store.board = { ...board, cards: [{ ...board.cards[0], content: "发布材料（改）" }] };
  await harness.store.saveNow();
  await settlePage(harness.wrapper);
  return { board, m1 };
}

describe("F01 影响确认界面：等待决定时页面中央有可操作的确认框", () => {
  it("保存前命中影响：出现确认框、写明受影响任务与材料；此时还没有落库", async () => {
    const services = stubServices();
    await openImpactViaSave(services);

    expect(harness!.store.pendingImpact, "预判命中后必须进入等待确认").not.toBeNull();
    // 校准（集成后真实实现）：A 组的影响确认面是 v2/ImpactConfirmLayer，钩子为 impact-confirm。
    const dialog = harness!.wrapper.find('[data-im="impact-confirm"]');
    expect(dialog.exists(), "页面上必须有真实的影响确认界面（基线没有挂载）").toBe(true);
    expect(dialog.attributes("role")).toBe("alertdialog");
    const text = dialog.text();
    expect(text).toContain("等待确认");
    expect(text).toContain("整理发布材料");
    expect(text).toContain("a.pdf");
    // 等待决定期间不许偷偷落库
    expect(services.saveBoardState).not.toHaveBeenCalled();
    // 同一时刻只有一个阻塞确认面
    expect(document.querySelectorAll('[role="alertdialog"]')).toHaveLength(1);
  });

  it("取消：改动不生效、任务继续，确认框关闭且没有发生保存", async () => {
    const services = stubServices();
    await openImpactViaSave(services);
    const cancel = harness!.wrapper.get('[data-im="impact-confirm-cancel"]');
    expect(cancel.text()).toContain("取消");
    await cancel.trigger("click");
    await settlePage(harness!.wrapper);

    expect(harness!.store.pendingImpact).toBeNull();
    // v-show：窗口 DOM 保留（状态可恢复），取消后不再显示
    expect(harness!.wrapper.find('[data-im="impact-confirm-scrim"]').attributes("style") ?? "").toContain("display: none");
    expect(services.saveBoardState).not.toHaveBeenCalled();
    expect(harness!.store.intents.find((item) => item.id === "i_run")?.status).toBe("running");
  });

  it("确认：保存这次改动；保存失败时如实显示失败结果，不宣称任务已暂停", async () => {
    const services = stubServices();
    services.saveBoardState.mockRejectedValue(new Error("网络断了"));
    await openImpactViaSave(services);

    const continueButton = harness!.wrapper.get('[data-im="impact-confirm-submit"]');
    expect(continueButton.text()).toMatch(/确认改动|继续/);
    await continueButton.trigger("click");
    await settlePage(harness!.wrapper);

    expect(services.saveBoardState, "确认后必须真的尝试保存（而不是只关框）").toHaveBeenCalled();
    expect(harness!.store.pendingImpact).toBeNull();
    const outcome = document.querySelector('[data-im="impact-confirm-outcome"]');
    expect(outcome, "保存失败必须显示真实结果，让用户知道改动没有落库").not.toBeNull();
    expect(outcome?.textContent ?? "").toContain("没有保存");
    expect(harness!.wrapper.text()).not.toContain("已保存生效");
  });
});
