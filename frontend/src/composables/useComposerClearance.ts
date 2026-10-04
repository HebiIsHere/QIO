/**
 * 底部让位：把输入区占的高度，作为底部这几块的下内边距，
 * 让它们整体抬到输入气泡上方。
 *
 * 为什么需要：输入区是固定悬浮在窗口底部的，而下面这几块（知识候选卡、
 * 兼容模式提示、话题切换提示）在排版里排在消息流之后，于是正好落在输入区下面 ——
 * 实测 1440×900 下「保存 / 修改 / 忽略」三个按钮的中点点下去，命中的是输入框里的
 * 文字区，用户看得见却点不到。
 *
 * 两条规则（第二条是修复提示词 §6 要求的收口）：
 * 1. **有内容**时写下内边距 = 输入区高度 + 间隙；
 * 2. **空块不写** —— 以前即使没有任何内容也留出「输入区高度 + 16px」，消息区底边被抬高，
 *    靠 sticky 贴底的元素（例如「回到最新消息」按钮）也跟着飘到输入框上方很远。
 *
 * 尺寸不再自己量：统一走 `composerMetrics`（以前消息流、这里、按钮锚定各量一遍）。
 */
import { onBeforeUnmount, onMounted, type Ref } from "vue";
import { subscribeComposerMetrics } from "./composerMetrics";

/** 输入区与底部内容之间保留的间隙（与消息流一致） */
const GAP_PX = 16;

export function useComposerClearance(target: Ref<HTMLElement | null>): void {
  let unsubscribe: (() => void) | null = null;
  let mutations: MutationObserver | null = null;

  /** 这一块现在真的有没有内容（空块不需要让位） */
  const hasContent = (el: HTMLElement): boolean => {
    if (el.childElementCount > 0) return true;
    return Boolean(el.textContent && el.textContent.trim());
  };

  const apply = (height: number): void => {
    const el = target.value;
    if (!el) return;
    if (!hasContent(el)) {
      el.style.paddingBottom = "";
      return;
    }
    el.style.paddingBottom = height > 0 ? `${Math.round(height) + GAP_PX}px` : "";
  };

  /** 最近一次量到的输入区高度：子节点变化时用它重算，不重新读布局 */
  let lastHeight = 0;

  onMounted(() => {
    unsubscribe = subscribeComposerMetrics(({ height }) => {
      lastHeight = height;
      apply(height);
    });
    // 候选卡出现/消失时块自身高度可能不变（0 → 有内容），光靠尺寸观察会漏；盯一次子节点
    const el = target.value;
    if (el && typeof MutationObserver !== "undefined") {
      mutations = new MutationObserver(() => apply(lastHeight));
      mutations.observe(el, { childList: true, subtree: true });
    }
  });

  onBeforeUnmount(() => {
    unsubscribe?.();
    unsubscribe = null;
    mutations?.disconnect();
    mutations = null;
  });
}
