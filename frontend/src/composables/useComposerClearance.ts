/**
 * 底部让位：把输入区占的高度，作为底部这几块的下内边距，
 * 让它们整体抬到输入气泡上方。
 *
 * 为什么需要：输入区是固定悬浮在窗口底部的，而下面这几块（知识候选卡、
 * 兼容模式提示、话题切换提示）在排版里排在消息流之后，于是正好落在输入区下面 ——
 * 实测 1440×900 下「保存 / 修改 / 忽略」三个按钮的中点点下去，命中的是输入框里的
 * 文字区，用户看得见却点不到。
 *
 * 消息流早就有同一份测量（MessageStream 里量 `.composer` 写到自己的 padding-bottom），
 * 这里把同一件事复用在底部这几块上，保证两处用的是同一个数。
 */
import { onBeforeUnmount, onMounted, type Ref } from "vue";

/** 输入区与底部内容之间保留的间隙（与消息流一致） */
const GAP_PX = 16;

export function useComposerClearance(target: Ref<HTMLElement | null>): void {
  let observer: ResizeObserver | null = null;
  let raf = 0;
  let tries = 0;

  const apply = (): void => {
    const el = target.value;
    if (!el) return;
    const composer = document.querySelector<HTMLElement>(".composer");
    if (!composer) {
      el.style.paddingBottom = "";
      return;
    }
    const height = Math.round(composer.getBoundingClientRect().height);
    el.style.paddingBottom = height > 0 ? `${height + GAP_PX}px` : "";
  };

  const observeComposer = (): void => {
    const composer = document.querySelector<HTMLElement>(".composer");
    if (!composer) {
      // 输入区可能比本视图晚挂载：有限次重试，避免无限轮询
      if (tries < 40) {
        tries += 1;
        raf = requestAnimationFrame(observeComposer);
      }
      return;
    }
    apply();
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(apply);
      observer.observe(composer);
    }
  };

  onMounted(() => {
    // 先算一次：首屏就有候选卡时不能等到下一次尺寸变化才让位
    apply();
    observeComposer();
  });

  onBeforeUnmount(() => {
    if (raf) cancelAnimationFrame(raf);
    observer?.disconnect();
    observer = null;
  });
}
