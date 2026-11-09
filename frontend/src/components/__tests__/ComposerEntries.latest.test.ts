/**
 * 「回到最新消息」入口（修复提示词 §三）。
 *
 * 它已并入输入区辅助入口行：固定顺序末尾、与其它入口同尺寸/间距、
 * 强调色实底 + 白色箭头、角标与实底清楚区分；只改变阅读位置，
 * 不取消「从历史继续」选择、不改消息状态。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { nextTick } from "vue";
import ComposerEntries from "../conversation/ComposerEntries.vue";
import { useSessionStore } from "../../stores/session";
import { useChatViewportStore } from "../../stores/chatViewport";

const SRC = readFileSync(
  resolve(process.cwd(), "src/components/conversation/ComposerEntries.vue"),
  "utf8",
);

function freshPinia(): Pinia {
  const pinia = createPinia();
  setActivePinia(pinia);
  return pinia;
}

function mountEntries(extraEntries: { id: string; title: string; hint?: string; visible?: boolean }[] = []) {
  const pinia = freshPinia();
  const w = mount(ComposerEntries, {
    props: { extraEntries },
    global: { plugins: [pinia] },
  });
  return {
    w,
    session: useSessionStore(),
    viewport: useChatViewportStore(),
  };
}

describe("回到最新入口", () => {
  it("只有离开底部且确实有消息时出现", async () => {
    const { w, session, viewport } = mountEntries();
    expect(w.find('[data-test="back-to-latest"]').exists()).toBe(false);

    // 跟随中：不出现（即使有消息）
    session.pushUser("一条消息");
    viewport.setFollowing(true);
    await nextTick();
    expect(w.find('[data-test="back-to-latest"]').exists()).toBe(false);

    // 离开底部：出现
    viewport.setFollowing(false);
    await nextTick();
    expect(w.find('[data-test="back-to-latest"]').exists()).toBe(true);
    w.unmount();
  });

  it("固定顺序在末尾：额外事项入口在前，回到最新在后", async () => {
    const { w, session, viewport } = mountEntries([
      { id: "recovery", title: "恢复事项", hint: "需要你处理", visible: true },
    ]);
    session.pushUser("一条消息");
    viewport.setFollowing(false);
    await nextTick();
    const row = w.find(".entry-row");
    expect(row.exists()).toBe(true);
    const buttons = row.findAll("button.entry-btn");
    expect(buttons.length).toBeGreaterThanOrEqual(2);
    expect(buttons[buttons.length - 1].attributes("data-entry")).toBe("backToLatest");
    w.unmount();
  });

  it("角标显示未读数量，可访问名称包含条数；点击只发出回到最新请求", async () => {
    const { w, session, viewport } = mountEntries();
    session.pushUser("一条消息");
    viewport.setFollowing(false);
    viewport.setUnseen(3);
    await nextTick();
    const button = w.find('[data-test="back-to-latest"]');
    expect(button.exists()).toBe(true);
    expect(button.element.tagName).toBe("BUTTON");
    expect(button.attributes("aria-label")).toContain("回到最新消息");
    expect(button.attributes("aria-label")).toContain("3 条新消息");
    expect(button.find(".count").text()).toBe("3");

    const before = viewport.backRequest;
    await button.trigger("click");
    expect(viewport.backRequest).toBe(before + 1);
    // 只改变阅读位置：不取消接续选择（没有登记时也不会凭空产生）
    expect(session.pendingContinuation).toBeNull();
    w.unmount();
  });

  it("强调色实底 + 白色箭头，角标与实底清楚区分（颜色全部走令牌）", () => {
    const rule = /\.entry-btn\.back-latest\s*\{([^}]*)\}/.exec(SRC)?.[1] ?? "";
    expect(rule).toContain("background: var(--accent)");
    expect(rule).toContain("color: var(--on-accent)");
    const hover = /\.entry-btn\.back-latest:hover\s*\{([^}]*)\}/.exec(SRC)?.[1] ?? "";
    expect(hover).toContain("var(--accent-hover)");
    const count = /\.entry-btn\.back-latest \.count\s*\{([^}]*)\}/.exec(SRC)?.[1] ?? "";
    expect(count).toContain("background: var(--bg-surface)");
    expect(count).toContain("color: var(--accent)");
    // 尺寸/图标与其它入口一致
    expect(SRC).toMatch(/\.entry-btn\s*\{[^}]*width:\s*34px/);
    expect(SRC).toMatch(/\.entry-btn \.ico\s*\{[^}]*width:\s*16px/);
    // 没有光晕/额外描边
    expect(SRC).not.toMatch(/back-latest[^{]*\{[^}]*box-shadow/);
  });

  it("没有任何入口时整行收起（不占空白）", async () => {
    const { w } = mountEntries();
    await nextTick();
    expect(w.find(".entry-row").exists()).toBe(false);
    w.unmount();
  });
});
