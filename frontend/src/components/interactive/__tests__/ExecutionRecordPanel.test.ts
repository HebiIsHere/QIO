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

/* --------------------------------------------------------------------------
 * #69：用户可见处只放可读名称；原始 ID / 内部枚举 / 结构数据只进「诊断」。
 * ------------------------------------------------------------------------ */
function readableIntent(overrides: Partial<IntentView> = {}): IntentView {
  return {
    ...intent,
    basis: [{ card_id: "card_a", version: 3, role: "source" }],
    scope: {
      read_card_ids: ["card_a"],
      write_card_ids: ["card_a", "card_b"],
      group_ids: ["group_1"],
      allowed_actions: ["board_update_card"],
    },
    authorization: {
      version: 2,
      confirmed_by: "user",
      confirmed_at: "2026-10-24T00:00:00.000Z",
      source: "explicit_user_confirmation",
      scope: { read_card_ids: ["card_a"] },
      nested_extra: { secret_shape: true },
    },
    ...overrides,
  };
}

function mountReadable(overrides: Partial<IntentView> = {}) {
  return mount(ExecutionRecordPanel, {
    props: { intent: readableIntent(overrides), boardId: "board_1", open: true },
    attachTo: document.body,
  });
}

describe("执行记录可读呈现（#69）", () => {
  it("实际操作与状态用中文名称，原始枚举只进诊断", async () => {
    const w = mountReadable();
    await flushPromises();
    const operations = w.get('[data-im="execution-record-operations"]').text();
    expect(operations).toContain("修改卡片");
    expect(operations).toContain("失败");
    expect(operations).not.toContain("board_update_card");
    expect(operations).not.toContain("failed");
    const diagnostics = w.get('[data-im="execution-record-diagnostics"]').text();
    expect(diagnostics).toContain("board_update_card");
    expect(diagnostics).toContain("failed");
    w.unmount();
  });

  it("授权范围给数量 + 可读名称摘要，原始 card_id / group_id / 枚举只进诊断", async () => {
    const w = mountReadable();
    await flushPromises();
    const scope = w.get('[data-im="execution-record-scope"]').text();
    expect(scope).toContain("可读卡片：1 张");
    expect(scope).toContain("可写卡片：2 张");
    expect(scope).toContain("分组：1 个");
    expect(scope).toContain("允许操作：1 类：修改卡片");
    expect(scope).not.toContain("card_a");
    expect(scope).not.toContain("group_1");
    expect(scope).not.toContain("board_update_card");
    const diagnostics = w.get('[data-im="execution-record-diagnostics"]').text();
    expect(diagnostics).toContain("card_a");
    expect(diagnostics).toContain("group_1");
    expect(diagnostics).toContain("board_update_card");
    w.unmount();
  });

  it("确认材料给数量 + 用途摘要，原始 card_id 只进诊断", async () => {
    const w = mountReadable();
    await flushPromises();
    const basis = w.get('[data-im="execution-record-basis"]').text();
    expect(basis).toContain("共 1 份材料");
    expect(basis).toContain("来源 1");
    expect(basis).not.toContain("card_a");
    expect(w.get('[data-im="execution-record-diagnostics"]').text()).toContain("card_a");
    w.unmount();
  });

  it("授权快照用可读字段；嵌套对象不会变成 [object Object]，原始结构只进诊断", async () => {
    const w = mountReadable();
    await flushPromises();
    const authorization = w.get('[data-im="execution-record-authorization"]').text();
    expect(authorization).toContain("版本 2");
    expect(authorization).toContain("用户");
    expect(authorization).toContain("用户显式确认");
    expect(authorization).not.toContain("[object Object]");
    expect(authorization).not.toContain("secret_shape");
    expect(w.text()).not.toContain("[object Object]");
    const diagnostics = w.get('[data-im="execution-record-diagnostics"]').text();
    expect(diagnostics).toContain("secret_shape");
    expect(diagnostics).toContain("explicit_user_confirmation");
    w.unmount();
  });

  it("结果 content 为对象时给可读标题 / 摘要，诊断保留原始 JSON", async () => {
    mocks.fetchIntentResults.mockResolvedValueOnce({
      available: true,
      results: [
        {
          result_id: "res_object",
          title: "对象结果",
          content: { title: "摘要标题", text: "正文内容" },
        },
      ],
    } as never);
    const w = mountReadable();
    await flushPromises();
    const results = w.get('[data-im="execution-record-results"]').text();
    expect(results).toContain("对象结果");
    expect(results).toContain("摘要标题：正文内容");
    expect(results).not.toContain("[object Object]");
    expect(w.get('[data-im="execution-record-diagnostics"]').text()).toContain("摘要标题");
    w.unmount();
  });

  it("诊断默认折叠、可展开，并保留原始标识与结构", async () => {
    const w = mountReadable();
    await flushPromises();
    const diagnostics = w.get('[data-im="execution-record-diagnostics"]');
    expect(diagnostics.element.tagName.toLowerCase()).toBe("details");
    expect(diagnostics.attributes("open")).toBeUndefined();
    expect(diagnostics.find("summary").text()).toContain("诊断");
    expect(diagnostics.text()).toContain("intent_1");
    expect(diagnostics.text()).toContain("series_1");
    w.unmount();
  });

  it("未登记的操作 / 状态给纯可读回退，不把内部枚举放进主文本", async () => {
    mocks.fetchLedger.mockResolvedValueOnce({
      available: true,
      operations: [
        {
          operation_id: "op_unknown",
          intent_id: "intent_1",
          action: "legacy_weird",
          status: "weird_status",
        },
      ],
    });
    const w = mountReadable();
    await flushPromises();
    const operations = w.get('[data-im="execution-record-operations"]').text();
    expect(operations).toContain("其他操作");
    expect(operations).toContain("状态未记录");
    expect(operations).not.toContain("legacy_weird");
    expect(operations).not.toContain("weird_status");
    const diagnostics = w.get('[data-im="execution-record-diagnostics"]').text();
    expect(diagnostics).toContain("legacy_weird");
    expect(diagnostics).toContain("weird_status");
    w.unmount();
  });

  it("意图状态与重试状态用可读名称，原始状态只进诊断", async () => {
    const w = mountReadable({ status: "weird_intent_status" });
    await flushPromises();
    const status = w.get('[data-im="execution-record-status"]').text();
    expect(status).not.toContain("weird_intent_status");
    expect(status).toContain("状态未记录");
    expect(w.get('[data-im="execution-record-retry-facts"]').text()).toContain("重试已停止");
    expect(w.get('[data-im="execution-record-retry-facts"]').text()).not.toContain("stopped");
    expect(w.get('[data-im="execution-record-diagnostics"]').text()).toContain("weird_intent_status");
    w.unmount();
  });
});