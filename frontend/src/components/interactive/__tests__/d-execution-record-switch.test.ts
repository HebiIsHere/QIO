/**
 * D 组定向用例（问题 8）：执行记录切换任务后，新标题下不能短暂显示旧结果。
 *
 * 覆盖：正常切换时立即清空旧详情并只显示新任务的读取状态；
 * A/B 返回次序倒置、快速连续切换（A→B→C）时迟到响应不能覆盖当前任务；
 * 读取失败时显示失败而不是旧内容/「没有结果」；面板关闭后迟到的返回不能重新填回。
 * 全部用受控 deferred Promise 控制返回顺序，不联网。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
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

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

interface Response {
  ledger: Deferred<{ available: boolean; operations: unknown[] }>;
  results: Deferred<{ available: boolean; results: unknown[] }>;
}

let calls: Response[] = [];

function intentOf(intentId: string, title: string): IntentView {
  return {
    intent_id: intentId,
    board_id: "board_1",
    version: 1,
    status: "failed",
    title,
  };
}

function resultOf(callIndex: number, intentId: string, title: string, content: string) {
  const call = calls[callIndex];
  if (!call) throw new Error("第 " + callIndex + " 次读取不存在");
  call.ledger.resolve({
    available: true,
    operations: [
      {
        operation_id: "op_" + intentId,
        intent_id: intentId,
        action: title + "的操作",
        status: "failed",
      },
    ],
  });
  call.results.resolve({
    available: true,
    results: [{ result_id: "res_" + intentId, intent_id: intentId, title, content }],
  });
}

function mountPanel(intent: IntentView) {
  return mount(ExecutionRecordPanel, {
    props: { intent, boardId: "board_1", open: true },
    attachTo: document.body,
  });
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("执行记录切换隔离", () => {
  it("从 A 切到 B 时立即清空 A 的操作与结果，只显示 B 的读取状态", async () => {
    calls = [];
    mocks.fetchLedger.mockReset();
    mocks.fetchIntentResults.mockReset();
    mocks.fetchLedger.mockImplementation(() => {
      const call = { ledger: deferred<{ available: boolean; operations: unknown[] }>(), results: deferred<{ available: boolean; results: unknown[] }>() };
      calls.push(call);
      return call.ledger.promise;
    });
    mocks.fetchIntentResults.mockImplementation(() => calls[calls.length - 1].results.promise);

    const intentA = intentOf("intent_a", "任务 A");
    const intentB = intentOf("intent_b", "任务 B");
    const wrapper = mountPanel(intentA);
    resultOf(0, "intent_a", "A 的结果", "A 的正文");
    await flushPromises();
    expect(wrapper.text()).toContain("任务 A");
    expect(wrapper.text()).toContain("A 的结果");

    // 切到 B：B 的读取还没回来。此刻标题已经是 B，内容必须已经清空。
    await wrapper.setProps({ intent: intentB });
    await flushPromises();
    expect(wrapper.text()).toContain("任务 B");
    expect(wrapper.text()).toContain("正在读取操作记录…");
    expect(wrapper.text()).not.toContain("A 的结果");
    expect(wrapper.text()).not.toContain("A 的正文");

    resultOf(1, "intent_b", "B 的结果", "B 的正文");
    await flushPromises();
    expect(wrapper.text()).toContain("B 的结果");
    expect(wrapper.text()).not.toContain("A 的结果");
    wrapper.unmount();
  });

  it("A/B 返回次序倒置：B 先返回，A 迟到也不能覆盖 B", async () => {
    calls = [];
    mocks.fetchLedger.mockReset();
    mocks.fetchIntentResults.mockReset();
    mocks.fetchLedger.mockImplementation(() => {
      const call = { ledger: deferred<{ available: boolean; operations: unknown[] }>(), results: deferred<{ available: boolean; results: unknown[] }>() };
      calls.push(call);
      return call.ledger.promise;
    });
    mocks.fetchIntentResults.mockImplementation(() => calls[calls.length - 1].results.promise);

    const intentA = intentOf("intent_a", "任务 A");
    const intentB = intentOf("intent_b", "任务 B");
    const wrapper = mountPanel(intentA);
    await wrapper.setProps({ intent: intentB });
    await flushPromises();

    resultOf(1, "intent_b", "B 的结果", "B 的正文");
    await flushPromises();
    expect(wrapper.text()).toContain("B 的结果");

    // A 的旧请求现在才返回：没有 open/当前 intent 校验的话会覆盖 B。
    resultOf(0, "intent_a", "A 的结果", "迟到的 A 正文");
    await flushPromises();
    expect(wrapper.text()).toContain("任务 B");
    expect(wrapper.text()).toContain("B 的结果");
    expect(wrapper.text()).not.toContain("A 的结果");
    expect(wrapper.text()).not.toContain("迟到的 A 正文");
    wrapper.unmount();
  });

  it("快速连续切换 A→B→C：只有 C 的内容能留下", async () => {
    calls = [];
    mocks.fetchLedger.mockReset();
    mocks.fetchIntentResults.mockReset();
    mocks.fetchLedger.mockImplementation(() => {
      const call = { ledger: deferred<{ available: boolean; operations: unknown[] }>(), results: deferred<{ available: boolean; results: unknown[] }>() };
      calls.push(call);
      return call.ledger.promise;
    });
    mocks.fetchIntentResults.mockImplementation(() => calls[calls.length - 1].results.promise);

    const wrapper = mountPanel(intentOf("intent_a", "任务 A"));
    await wrapper.setProps({ intent: intentOf("intent_b", "任务 B") });
    await wrapper.setProps({ intent: intentOf("intent_c", "任务 C") });
    await flushPromises();

    // B、A 的迟到返回
    resultOf(1, "intent_b", "B 的结果", "B 的正文");
    resultOf(0, "intent_a", "A 的结果", "A 的正文");
    await flushPromises();
    expect(wrapper.text()).not.toContain("B 的结果");
    expect(wrapper.text()).not.toContain("A 的结果");
    expect(wrapper.text()).toContain("任务 C");

    resultOf(2, "intent_c", "C 的结果", "C 的正文");
    await flushPromises();
    expect(wrapper.text()).toContain("C 的结果");
    wrapper.unmount();
  });

  it("读取失败：显示失败原因、不保留旧内容，结果区也不能说成「没有结果」", async () => {
    calls = [];
    mocks.fetchLedger.mockReset();
    mocks.fetchIntentResults.mockReset();
    mocks.fetchLedger.mockImplementation(() => {
      const call = { ledger: deferred<{ available: boolean; operations: unknown[] }>(), results: deferred<{ available: boolean; results: unknown[] }>() };
      calls.push(call);
      return call.ledger.promise;
    });
    mocks.fetchIntentResults.mockImplementation(() => calls[calls.length - 1].results.promise);

    const wrapper = mountPanel(intentOf("intent_a", "任务 A"));
    resultOf(0, "intent_a", "A 的结果", "A 的正文");
    await flushPromises();
    expect(wrapper.text()).toContain("A 的结果");

    await wrapper.setProps({ intent: intentOf("intent_b", "任务 B") });
    await flushPromises();
    // B 读取失败：错误与结果都以同一份读取为准
    calls[1].ledger.reject(new Error("账本读取中断"));
    calls[1].results.reject(new Error("结果读取中断"));
    await flushPromises();

    expect(wrapper.get('[data-im="execution-record-error"]').text()).toContain("读取失败");
    expect(wrapper.text()).toContain("账本读取中断");
    expect(wrapper.text()).not.toContain("A 的结果");
    expect(wrapper.find('[data-im="execution-record-results-error"]').exists()).toBe(true);
    expect(wrapper.text()).not.toContain("没有已保存的结果卡");
    wrapper.unmount();
  });

  it("面板关闭后迟到的返回不能重新填回内容；重开只显示新任务", async () => {
    calls = [];
    mocks.fetchLedger.mockReset();
    mocks.fetchIntentResults.mockReset();
    mocks.fetchLedger.mockImplementation(() => {
      const call = { ledger: deferred<{ available: boolean; operations: unknown[] }>(), results: deferred<{ available: boolean; results: unknown[] }>() };
      calls.push(call);
      return call.ledger.promise;
    });
    mocks.fetchIntentResults.mockImplementation(() => calls[calls.length - 1].results.promise);

    const wrapper = mountPanel(intentOf("intent_a", "任务 A"));
    await flashAll();
    await wrapper.setProps({ open: false });
    await flushPromises();
    expect(wrapper.find('[data-im="execution-record"]').exists()).toBe(false);

    // 关闭后 A 的请求才返回：不能把内容/错误写回面板，更不能让它重新打开。
    resultOf(0, "intent_a", "A 的结果", "关闭后迟到的正文");
    await flushPromises();
    expect(wrapper.find('[data-im="execution-record"]').exists()).toBe(false);
    expect(wrapper.text()).not.toContain("关闭后迟到的正文");

    // 重开并切到 B：先显示加载态，再显示 B 的内容。
    await wrapper.setProps({ open: true, intent: intentOf("intent_b", "任务 B") });
    await flushPromises();
    expect(wrapper.text()).toContain("正在读取操作记录…");
    expect(wrapper.text()).not.toContain("关闭后迟到的正文");
    resultOf(1, "intent_b", "B 的结果", "B 的正文");
    await flushPromises();
    expect(wrapper.text()).toContain("B 的结果");
    wrapper.unmount();
  });
});

/* --------------------------------------------------------------------------
 * #68：操作（账本）与成果（结果卡）分别加载 / 失败 / 重试
 *
 * 两侧各有一份独立的 loading / error / data / 重试：单边 503 不能把另一侧也清空，
 * 也不能把「读取失败」说成「没有数据」；重试只重读对应一侧。
 * ------------------------------------------------------------------------ */
