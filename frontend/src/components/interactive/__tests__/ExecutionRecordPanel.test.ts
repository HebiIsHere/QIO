import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ExecutionRecordPanel from "../ExecutionRecordPanel.vue";
import type { IntentView } from "../../../interactive/intentWorkbench";

const mocks = vi.hoisted(() => ({
  fetchLedger: vi.fn(),
  fetchIntentResults: vi.fn(),
}));
vi.mock("../../../services/interactive", () => ({
  fetchLedger: mocks.fetchLedger,
  fetchIntentResults: mocks.fetchIntentResults,
}));

const intent: IntentView = {
  intent_id: "intent_1",
  board_id: "board_1",
  version: 2,
  status: "failed",
  title: "整理方案对比",
  goal: "把两份方案放到组里",
  expected: "保留原始材料",
  basis: [{ card_id: "card_a", version: 3 }],
  scope: {
    read_card_ids: ["card_a"],
    write_card_ids: ["card_a", "card_b"],
    group_ids: ["group_1"],
    allowed_actions: ["board_update_card"],
  },
  outcome: "部分完成：card_a 已更新",
  retry: {
    series_id: "series_1",
    intent_id: "intent_1",
    status: "stopped",
    attempts: 1,
    limit: 3,
    last_error: "timeout",
    next_action: "manual",
  },
  updated_at: "2026-10-24T00:00:00.000Z",
};

beforeEach(() => {
  mocks.fetchLedger.mockReset();
  mocks.fetchIntentResults.mockReset();
  mocks.fetchLedger.mockResolvedValue({
    available: true,
    operations: [
      { operation_id: "op_1", action: "board_update_card", status: "failed", operation_key: "key_1" },
    ],
  });
  mocks.fetchIntentResults.mockResolvedValue({
    available: true,
    results: [{ result_id: "res_1", title: "对比结果", content: "正文" }],
  });
});

describe("执行记录", () => {
  it("展示确认材料、授权范围、真实操作、结果和失败/重试事实", async () => {
    const w = mount(ExecutionRecordPanel, {
      props: { intent, boardId: "board_1", open: true },
      attachTo: document.body,
    });
    await flushPromises();
    expect(w.find('[data-im="execution-record"]').exists()).toBe(true);
    expect(w.text()).toContain("整理方案对比");
    expect(w.text()).toContain("card_a");
    expect(w.text()).toContain("board_update_card");
    expect(w.text()).toContain("对比结果");
    expect(w.text()).toContain("timeout");
    expect(w.text()).toContain("1 / 3");
    expect(mocks.fetchLedger).toHaveBeenCalledWith("board_1");
    expect(mocks.fetchIntentResults).toHaveBeenCalledWith("board_1");
  });
});
