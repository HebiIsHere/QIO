/**
 * 提交失败默认区取「本次请求」的事实（子智能体 C 负责，契约 §11.6 / §11.8）。
 *
 * 上一轮的缺陷：默认失败区读的是 `lastSubmission`（上一次提交结果），
 * 本次请求的真实原因（store.submitError）藏在展开的详情里 —— 用户看到的是别人的旧原因。
 *
 * 这里用纯函数证明：
 * - 本次原因优先，且**不**把上一次提交的原因当作本次原因；
 * - 服务端直接返回失败结果时（submitError 为空），本次事实就在这份结果里，可以当原因用；
 * - 内容保留情况单独、准确地说：板面保存也失败时不许声称「已保存」；
 * - 文案里不出现实现说明（接口 / adapter / 求差 / 状态机 / DOM）。
 */
import { describe, expect, it } from "vitest";
import { submitFailureBrief, submitFailureText } from "../submission";
import type { SubmissionResult } from "../types";

function result(fields: Partial<SubmissionResult> & { error?: string }): SubmissionResult {
  const { error, ...rest } = fields;
  return {
    status: "failed",
    expressions: [],
    checkedCleared: [],
    submission: { id: "s1", status: "failed", error },
    delivery: { delivered: false, reason: "" },
    ...rest,
  } as unknown as SubmissionResult;
}

const STALE = result({ error: "上一次的旧原因" });

describe("失败原因取本次请求的事实", () => {
  it("只有 failed 状态才给失败说明", () => {
    expect(submitFailureBrief({ status: "idle" })).toBeNull();
    expect(submitFailureBrief({ status: "submitting" })).toBeNull();
    expect(submitFailureBrief({ status: "succeeded", lastResult: STALE })).toBeNull();
    expect(submitFailureBrief({ status: "duplicate" })).toBeNull();
  });

  it("本次原因优先：不把上一次提交结果里的原因当成本次原因", () => {
    const brief = submitFailureBrief({
      status: "failed",
      error: "网络中断",
      lastResult: STALE,
      saveStatus: "saved",
    });
    expect(brief).toBeTruthy();
    expect(brief!.reason).toContain("网络中断");
    expect(brief!.reason).not.toContain("上一次的旧原因");
    // 旧原因只作为「上一次」的排查信息出现在较长诊断里，并明确标注来源
    expect(brief!.detail).toContain("上一次");
    expect(brief!.detail).toContain("上一次的旧原因");
  });

  it("服务端返回失败结果时，本次事实就在这份结果里（submitError 为空）", () => {
    const brief = submitFailureBrief({
      status: "failed",
      error: null,
      lastResult: result({ error: "服务端拒绝：没有可提交的内容" }),
      saveStatus: "saved",
    });
    expect(brief!.reason).toContain("服务端拒绝");
    // 它就是本次的原因，不能再标注成「上一次提交返回的说明」
    expect(brief!.detail).not.toContain("上一次提交返回的说明");
  });

  it("拿不到任何原因时如实说明，不编造、也不说成上一次的原因", () => {
    const brief = submitFailureBrief({ status: "failed", error: "  ", lastResult: null });
    expect(brief!.reason.length).toBeGreaterThan(0);
    expect(brief!.reason).not.toContain("上一次");
  });
});

describe("内容保留情况单独准确说明", () => {
  it("板面已保存：说清保留了什么、基准没有更新", () => {
    const brief = submitFailureBrief({ status: "failed", error: "网络中断", saveStatus: "saved" });
    expect(brief!.retention).toContain("保留");
    expect(brief!.retention).toContain("基准没有更新");
    expect(brief!.retention).toContain("已保存的板面");
  });

  it("板面保存也失败：不许声称全部修改已经保存", () => {
    const brief = submitFailureBrief({
      status: "failed",
      error: "板面没有保存成功，本次未提交（磁盘写满）",
      saveStatus: "error",
      saveError: "磁盘写满",
    });
    expect(brief!.retention).not.toContain("已保存的板面");
    expect(brief!.retention).toMatch(/没有保存|还没保存|没有存到服务器/);
    // 也不许把它写成「关闭后一定能恢复」
    expect(brief!.retention).not.toMatch(/一定能恢复|关闭后也能恢复/);
    expect(brief!.detail).toContain("磁盘写满");
  });

  it("还有改动没保存完时，也不说成已保存", () => {
    for (const context of [
      { status: "failed" as const, error: "网络中断", saveStatus: "saving" as const },
      { status: "failed" as const, error: "网络中断", saveStatus: "idle" as const, dirty: true },
    ]) {
      const brief = submitFailureBrief(context);
      expect(brief!.retention).not.toContain("已保存的板面");
      expect(brief!.retention).toMatch(/没有保存|还没保存|没有存到服务器/);
    }
  });
});

describe("默认失败区可直接读懂（不需要展开详情）", () => {
  it("原因、保留、重试三件事都在简短文案里", () => {
    const brief = submitFailureBrief({ status: "failed", error: "网络中断", saveStatus: "saved" })!;
    expect(brief.reason).toContain("网络中断");
    expect(brief.retention.length).toBeGreaterThan(10);
    expect(brief.nextStep).toContain("重新提交");
  });

  it("不出现实现说明，也不出现开发用语", () => {
    const brief = submitFailureBrief({
      status: "failed",
      error: "网络中断",
      saveStatus: "error",
      saveError: "磁盘写满",
      lastResult: STALE,
    })!;
    const text = [brief.reason, brief.retention, brief.nextStep, brief.detail].join(" ");
    for (const word of ["接口", "adapter", "求差", "状态机", "DOM", "板面提交接口"]) {
      expect(text.includes(word), word).toBe(false);
    }
  });

  it("较长诊断留在详情里，但不成为了解原因的必经入口", () => {
    const brief = submitFailureBrief({
      status: "failed",
      error: "网络中断",
      saveStatus: "saved",
      lastResult: STALE,
    })!;
    expect(brief.detail).toContain("上一次的旧原因");
    expect(brief.detail.length).toBeGreaterThan(brief.reason.length);
  });
});

describe("较长的诊断句（详情区）保留旧语义并区分保存失败", () => {
  it("默认（板面已保存）仍然说「不会丢改动」", () => {
    expect(submitFailureText(null)).toContain("不会丢改动");
    expect(submitFailureText(null)).toContain("基准没有被更新");
  });

  it("板面保存也失败时，不说成已保存的板面都保住了", () => {
    const text = submitFailureText(null, true);
    expect(text).toContain("不会丢改动");
    expect(text).not.toContain("已保存的板面");
    expect(text).toMatch(/没有保存|还没保存|当前页面/);
  });
});
