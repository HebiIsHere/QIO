/**
 * 自动弹出门控：星球覆盖层（小球态 vs 展开态）、输入法组合输入、失焦延迟、设置页。
 *
 * 真实缺陷回归（Lead 复核）：对话页空闲时 PlanetView 常驻在 DOM 里，根元素是
 * `.planet-view.ball`（入口小球），**不是覆盖层** —— 只按选择器判断会让审批永远不自动弹。
 */
import { beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { planetContinuum } from "../../composables/planetContinuum";
import { useModalArbiterStore } from "../modalArbiter";
import {
  INPUT_LEAVE_RETRY_DELAY_MS,
  canAutoOpenApproval,
  feedbackGate,
  installFeedbackGate,
  isPlanetOverlayOpen,
  resetFeedbackGateState,
  visibleSurface,
} from "../feedbackGate";

function mountConversationSurface(): void {
  const surface = document.createElement("div");
  surface.className = "conversation-workspace";
  document.body.appendChild(surface);
}

describe("星球覆盖层判断：入口小球不是覆盖层", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    document.body.innerHTML = "";
    planetContinuum.phase = "idle";
    resetFeedbackGateState();
    mountConversationSurface();
  });

  it("对话页空闲时 .planet-view.ball 存在，但可以自动弹出", () => {
    const ball = document.createElement("div");
    ball.className = "planet-view ball";
    document.body.appendChild(ball);

    expect(isPlanetOverlayOpen()).toBe(false);
    expect(visibleSurface()).toBe("chat");
    expect(canAutoOpenApproval().ok).toBe(true);
  });

  it("星球展开（entered）后不自动弹；关闭回小球态后恢复", () => {
    const ball = document.createElement("div");
    ball.className = "planet-view ball";
    document.body.appendChild(ball);
    expect(canAutoOpenApproval().ok).toBe(true);

    ball.className = "planet-view entered";
    const opened = canAutoOpenApproval();
    expect(opened.ok).toBe(false);
    expect(opened.reason).toBe("planet-overlay");
    expect(opened.retry).toBe(true);

    ball.className = "planet-view ball";
    expect(canAutoOpenApproval().ok).toBe(true);
  });

  it("连续体阶段非 idle（展开中/收起中）同样算覆盖层，即使 class 还没落到元素上", () => {
    planetContinuum.phase = "activating";
    expect(canAutoOpenApproval().reason).toBe("planet-overlay");
    planetContinuum.phase = "returning";
    expect(canAutoOpenApproval().reason).toBe("planet-overlay");
    planetContinuum.phase = "idle";
    expect(canAutoOpenApproval().ok).toBe(true);
  });
});

describe("输入门控：组合输入 / 输入中 / 失焦延迟", () => {
  let stopGate: (() => void) | null = null;

  beforeEach(() => {
    setActivePinia(createPinia());
    document.body.innerHTML = "";
    planetContinuum.phase = "idle";
    resetFeedbackGateState();
    mountConversationSurface();
    stopGate = installFeedbackGate(document);
  });

  it("输入法组合输入期间不弹；组合结束后仍在输入也不弹", () => {
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();
    expect(canAutoOpenApproval().reason).toBe("editing");

    input.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
    expect(feedbackGate.composing).toBe(true);
    expect(canAutoOpenApproval().reason).toBe("composing");

    input.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true }));
    expect(feedbackGate.composing).toBe(false);
    expect(canAutoOpenApproval().reason).toBe("editing");
    stopGate?.();
  });

  it("离开输入框后有短暂延迟再允许自动弹出", () => {
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();
    input.blur();
    expect(feedbackGate.editing).toBe(false);
    const decision = canAutoOpenApproval();
    expect(decision.ok).toBe(false);
    expect(decision.reason).toBe("input-just-left");
    expect(decision.retry).toBe(true);

    feedbackGate.lastInputLeftAt = Date.now() - (INPUT_LEAVE_RETRY_DELAY_MS + 10);
    expect(canAutoOpenApproval().ok).toBe(true);
    stopGate?.();
  });
});

describe("页面门控：设置页 / 调试页 / 中央弹窗", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    document.body.innerHTML = "";
    planetContinuum.phase = "idle";
    resetFeedbackGateState();
  });

  it("设置页不自动弹（永久性阻挡，不留 pending 标记）", () => {
    const settings = document.createElement("div");
    settings.className = "settings";
    document.body.appendChild(settings);
    const decision = canAutoOpenApproval();
    expect(decision.ok).toBe(false);
    expect(decision.reason).toBe("no-chat-surface");
    expect(decision.retry).toBe(false);
  });

  it("调试页不自动弹", () => {
    const debug = document.createElement("div");
    debug.className = "debug";
    document.body.appendChild(debug);
    expect(canAutoOpenApproval().reason).toBe("no-chat-surface");
  });

  it("有中央弹窗时不自动弹（走 modalArbiter，不看 z-index）", () => {
    mountConversationSurface();
    const arbiter = useModalArbiterStore();
    arbiter.open({ id: "impact-1", kind: "impact" });
    const decision = canAutoOpenApproval();
    expect(decision.ok).toBe(false);
    expect(decision.reason).toBe("modal");
    expect(decision.retry).toBe(true);
    arbiter.close("impact-1");
    expect(canAutoOpenApproval().ok).toBe(true);
  });
});
