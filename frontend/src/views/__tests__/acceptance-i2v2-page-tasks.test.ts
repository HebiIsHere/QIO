/**
 * D 组独立验收 · F03：任务与批量入口（页面级）。
 *
 * 用户可见契约（任务书 F03 / 流程 4）：
 * - 顶部「任务」入口必须有真实承接界面：列出需要管理的任务，运行中任务可暂停 / 取消，
 *   已暂停任务可按当前材料继续；顶部入口的 aria-expanded 与面板开合一致；
 * - 批量入口必须接到真实界面（集成后的实现：任务面板的待确认分区 + 意图层批量栏）；
 * - 聊天停止 ≠ 任务取消：任务控制只调用执行控制接口（pauseIntent / resumeIntent / cancelIntent）。
 *
 * 集成后校准：A 组把任务面板实现为 v2/TaskCenterPanel，内容来自 store.taskRows
 * （store.intentViews + store.intents + executionStatus 的唯一一份合并行），不再是旧 IntentStatusPopover。
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

function view(intentId: string, status: string, title: string): IntentViewWithDisplay {
  return {
    intent_id: intentId,
    board_id: "board_t",
    version: 1,
    status,
    title,
    goal: "目标",
    expected: "预期",
    scope: { read_card_ids: [], allowed_actions: [] },
    depends_on: [],
    conflicts_with: [],
    authorization_valid: true,
    operations_summary: { applied: 0, pending: 1 },
    created_at: STAMP,
    updated_at: STAMP,
  } as unknown as IntentViewWithDisplay;
}

function payload(intents: IntentViewWithDisplay[]) {
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

async function mountWithTasks() {
  const services = stubServices();
  services.fetchIntentViews.mockResolvedValue(
    payload([view("i_run", "running", "整理发布材料"), view("i_pause", "paused", "核对实验记录")]),
  );
  harness = mountInteractivePage();
  await settlePage(harness.wrapper);
  return services;
}

async function openTaskCenter() {
  const entry = harness!.wrapper.get('[data-im="tasks-entry"]');
  expect(entry.attributes("aria-expanded")).toBe("false");
  await entry.trigger("click");
  await settlePage(harness!.wrapper);
  return harness!.wrapper.get('[data-im="tasks-entry"]');
}

describe("F03 任务入口：有真实任务面板，控制调用真实执行接口", () => {
  it("点「任务」打开真实任务面板：列出运行中 / 已暂停任务，入口状态同步", async () => {
    await mountWithTasks();
    const entry = await openTaskCenter();
    expect(entry.attributes("aria-expanded")).toBe("true");
    const panel = harness!.wrapper.find('[data-im="task-center"]');
    expect(panel.exists(), "任务入口必须打开真实面板").toBe(true);
    const text = panel.text();
    expect(text).toContain("整理发布材料");
    expect(text).toContain("核对实验记录");
    expect(panel.find('[data-im="task-live"]').exists()).toBe(true);
    expect(panel.find('[data-im="task-paused"]').exists()).toBe(true);
  });

  it("运行中任务：暂停与取消分别调用 pauseIntent / cancelIntent", async () => {
    const services = await mountWithTasks();
    await openTaskCenter();

    const pause = harness!.wrapper.get('[data-im="task-pause"][data-intent-id="i_run"]');
    await pause.trigger("click");
    await settlePage(harness!.wrapper);
    expect(services.pauseIntent).toHaveBeenCalledWith("i_run");

    const cancel = harness!.wrapper.get('[data-im="task-cancel"][data-intent-id="i_run"]');
    await cancel.trigger("click");
    await settlePage(harness!.wrapper);
    expect(services.cancelIntent).toHaveBeenCalledWith("i_run");
  });

  it("已暂停任务：继续按当前材料恢复，调用 resumeIntent 而不是聊天发送", async () => {
    const services = await mountWithTasks();
    await openTaskCenter();

    const resume = harness!.wrapper.get('[data-im="task-resume"][data-intent-id="i_pause"]');
    expect(resume.exists(), "已暂停任务必须有继续入口").toBe(true);
    await resume.trigger("click");
    await settlePage(harness!.wrapper);
    expect(services.resumeIntent).toHaveBeenCalledWith("i_pause");
  });
});

describe("F03 批量入口：同批确认接到真实批量界面", () => {
  it("两条待确认意图：意图层批量栏可用，确认只提交显式勾选的 ids", async () => {
    const services = stubServices();
    services.fetchIntentViews.mockResolvedValue({
      available: true,
      source: "service",
      intents: [view("a", "proposed", "意图甲"), view("b", "proposed", "意图乙")],
      batch_confirm: { min: 2, eligible_ids: ["a", "b"], available: true },
    } as never);
    services.batchConfirmIntents.mockResolvedValue({
      batch: true,
      results: [
        { intent_id: "a", ok: true, status: "approved", turn_id: "turn_1" },
        { intent_id: "b", ok: true, status: "approved", turn_id: "turn_2" },
      ],
    } as never);

    harness = mountInteractivePage();
    await settlePage(harness.wrapper);
    expect(harness!.wrapper.find('[data-im="intent-batch-bar"]').exists()).toBe(true);
    for (const id of ["a", "b"]) {
      const card = harness!.wrapper.findAll('[data-im="virtual-intent"]').find((item) => item.attributes("data-intent-id") === id);
      await card!.find('[data-im="intent-check"]').setValue(true);
    }
    await settlePage(harness!.wrapper);
    await harness!.wrapper.get('[data-im="intent-batch-confirm"]').trigger("click");
    await settlePage(harness!.wrapper);
    expect(services.batchConfirmIntents).toHaveBeenCalledWith("board_t", ["a", "b"]);
  });

  it("任务面板同时承接批量待确认入口（同一份 taskRows）", async () => {
    const services = stubServices();
    services.fetchIntentViews.mockResolvedValue(
      payload([view("a", "proposed", "意图甲"), view("b", "proposed", "意图乙")]),
    );
    harness = mountInteractivePage();
    await settlePage(harness.wrapper);
    await openTaskCenter();
    expect(harness!.wrapper.find('[data-im="task-pending"]').exists()).toBe(true);
    expect(harness!.wrapper.findAll('[data-im="task-pending-row"]').length).toBeGreaterThanOrEqual(2);
  });
});
