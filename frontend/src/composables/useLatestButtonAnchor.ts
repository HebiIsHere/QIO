/**
 * 「回到最新消息」按钮的锚定测量：把输入框与底部内容块的真实边界，变成按钮该站的 `bottom`。
 *
 * 为什么单独一个组合式：按钮被 Teleport 到对话视图的浮层里（见 `ConversationView` 的
 * `.latest-layer`），它不在消息流内部，因此**不能**再用 sticky 相对滚动容器定位 ——
 * 那样量到的是消息区底边，而消息区底边又被空着的底部内容块抬高，结果按钮飘在输入框上方很远。
 *
 * 量法：
 * - 输入框上缘：走共享的 `composerMetrics`（不自己 querySelector，避免三处各量一遍）；
 * - 底部内容块（`.bottom-cluster`）为空时高度为 0，不参与比较；有卡片时它的上缘在卡片顶部；
 * - 锚线取两者中更靠上的那条（见 `utils/latestButton.ts`），按钮因此停在卡片上方，
 *   既不会被卡片盖住，也不会为了避让抬到消息区中部。
 *
 * 所有监听与观察器在卸载时清理；同一帧多次通知合并成一次测量。
 */
import { onBeforeUnmount, onMounted, ref, type Ref } from "vue";
import {
  latestButtonBottom,
  latestButtonLeftFromBase,
  latestButtonOffsetFromBase,
  latestButtonTargetBottom,
} from "../utils/latestButton";
import { subscribeComposerMetrics } from "./composerMetrics";

