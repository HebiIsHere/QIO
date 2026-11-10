/**
 * P1R-D · 对话级话题阶段/恢复提示（TopicStageNotice）。
 *
 * 只验证受控呈现：阶段来自后端 TOPIC_PHASE（经 E 的 store），组件不推断、不伪造
 * 「模型生成中」；恢复失败显示真实原因，重试只 emit（由 store 唯一入口执行）。
 */
import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import TopicStageNotice from "../TopicStageNotice.vue";

function mountNotice(props: Record<string, unknown> = {}) {
  return mount(TopicStageNotice, {
    props: { stage: null, recovery: null, retrying: false, ...props },
  });
}

describe("阶段提示：只在后端给出阶段时出现", () => {
  it("没有阶段事实（旧后端 / 普通轮）：什么都不渲染", () => {
    const w = mountNotice();
    expect(w.find('[data-test="topic-stage-notice"]').exists()).toBe(false);
    w.unmount();
  });

  it("等待确认 / 已可回答：不显示「执行中」提示（避免和弹窗/状态条重复）", () => {
    for (const stage of ["awaiting_confirmation", "answerable", "decision_open"]) {
      const w = mountNotice({ stage });
      expect(w.find('[data-test="topic-stage-notice"]').exists()).toBe(false);
      w.unmount();
    }
  });

  it("已确认执行中：说明在做什么，不出现「生成」等伪造措辞", () => {
    const w = mountNotice({ stage: "executing" });
    const notice = w.find('[data-test="topic-stage-notice"]');
    expect(notice.attributes("data-stage")).toBe("executing");
    expect(notice.text()).toContain("已确认");
    expect(notice.text()).toContain("执行");
    expect(notice.text()).not.toContain("生成");
    w.unmount();
  });

  it("目标上下文准备中：说明话题已变化、上下文准备中", () => {
    const w = mountNotice({ stage: "context_preparing" });
    const notice = w.find('[data-test="topic-stage-notice"]');
    expect(notice.attributes("data-stage")).toBe("context_preparing");
    expect(notice.text()).toContain("上下文");
    expect(notice.text()).not.toContain("生成");
    w.unmount();
  });
});

describe("恢复提示：真实原因 + 可用重试", () => {
  it("回答准备失败：显示真实原因；点重试把 recordId 交给外层", async () => {
    const w = mountNotice({
      stage: "recoverable_failure",
      recovery: { recordId: "rec_1", message: "话题已切换，回答准备失败", retryable: true },
    });
    const notice = w.find('[data-test="topic-stage-notice"]');
    expect(notice.attributes("role")).toBe("alert");
    expect(notice.text()).toContain("回答准备失败");
    expect(notice.text()).toContain("保留");
    const retry = w.find('[data-test="topic-stage-retry"]');
    expect(retry.exists()).toBe(true);
    await retry.trigger("click");
    expect(w.emitted("retry")?.[0]).toEqual(["rec_1"]);
    w.unmount();
  });

  it("不可重试：说明事实但不给空按钮", () => {
    const w = mountNotice({
      stage: "recoverable_failure",
      recovery: { recordId: "rec_2", message: "话题已切换，回答生成失败", retryable: false },
    });
    expect(w.find('[data-test="topic-stage-notice"]').text()).toContain("回答生成失败");
    expect(w.find('[data-test="topic-stage-retry"]').exists()).toBe(false);
    w.unmount();
  });

  it("重试中：按钮禁用，不重复提交", () => {
    const w = mountNotice({
      stage: "recoverable_failure",
      recovery: { recordId: "rec_1", message: "话题已切换，回答准备失败", retryable: true },
      retrying: true,
    });
    expect(w.find('[data-test="topic-stage-retry"]').attributes("disabled")).toBeDefined();
    w.unmount();
  });

  it("阶段说失败但恢复记录还没到：如实说「没有完成」，不编造具体原因", () => {
    const w = mountNotice({ stage: "recoverable_failure", recovery: null });
    const notice = w.find('[data-test="topic-stage-notice"]');
    expect(notice.text()).toContain("没有完成");
    expect(w.find('[data-test="topic-stage-retry"]').exists()).toBe(false);
    w.unmount();
  });
});