interface LedgerResponse {
  available: boolean;
  operations: unknown[];
  reason?: string;
}

interface ResultsResponse {
  available: boolean;
  results: unknown[];
  reason?: string;
}

let ledgerReads: Deferred<LedgerResponse>[] = [];
let resultReads: Deferred<ResultsResponse>[] = [];

function resetReadMocks(): void {
  ledgerReads = [];
  resultReads = [];
  mocks.fetchLedger.mockReset();
  mocks.fetchIntentResults.mockReset();
  mocks.fetchLedger.mockImplementation(() => {
    const read = deferred<LedgerResponse>();
    ledgerReads.push(read);
    return read.promise;
  });
  mocks.fetchIntentResults.mockImplementation(() => {
    const read = deferred<ResultsResponse>();
    resultReads.push(read);
    return read.promise;
  });
}

function operationOf(intentId: string, suffix: string, status = "applied") {
  return {
    operation_id: "op_" + intentId + "_" + suffix,
    intent_id: intentId,
    operation_key: "key_" + suffix,
    action: "board_update_card",
    status,
  };
}

function resultCardOf(intentId: string, title: string, content: string) {
  return { result_id: "res_" + intentId + "_" + title, intent_id: intentId, title, content };
}

function mountPanelOn(intent: IntentView, boardId: string) {
  return mount(ExecutionRecordPanel, {
    props: { intent, boardId, open: true },
    attachTo: document.body,
  });
}