export function useLatestButtonAnchor(anchorRef?: Ref<HTMLElement | null>): {
  bottomPx: Ref<number | null>;
  centerX: Ref<number | null>;
  remeasure: () => void;
} {
  const bottomPx = ref<number | null>(null);
  /** 按钮的水平中心（视口坐标）：对齐对话内容列中心 */
  const centerX = ref<number | null>(null);
  let unsubscribeComposer: (() => void) | null = null;
  let clusterObserver: ResizeObserver | null = null;
  let clusterMutations: MutationObserver | null = null;
  let raf = 0;
  let disposed = false;
  /** 最近一次量到的输入框上缘与中线（找不到输入框时都是 null） */
  let composerTop: number | null = null;
  let composerCenterX: number | null = null;
  /** 输入面板给出的局部→视口比例（锚点自己还没渲染时用它兜底） */
  let composerScale = 1;

  /**
   * 在锚点自身探测「参考原点 + 缩放比例」（只读探测，测完立刻还原样式）。
   *
   * 一次探测拿到三件事：
   * - `baseBottom`：写 `bottom: 0` 时下缘落在哪 = **包含块底边**（不一定是视口底边）；
   * - `baseLeft`：写 `left: 0` 时左缘落在哪 = 包含块左边；
   * - `scale`：局部像素 → 视口像素（写 0 → 100 看实际移动多少）。
   *
   * 这样就不再依赖 `window.innerHeight` 与「包含块 = 视口」这两个假设 ——
   * 实测 CSS zoom 下包含块底边会比视口高 15px，用视口高度算会让按钮飘高。
   */
  const probeAnchor = (
    el: HTMLElement,
  ): { baseBottom: number; baseLeft: number; scale: number; width: number } | null => {
    const prevBottom = el.style.bottom;
    const prevLeft = el.style.left;
    try {
      el.style.bottom = "0px";
      el.style.left = "0px";
      const zero = el.getBoundingClientRect();
      el.style.bottom = "100px";
      el.style.left = "100px";
      const hundred = el.getBoundingClientRect();
      const scale = (zero.bottom - hundred.bottom) / 100;
      if (!Number.isFinite(scale) || scale <= 0.1 || scale >= 10) return null;
      return {
        baseBottom: zero.bottom,
        baseLeft: zero.left,
        scale,
        width: el.offsetWidth,
      };
    } catch {
      return null;
    } finally {
      el.style.bottom = prevBottom;
      el.style.left = prevLeft;
    }
  };

  /**
   * 对话内容列的中心：消息列是「按 --column-inset-* 让位、内部再居中」的，
   * 所以内容列中心 = 消息区内边距之间的中点。拿不到消息流时才退回输入框中线
   * （两者是同一条列，见 tokens.css 的说明）。
   */
  const columnCenterX = (): number | null => {
    const stream = document.querySelector<HTMLElement>(".stream");
    if (stream) {
      const rect = stream.getBoundingClientRect();
      const style = getComputedStyle(stream);
      const left = rect.left + (parseFloat(style.paddingLeft) || 0);
      const right = rect.right - (parseFloat(style.paddingRight) || 0);
      if (right > left) return (left + right) / 2;
    }
    return composerCenterX;
  };

  const measure = (): void => {
    if (disposed) return;
    const cluster = document.querySelector<HTMLElement>(".bottom-cluster");
    let clusterTop: number | null = null;
    if (cluster) {
      const rect = cluster.getBoundingClientRect();
      // 高度为 0（空块）时它就是输入框上缘，不参与「更靠上」的比较
      if (rect.height > 0) clusterTop = rect.top;
    }
    // 锚线：输入区面板上缘与底部内容块上缘里更靠上的那条
    const tops = [composerTop, clusterTop].filter(
      (v): v is number => typeof v === "number" && Number.isFinite(v),
    );
    if (!tops.length) {
      bottomPx.value = null;
      centerX.value = null;
      return;
    }
    const anchorTop = Math.min(...tops);
    const targetBottom = latestButtonTargetBottom(anchorTop);
    const targetCenterX = columnCenterX();

    const anchorEl = anchorRef?.value ?? null;
    const probe = anchorEl ? probeAnchor(anchorEl) : null;
    if (probe) {
      // 用实测原点 + 比例换算：不假设「包含块 = 视口」
      bottomPx.value = latestButtonOffsetFromBase({
        baseBottom: probe.baseBottom,
        targetBottom,
        scale: probe.scale,
      });
      centerX.value =
        targetCenterX === null
          ? null
          : latestButtonLeftFromBase({
              baseLeft: probe.baseLeft,
              targetCenterX,
              anchorWidth: probe.width,
              scale: probe.scale,
            });
      return;
    }
    // 锚点还没渲染（按钮隐藏）：退回按视口高度估算，比例用输入面板的
    const scale = composerScale > 0 ? composerScale : 1;
    const raw = latestButtonBottom({
      composerTop,
      clusterTop,
      viewportHeight: window.innerHeight,
    });
    bottomPx.value = raw === null ? null : raw / scale;
    centerX.value = targetCenterX === null ? null : targetCenterX / scale;
  };

  /** 同一帧内多次通知只测一次 */
  const schedule = (): void => {
    if (disposed || raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      measure();
    });
  };

  const observeCluster = (): void => {
    if (disposed) return;
    const cluster = document.querySelector<HTMLElement>(".bottom-cluster");
    if (!cluster || typeof ResizeObserver === "undefined") return;
    clusterObserver = new ResizeObserver(schedule);
    clusterObserver.observe(cluster);
    // 空块 ↔ 有卡片：块自身高度可能没有变化，尺寸观察器不一定报，再盯一次子节点
    if (typeof MutationObserver !== "undefined") {
      clusterMutations = new MutationObserver(schedule);
      clusterMutations.observe(cluster, { childList: true, subtree: true });
    }
  };

  onMounted(() => {
    // 订阅时同步量一次，首帧就有位置；之后尺寸/视口变化由共享量测合并通知
    unsubscribeComposer = subscribeComposerMetrics(({ found, top, centerX: cx, scale: s }) => {
      // 锚的是**输入区面板**上缘：面板顶部还有一行话题名 / 键盘提示（`.topicbar`），
      // 贴到输入框本体（textarea）上缘会让按钮落进那一行里 —— 实测 elementFromPoint
      // 命中的是 .topicbar，按钮点不到。输入框本体距面板上缘的距离随窗口缩放变化，
      // 用面板上缘才能保证「按钮始终在输入区之外、且始终可点」。
      composerTop = found ? top : null;
      composerCenterX = found ? cx : null;
      composerScale = s > 0 ? s : 1;
      schedule();
    });
    observeCluster();
    measure();
  });

  onBeforeUnmount(() => {
    disposed = true;
    if (raf) cancelAnimationFrame(raf);
    raf = 0;
    unsubscribeComposer?.();
    unsubscribeComposer = null;
    clusterObserver?.disconnect();
    clusterObserver = null;
    clusterMutations?.disconnect();
    clusterMutations = null;
  });

  return { bottomPx, centerX, remeasure: measure };
}
