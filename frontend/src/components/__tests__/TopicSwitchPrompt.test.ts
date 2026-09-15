/**
 * 待确认切换的 UI（spec 第 29~30 / 74 条）。
 *
 * 必须低干扰：出现在输入区附近，不是大模态弹窗、不用浏览器确认框、
 * 不遮挡内容；用户明确表态才改变话题。
 */
import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";

import TopicSwitchPrompt from "../TopicSwitchPrompt.vue";

describe("TopicSwitchPrompt", () => {
  it("显示「转到「X」？」与两个选项", () => {
    const w = mount(TopicSwitchPrompt, { props: { topicName: "顺丁橡胶降解" } });

    expect(w.text()).toContain("转到「顺丁橡胶降解」？");
    expect(w.text()).toContain("保留当前");
    expect(w.text()).toContain("转到这里");
  });

  it("点「转到这里」只发出 confirm（自己不改任何状态）", async () => {
    const w = mount(TopicSwitchPrompt, { props: { topicName: "顺丁橡胶降解" } });

    await w.findAll("button")[1].trigger("click");

    expect(w.emitted("confirm")).toHaveLength(1);
    expect(w.emitted("keep")).toBeUndefined();
  });

  it("点「保留当前」只发出 keep", async () => {
    const w = mount(TopicSwitchPrompt, { props: { topicName: "顺丁橡胶降解" } });

    await w.findAll("button")[0].trigger("click");

    expect(w.emitted("keep")).toHaveLength(1);
    expect(w.emitted("confirm")).toBeUndefined();
  });

  it("请求进行中时按钮禁用（不重复提交）", () => {
    const w = mount(TopicSwitchPrompt, { props: { topicName: "X", busy: true } });

    expect(w.findAll("button").every((b) => b.attributes("disabled") !== undefined)).toBe(true);
  });

  it("不是模态弹窗：没有遮罩层，也不是 dialog 角色", () => {
    const w = mount(TopicSwitchPrompt, { props: { topicName: "X" } });

    expect(w.find(".topic-switch").exists()).toBe(true);
    expect(w.find(".overlay").exists()).toBe(false);
    expect(w.find("[role='dialog']").exists()).toBe(false);
    expect(w.find(".topic-switch").attributes("aria-live")).toBe("polite");
  });
});