describe("执行记录：操作与成果分别读取（#68）", () => {
  it("操作读取失败（503）不影响成果：成果照常显示，操作区明确失败并可单独重试", async () => {
    resetReadMocks();
    const wrapper = mountPanel(intentOf("intent_a", "任务 A"));
    await flashAll();
    expect(ledgerReads).toHaveLength(1);
    expect(resultReads).toHaveLength(1);

    ledgerReads[0].reject(new Error("账本读取失败：503 Service Unavailable"));
    resultReads[0].resolve({
      available: true,
      results: [resultCardOf("intent_a", "A 的成果", "A 的正文")],
    });
    await flashAll();

    expect(wrapper.get('[data-im="execution-record-error"]').text()).toContain("读取失败");
    expect(wrapper.get('[data-im="execution-record-error"]').text()).toContain("503");
    expect(wrapper.find('[data-im="execution-record-retry"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="execution-record-results-error"]').exists()).toBe(false);
    expect(wrapper.text()).toContain("A 的成果");
    expect(wrapper.text()).toContain("A 的正文");
    expect(wrapper.text()).not.toContain("正在读取结果…");
    expect(wrapper.text()).not.toContain("没有已记录的操作");
    wrapper.unmount();
  });

  it("成果读取失败不影响操作：操作照常显示，成果区明确失败（不能当成没有结果）", async () => {
    resetReadMocks();
    const wrapper = mountPanel(intentOf("intent_a", "任务 A"));
    await flashAll();

    ledgerReads[0].resolve({
      available: true,
      operations: [operationOf("intent_a", "one")],
    });
    resultReads[0].reject(new Error("结果读取失败：500 Internal Server Error"));
    await flashAll();

    expect(wrapper.get('[data-im="execution-record-operations"]').text()).toContain("key_one");
    expect(wrapper.find('[data-im="execution-record-error"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="execution-record-results-retry"]').exists()).toBe(true);
    const resultsError = wrapper.get('[data-im="execution-record-results-error"]');
    expect(resultsError.text()).toContain("读取失败");
    expect(resultsError.text()).toContain("500");
    expect(wrapper.text()).not.toContain("没有已保存的结果卡");
    expect(wrapper.text()).not.toContain("正在读取结果…");
    wrapper.unmount();
  });

  it("两边都失败时两边都明确失败，不把失败说成「没有数据」", async () => {
    resetReadMocks();
    const wrapper = mountPanel(intentOf("intent_a", "任务 A"));
    await flashAll();

    ledgerReads[0].reject(new Error("账本读取中断"));
    resultReads[0].reject(new Error("结果读取中断"));
    await flashAll();

    expect(wrapper.get('[data-im="execution-record-error"]').text()).toContain("账本读取中断");
    expect(wrapper.get('[data-im="execution-record-results-error"]').text()).toContain("结果读取中断");
    expect(wrapper.text()).not.toContain("没有已记录的操作");
    expect(wrapper.text()).not.toContain("没有已保存的结果卡");
    wrapper.unmount();
  });

  it("重试只重试对应一侧：操作重试不重复读成果，成果重试不重复读操作", async () => {
    resetReadMocks();
    const wrapper = mountPanel(intentOf("intent_a", "任务 A"));
    await flashAll();
    ledgerReads[0].reject(new Error("账本 503"));
    resultReads[0].reject(new Error("结果 503"));
    await flashAll();

    // 只重试操作：成果保持自己的失败状态，不再发请求。
    await wrapper.get('[data-im="execution-record-retry"]').trigger("click");
    await flashAll();
    expect(ledgerReads).toHaveLength(2);
    expect(resultReads).toHaveLength(1);
    ledgerReads[1].resolve({ available: true, operations: [operationOf("intent_a", "retry")] });
    await flashAll();
    expect(wrapper.get('[data-im="execution-record-operations"]').text()).toContain("key_retry");
    expect(wrapper.find('[data-im="execution-record-results-error"]').exists()).toBe(true);

    // 只重试成果：操作侧不重复读取。
    await wrapper.get('[data-im="execution-record-results-retry"]').trigger("click");
    await flashAll();
    expect(resultReads).toHaveLength(2);
    expect(ledgerReads).toHaveLength(2);
    resultReads[1].resolve({
      available: true,
      results: [resultCardOf("intent_a", "重试后的成果", "重试后的正文")],
    });
    await flashAll();
    expect(wrapper.text()).toContain("重试后的成果");
    expect(wrapper.text()).not.toContain("结果读取失败");
    wrapper.unmount();
  });

  it("一侧返回先到就先显示该侧内容，不等待另一侧", async () => {
    resetReadMocks();
    const wrapper = mountPanel(intentOf("intent_a", "任务 A"));
    await flashAll();

    resultReads[0].resolve({
      available: true,
      results: [resultCardOf("intent_a", "先到的成果", "先到的正文")],
    });
    await flashAll();
    // 操作还在读：成果已经可见，操作区保持自己的读取状态。
    expect(wrapper.text()).toContain("先到的成果");
    expect(wrapper.get('[data-im="execution-record-loading"]').exists()).toBe(true);

    ledgerReads[0].resolve({ available: true, operations: [operationOf("intent_a", "later")] });
    await flashAll();
    expect(wrapper.get('[data-im="execution-record-operations"]').text()).toContain("key_later");
    expect(wrapper.text()).toContain("先到的成果");
    wrapper.unmount();
  });

  it("账本不可用（available=false）如实说明不可用，不冒充「没有记录」", async () => {
    resetReadMocks();
    const wrapper = mountPanel(intentOf("intent_a", "任务 A"));
    await flashAll();

    ledgerReads[0].resolve({
      available: false,
      operations: [],
      reason: "账本数据暂不可用",
    });
    resultReads[0].resolve({
      available: true,
      results: [resultCardOf("intent_a", "正常的成果", "正文")],
    });
    await flashAll();

    expect(wrapper.find('[data-im="execution-record-unavailable"]').exists()).toBe(true);
    expect(wrapper.get('[data-im="execution-record-unavailable"]').text()).toContain("账本数据暂不可用");
    expect(wrapper.text()).not.toContain("没有已记录的操作");
    expect(wrapper.text()).toContain("正常的成果");
    wrapper.unmount();
  });
});

