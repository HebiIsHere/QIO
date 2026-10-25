/**
 * D 组独立验收 · F03：任务与批量入口（页面级）。
 *
 * 用户可见契约（任务书 F03）：
 * - 顶部「任务」入口必须有真实承接界面：列出需要管理的任务，运行中任务可暂停 / 取消，
 *   已暂停任务可按当前材料继续；顶部入口的 aria-expanded 与面板开合一致；
 * - 批量操作入口必须接到真实批量界面（同批确认只提交显式 ids）；
 * - 聊天停止 ≠ 任务取消：任务面板里的控制只调用任务执行接口，不冒充聊天停止。
 *
 * 基线问题：tasksOpen / batchOpen 只是 store 里的布尔值，页面上没有任何承接界面。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { Intent } from "../../interactive/types";
import {
  mountInteractivePage,
  resetServices,
  settlePage,
  stubServices,
  type PageHarness,
} from "./acceptance-i2v2-page-harness";

const STAMP = "2026-10-25T00:00:00.000Z";

function intent(id: string, status: string, title: string): Intent {
  return {
    id,
    boardId: "board_t",
    submissionId: null,
    title,
    summary: "",
    status,
    preview: null,
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 1, total: 3, text: "第 2/3 步" },
    reason: "",
    demo: false,
    createdAt: STAMP,
    updatedAt: STAMP,
  } as unknown as Intent;
}

function withExecution(item: Intent, status: string): Intent {
  return {
    ...item,
    execution: {
      id: "exec_" + item.id,
      intentId: item.id,
      status,
      attempt: 1,
      steps: [{ op: "add_text_card", status: status === "done" ? "done" : "running" }],
      createdAt: STAMP,
      updatedAt: STAMP,
    },
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

async function mountWithTasks() {
  const services = stubServices();
  harness = mountInteractivePage();
  await settlePage(harness.wrapper);
  harness.store.intents = [
    withExecution(intent("i_run", "running", "整理发布材料"), "running"),
    withExecution(intent("i_pause", "paused", "核对实验记录"), "paused"),
  ];
  await settlePage(harness.wrapper);
  return services;
}

describe("F03 任务入口：有真实任务面板，控制调用真实执行接口", () => {
  it("点「任务」打开真实任务面板：列出运行中 / 已暂停任务，入口状态同步", async () => {
    await mountWithTasks();
    const entry = harness!.wrapper.get('[data-im="tasks-entry"]');
    expect(entry.attributes("aria-expanded")).toBe("false");
    await entry.trigger("click");
    await settlePage(harness!.wrapper);

    expect(harness!.wrapper.get('[data-im="tasks-entry"]').attributes("aria-expanded")).toBe("true");
    const text = harness!.wrapper.text();
    expect(text, "任务面板必须真的列出任务（基线只有空开关）").toContain("整理发布材料");
    expect(text).toContain("核对实验记录");
  });

  it("运行中任务：暂停与取消分别调用 pauseIntent / cancelIntent", async () => {
    const services = await mountWithTasks();
    await harness!.wrapper.get('[data-im="tasks-entry"]').trigger("click");
    await settlePage(harness!.wrapper);

    const pause = harness!.wrapper.find('[data-im="task-pause"]');
    expect(pause.exists(), "运行中任务必须有暂停入口").toBe(true);
    await pause.trigger("click");
    await settlePage(harness!.wrapper);
    expect(services.pauseIntent).toHaveBeenCalledWith("i_run");

    const cancel = harness!.wrapper
      .findAll('[data-im="task-cancel"]')
      .find((button) => button.attributes("data-intent-id") === "i_run");
    expect(cancel, "运行中任务必须有取消入口").toBeTruthy();
    await cancel!.trigger("click");
    await settlePage(harness!.wrapper);
    expect(services.cancelIntent).toHaveBeenCalledWith("i_run");
  });

  it("已暂停任务：继续按当前材料恢复，调用 resumeIntent 而不是聊天发送", async () => {
    const services = await mountWithTasks();
    await harness!.wrapper.get('[data-im="tasks-entry"]').trigger("click");
    await settlePage(harness!.wrapper);

    const resume = harness!.wrapper.find('[data-im="task-resume"]');
    expect(resume.exists(), "已暂停任务必须有继续入口").toBe(true);
    await resume.trigger("click");
    await settlePage(harness!.wrapper);
    expect(services.resumeIntent).toHaveBeenCalledWith("i_pause");
  });
});

describe("F03 批量入口：同批确认接到真实批量界面", () => {
  it("两条待确认意图：批量栏可用，确认只提交显式勾选的 ids", async () => {
    const services = stubServices();
    services.fetchIntentViews.mockResolvedValue({
      available: true,
      source: "service",
      intents: [
        { intent_id: "a", board_id: "board_t", version: 1, status: "proposed", title: "意图甲", authorization_valid: true, depends_on: [], conflicts_with: [], created_at: STAMP, updated_at: STAMP },
        { intent_id: "b", board_id: "board_t", version: 1, status: "proposed", title: "意图乙", authorization_valid: true, depends_on: [], conflicts_with: [], created_at: STAMP, updated_at: STAMP },
      ],
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
});
