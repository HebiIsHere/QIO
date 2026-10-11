/**
 * 提交区默认失败信息（子智能体 C 负责，契约 §11.6 / §11.8）。
 *
 * 上一轮的缺陷：默认失败区读 `lastSubmission`（上一次提交结果），本次原因藏在详情里；
 * 而且板面保存也失败时仍然写着「已保存的板面都保留」。
 *
 * 这里挂真实组件，检查默认（不展开详情）就能看到：**本次**原因、保留情况、可用重试，
 * 顺序稳定（原因 → 保留 → 重试），长原因完整保留在 DOM 里、按钮不被挤掉。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createPinia, setActivePinia } from "pinia";
import SubmitCluster from "../SubmitCluster.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import * as board from "../../../interactive/board";
import type { SubmissionResult } from "../../../interactive/types";

vi.mock("../../services/interactive", () => ({
  fetchBoardState: vi.fn(async () => ({
    boardId: "board_t",
    seq: 1,
    updatedAt: "2026-10-08T00:00:00.000Z",
    cards: [],
    groups: [],
    links: [],
    selection: [],
    pending: { expressions: [] },
  })),
}));

let wrapper: VueWrapper | null = null;

function staleResult(): SubmissionResult {
  return {
    status: "failed",
    expressions: [],
    checkedCleared: [],
    submission: { id: "s_old", status: "failed", error: "上一次的旧原因" },
    delivery: { delivered: false, reason: "" },
  } as unknown as SubmissionResult;
}

function mountCluster(): VueWrapper {
  const store = useInteractiveStore();
  store.board = board.emptyState("board_t");
  wrapper = mount(SubmitCluster);
  return wrapper;
}

beforeEach(() => {
  setActivePinia(createPinia());
});

describe("默认失败区显示本次请求的真实原因", () => {
  it("本次原因可见，上一次提交的旧原因不在默认区里", () => {
    const store = useInteractiveStore();
    store.submitStatus = "failed";
    store.submitError = "网络中断，本次请求没有发出去";
    store.lastSubmission = staleResult();
    store.saveStatus = "saved";
    const w = mountCluster();
    // 没有展开详情，默认区就已经给出本次原因
    expect(w.find('[data-im="submit-details-box"]').exists()).toBe(false);
    const failure = w.find('[data-im="submit-failure"]');
    expect(failure.exists()).toBe(true);
    expect(failure.text()).toContain("网络中断，本次请求没有发出去");
    expect(failure.text()).not.toContain("上一次的旧原因");
    expect(failure.text()).toContain("保留");
  });

  it("服务端返回失败结果（submitError 为空）时，默认区取这份结果的真实原因", () => {
    const store = useInteractiveStore();
    store.submitStatus = "failed";
    store.submitError = null;
    store.lastSubmission = {
      ...staleResult(),
      submission: { id: "s_now", status: "failed", error: "服务端拒绝：没有可提交的内容" },
    } as SubmissionResult;
    const w = mountCluster();
    expect(w.find('[data-im="submit-failure"]').text()).toContain("服务端拒绝");
  });

  it("成功状态不显示失败区（上一次的失败不冒充本次结果）", () => {
    const store = useInteractiveStore();
    store.submitStatus = "succeeded";
    store.submitError = null;
    store.lastSubmission = staleResult();
    const w = mountCluster();
    expect(w.find('[data-im="submit-failure"]').exists()).toBe(false);
  });
});

describe("保留情况与重试", () => {
  it("原因 → 保留 → 重试顺序稳定，重试按钮可用", () => {
    const store = useInteractiveStore();
    store.submitStatus = "failed";
    store.submitError = "网络中断";
    store.saveStatus = "saved";
    const w = mountCluster();
    const reason = w.find('[data-im="submit-failure-reason"]').element;
    const retention = w.find('[data-im="submit-failure-retention"]').element;
    const submit = w.find('[data-im="submit"]').element;
    expect(reason.compareDocumentPosition(retention) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(retention.compareDocumentPosition(submit) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const button = w.find('[data-im="submit"]');
    expect(button.attributes("disabled")).toBeUndefined();
    expect(button.text()).toContain("重新提交");
    expect(w.find('[data-im="submit-failure"]').attributes("role")).toBe("alert");
  });

  it("板面保存也失败：默认区不许声称全部修改已经保存", () => {
    const store = useInteractiveStore();
    store.submitStatus = "failed";
    store.submitError = "板面没有保存成功，本次未提交（磁盘写满）";
    store.saveStatus = "error";
    store.saveError = "磁盘写满";
    const w = mountCluster();
    const failure = w.find('[data-im="submit-failure"]').text();
    expect(failure).toContain("磁盘写满");
    expect(failure).not.toContain("已保存的板面");
    expect(failure).toMatch(/没有保存|还没保存|没有存到服务器/);
  });

  it("详情里保留较长诊断（保存失败原因与上一次的说明），但不是了解原因的必经入口", async () => {
    const store = useInteractiveStore();
    store.submitStatus = "failed";
    store.submitError = "网络中断";
    store.saveStatus = "saved";
    store.lastSubmission = staleResult();
    const w = mountCluster();
    expect(w.find('[data-im="submit-failure-detail"]').exists()).toBe(false);
    await w.find('[data-im="submit-details"]').trigger("click");
    const detail = w.find('[data-im="submit-failure-detail"]');
    expect(detail.exists()).toBe(true);
    expect(detail.text()).toContain("上一次的旧原因");
  });

  it("长原因完整保留在 DOM 里，按钮还在、还可以点（不靠截断文字）", () => {
    const store = useInteractiveStore();
    const long = "网络中断：" + "这是一段很长的失败原因说明。".repeat(30);
    store.submitStatus = "failed";
    store.submitError = long;
    store.saveStatus = "saved";
    const w = mountCluster();
    expect(w.find('[data-im="submit-failure-reason"]').text()).toContain(long);
    expect(w.find('[data-im="submit-failure"]').text().length).toBeGreaterThan(long.length);
    const button = w.find('[data-im="submit"]');
    expect(button.attributes("disabled")).toBeUndefined();
    expect(button.text()).toContain("重新提交");
  });

  it("失败区换行、不挤坏按钮：样式走令牌且有高度上限（扫源码）", () => {
    const source = readFileSync(resolve(process.cwd(), "src/components/interactive/SubmitCluster.vue"), "utf8");
    expect(source).toMatch(/\.failure\s*\{[^}]*overflow-wrap:\s*anywhere/);
    expect(source).toMatch(/\.failure\s*\{[^}]*max-height:/);
    expect(source).toMatch(/\.failure\s*\{[^}]*background:\s*var\(--danger-soft\)/);
    expect(source).toMatch(/\.actions\s*\{[^}]*flex:\s*none/);
    expect(source).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
  });
});
