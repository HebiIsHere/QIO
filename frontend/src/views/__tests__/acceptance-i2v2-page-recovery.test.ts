/**
 * D 组独立验收 · F05：恢复事项入口（页面级）。
 *
 * 用户可见契约（任务书 F05）：
 * - 恢复入口的计数是**页面级事实**，不依赖恢复面板被打开、也不依赖对话展开：
 *   有服务端可恢复意图时，入口就应该出现并给出数量；
 * - 点开入口时，对话收起也要能读到真实事项（不是「服务端没有可恢复的意图」这种伪空态）；
 * - 读取失败不伪装成空：保留最后一次已知事项与入口，并说明读取失败；
 * - 事项处理完后数量会减少（不是只增不减）。
 *
 * 基线问题：页面上的 recoveryPanelCount 只由 RecoveryPanel 的 updated 事件喂，
 * 而面板只有对话展开时才 active；computed 还取 max()，所以数量只增不减、脱离面板就没有事实。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { IntentViewWithDisplay } from "../../components/interactive/v2/intentDisplay";
import {
  mountInteractivePage,
  resetServices,
  settlePage,
  stubServices,
  type PageHarness,
} from "./acceptance-i2v2-page-harness";

const STAMP = "2026-10-25T00:00:00.000Z";

function recoverableView(id = "i_failed", title = "上次失败的整理任务"): IntentViewWithDisplay {
  return {
    intent_id: id,
    board_id: "board_t",
    version: 3,
    status: "failed",
    title,
    goal: "整理材料",
    expected: "清单",
    scope: { read_card_ids: [], allowed_actions: [] },
    depends_on: [],
    conflicts_with: [],
    authorization_valid: true,
    operations_summary: { applied: 1, failed: 1, pending: 0 },
    created_at: STAMP,
    updated_at: STAMP,
  } as unknown as IntentViewWithDisplay;
}

function viewsPayload(intents: IntentViewWithDisplay[]) {
  return {
    available: true,
    source: "service",
    intents,
    batch_confirm: { min: 2, eligible_ids: [], available: true },
  } as never;
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

function recoveryEntry() {
  return harness!.wrapper.findAll('[data-entry="recovery"]')[0];
}

describe("F05 恢复入口：独立于面板与对话，读取失败不伪装空", () => {
  it("对话收起、从未打开恢复面板：服务端有可恢复意图时入口已出现并显示数量", async () => {
    const services = stubServices();
    services.fetchIntentViews.mockResolvedValue(viewsPayload([recoverableView()]));
    harness = mountInteractivePage();
    await settlePage(harness.wrapper);

    expect(harness.store.chatOpen, "对话保持收起").toBe(false);
    const entry = recoveryEntry();
    expect(entry, "有真实恢复事项时入口必须出现（不能等到打开面板才知道）").toBeTruthy();
    expect(entry.attributes("aria-label") ?? "").toContain("1");
  });

  it("对话收起时点开恢复入口：仍能读到真实事项，而不是伪空态", async () => {
    const services = stubServices();
    services.fetchIntentViews.mockResolvedValue(viewsPayload([recoverableView()]));
    harness = mountInteractivePage();
    await settlePage(harness.wrapper);
    await recoveryEntry().trigger("click");
    await settlePage(harness.wrapper);

    expect(harness.store.chatOpen).toBe(false);
    const text = harness!.wrapper.text();
    expect(text).toContain("上次失败的整理任务");
    expect(text).not.toContain("服务端没有可恢复的意图");
  });

  it("读取恢复事项失败：入口保留已知道事项，并说明读取失败（不是伪装没有事项）", async () => {
    const services = stubServices();
    services.fetchIntentViews.mockResolvedValueOnce(viewsPayload([recoverableView()]));
    harness = mountInteractivePage();
    await settlePage(harness.wrapper);
    expect(recoveryEntry(), "先有一次成功的读取").toBeTruthy();

    // 触发页面重新读取（与提交后刷新同一条路径），这次服务端读取失败
    services.fetchIntentViews.mockRejectedValueOnce(new Error("网络断了"));
    harness.store.lastSubmission = { submission: { id: "sub_2" } } as never;
    await settlePage(harness.wrapper);
    await settlePage(harness.wrapper);
    const entry = recoveryEntry();
    expect(entry, "读取失败不能把入口一起清掉（那等于伪装成没有事项）").toBeTruthy();
    await entry.trigger("click");
    await settlePage(harness!.wrapper);

    const text = harness!.wrapper.text();
    expect(text).toContain("上次失败的整理任务");
    expect(text).toContain("失败");
    expect(text).not.toContain("服务端没有可恢复的意图");
  });

  it("草稿保存失败也属于恢复事项：入口必须出现（完整性，不只看板面保存）", async () => {
    stubServices();
    harness = mountInteractivePage();
    await settlePage(harness.wrapper);
    expect(recoveryEntry(), "没有事项时不该有入口").toBeFalsy();

    harness.store.draftStates = { "card:c1": { status: "error", error: "本机存储写不进去" } };
    await settlePage(harness.wrapper);
    expect(recoveryEntry(), "草稿保存失败必须计入恢复事项").toBeTruthy();
  });

  it("事项处理完：入口数量减少并消失（不是只增不减）", async () => {
    const services = stubServices();
    services.fetchIntentViews.mockResolvedValue(viewsPayload([recoverableView()]));
    harness = mountInteractivePage();
    await settlePage(harness.wrapper);
    expect(recoveryEntry()).toBeTruthy();

    // 用户处理完：服务端不再返回需要恢复的事项
    services.fetchIntentViews.mockResolvedValue(viewsPayload([]));
    harness.store.lastSubmission = { submission: { id: "sub_3" } } as never;
    await settlePage(harness.wrapper);
    expect(recoveryEntry(), "处理完的事项不能继续挂在入口上").toBeFalsy();
  });
});