describe("执行记录：记录身份（intentId + boardId + open + 代次）", () => {
  it("boardId 参与校验：切板面后旧板面的迟到响应不得写进新记录", async () => {
    resetReadMocks();
    const wrapper = mountPanelOn(intentOf("intent_a", "任务 A"), "board_1");
    await flashAll();
    expect(ledgerReads).toHaveLength(1);

    await wrapper.setProps({ boardId: "board_2" });
    await flashAll();
    expect(ledgerReads).toHaveLength(2);
    expect(resultReads).toHaveLength(2);

    // 新板面先返回，旧板面后到（迟到的旧响应必须被丢弃）。
    ledgerReads[1].resolve({ available: true, operations: [operationOf("intent_a", "newboard")] });
    resultReads[1].resolve({
      available: true,
      results: [resultCardOf("intent_a", "新板面成果", "新板面正文")],
    });
    await flashAll();
    expect(wrapper.text()).toContain("key_newboard");
    expect(wrapper.text()).toContain("新板面成果");

    ledgerReads[0].resolve({ available: true, operations: [operationOf("intent_a", "oldboard")] });
    resultReads[0].resolve({
      available: true,
      results: [resultCardOf("intent_a", "旧板面成果", "旧板面正文")],
    });
    await flashAll();
    expect(wrapper.text()).not.toContain("key_oldboard");
    expect(wrapper.text()).not.toContain("旧板面成果");
    expect(wrapper.text()).not.toContain("旧板面正文");
    expect(wrapper.text()).toContain("key_newboard");
    expect(wrapper.text()).toContain("新板面成果");
    wrapper.unmount();
  });

  it("快速切换 A→B：B 的成果先到可以显示，A 的迟到响应只按自己的侧丢弃", async () => {
    resetReadMocks();
    const wrapper = mountPanel(intentOf("intent_a", "任务 A"));
    await flashAll();
    await wrapper.setProps({ intent: intentOf("intent_b", "任务 B") });
    await flashAll();
    expect(ledgerReads).toHaveLength(2);
    expect(resultReads).toHaveLength(2);

    // B 的成果先到：成果区可先显示，操作区仍在读 B 的账本。
    resultReads[1].resolve({
      available: true,
      results: [resultCardOf("intent_b", "B 的成果", "B 的正文")],
    });
    await flashAll();
    expect(wrapper.text()).toContain("B 的成果");
    expect(wrapper.get('[data-im="execution-record-loading"]').exists()).toBe(true);

    // A 两侧迟到，都不能覆盖 B。
    ledgerReads[0].resolve({ available: true, operations: [operationOf("intent_a", "late")] });
    resultReads[0].resolve({
      available: true,
      results: [resultCardOf("intent_a", "A 的迟到成果", "A 的迟到正文")],
    });
    await flashAll();
    expect(wrapper.text()).toContain("B 的成果");
    expect(wrapper.text()).not.toContain("A 的迟到成果");
    expect(wrapper.text()).not.toContain("key_late");

    ledgerReads[1].resolve({ available: true, operations: [operationOf("intent_b", "final")] });
    await flashAll();
    expect(wrapper.get('[data-im="execution-record-operations"]').text()).toContain("key_final");
    expect(wrapper.text()).not.toContain("A 的迟到成果");
    wrapper.unmount();
  });

  it("关闭后重开同一意图：关闭期间的迟到响应不能填回，重开只显示新的读取状态", async () => {
    resetReadMocks();
    const wrapper = mountPanel(intentOf("intent_a", "任务 A"));
    await flashAll();
    expect(ledgerReads).toHaveLength(1);

    await wrapper.setProps({ open: false });
    await flushPromises();
    expect(wrapper.find('[data-im="execution-record"]').exists()).toBe(false);

    await wrapper.setProps({ open: true });
    await flashAll();
    expect(ledgerReads).toHaveLength(2);
    expect(resultReads).toHaveLength(2);
    expect(wrapper.text()).toContain("正在读取操作记录…");

    // 关闭前那一轮读取现在才返回：不能填进重开后的面板。
    ledgerReads[0].resolve({ available: true, operations: [operationOf("intent_a", "stale")] });
    resultReads[0].resolve({
      available: true,
      results: [resultCardOf("intent_a", "关闭期间的迟到成果", "迟到正文")],
    });
    await flashAll();
    expect(wrapper.text()).not.toContain("key_stale");
    expect(wrapper.text()).not.toContain("关闭期间的迟到成果");

    ledgerReads[1].resolve({ available: true, operations: [operationOf("intent_a", "fresh")] });
    resultReads[1].resolve({
      available: true,
      results: [resultCardOf("intent_a", "重开后的成果", "重开的正文")],
    });
    await flashAll();
    expect(wrapper.text()).toContain("key_fresh");
    expect(wrapper.text()).toContain("重开后的成果");
    wrapper.unmount();
  });
});

async function flashAll(): Promise<void> {
  await flushPromises();
  await flushPromises();
}