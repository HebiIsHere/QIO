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

async function flashAll(): Promise<void> {
  await flushPromises();
  await flushPromises();
}
