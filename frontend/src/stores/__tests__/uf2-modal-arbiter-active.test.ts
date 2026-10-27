/**
 * uf2 · F01 基础：modalArbiter 统一活动判断（isActive / isVisible / activeId）。
 *
 * 契约（COMMON §3.1）：= 已打开 && 无整体挂起 && 是栈顶活动项。
 * 旧 getter（active/hasModal/isOpen/hiddenBy/blocksAutoApproval/blocksNotices）保持兼容。
 */
import { beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useModalArbiterStore } from "../modalArbiter";

function setup() {
  setActivePinia(createPinia());
  return useModalArbiterStore();
}

beforeEach(() => setup());

describe("uf2-modalArbiter 统一活动判断", () => {
  it("没有打开任何弹窗：没有任何活动项", () => {
    const arbiter = useModalArbiterStore();
    expect(arbiter.activeId).toBeNull();
    expect(arbiter.isActive("a")).toBe(false);
    expect(arbiter.isVisible("a")).toBe(false);
    expect(arbiter.isActive("")).toBe(false);
  });

  it("已打开且是栈顶：isActive/isVisible 为 true；同 id 幂等", () => {
    const arbiter = useModalArbiterStore();
    expect(arbiter.open({ id: "approval-a", kind: "approval" })).toBe(true);
    expect(arbiter.activeId).toBe("approval-a");
    expect(arbiter.isActive("approval-a")).toBe(true);
    expect(arbiter.isVisible("approval-a")).toBe(true);
    expect(arbiter.open({ id: "approval-a", kind: "approval" })).toBe(true);
    expect(arbiter.activeId).toBe("approval-a");
    expect(arbiter.count).toBe(1);
  });

  it("被更高优先级弹窗盖住：低层再也不是活动项（但仍是「已打开」）", () => {
    const arbiter = useModalArbiterStore();
    arbiter.open({ id: "approval-a", kind: "approval" });
    expect(arbiter.open({ id: "impact-b", kind: "impact" })).toBe(true);
    expect(arbiter.activeId).toBe("impact-b");
    expect(arbiter.isActive("impact-b")).toBe(true);
    expect(arbiter.isActive("approval-a")).toBe(false);
    expect(arbiter.isVisible("approval-a")).toBe(false);
    // 兼容语义：仍然算打开（DOM/草稿保留），hasModal/blocksNotices 不变
    expect(arbiter.isOpen("approval-a")).toBe(true);
    expect(arbiter.hasModal).toBe(true);
    expect(arbiter.blocksNotices).toBe(true);

    // 高层关闭后低层恢复活动（不需要重新 open）
    arbiter.close("impact-b");
    expect(arbiter.activeId).toBe("approval-a");
    expect(arbiter.isActive("approval-a")).toBe(true);
  });

  it("整体挂起时所有弹窗都不活动（包括栈顶）；恢复后栈顶重新活动", () => {
    const arbiter = useModalArbiterStore();
    arbiter.open({ id: "impact-b", kind: "impact" });
    arbiter.suspendAll("close-ask");
    expect(arbiter.suspendedBy).toBe("close-ask");
    expect(arbiter.isActive("impact-b")).toBe(false);
    expect(arbiter.isVisible("impact-b")).toBe(false);
    // 旧 hiddenBy 语义保持：返回挂起方
    expect(arbiter.hiddenBy("impact-b")).toBe("close-ask");
    arbiter.resumeAll();
    expect(arbiter.isActive("impact-b")).toBe(true);
    expect(arbiter.hiddenBy("impact-b")).toBeNull();
  });

  it("open() 被拒（同/更低优先级）：不会有活动项被误判", () => {
    const arbiter = useModalArbiterStore();
    arbiter.open({ id: "impact-b", kind: "impact" });
    expect(arbiter.open({ id: "approval-late", kind: "approval" })).toBe(false);
    expect(arbiter.isOpen("approval-late")).toBe(false);
    expect(arbiter.isActive("approval-late")).toBe(false);
    expect(arbiter.activeId).toBe("impact-b");
  });

  it("按优先级入栈：后开的高优先级覆盖，关闭后回到原活动项", () => {
    const arbiter = useModalArbiterStore();
    arbiter.open({ id: "credential-c", kind: "credential" });
    arbiter.open({ id: "recovery-r", kind: "recovery" });
    arbiter.open({ id: "onboarding-o", kind: "onboarding" });
    expect(arbiter.activeId).toBe("onboarding-o");
    arbiter.close("onboarding-o");
    expect(arbiter.activeId).toBe("recovery-r");
    arbiter.close("recovery-r");
    expect(arbiter.activeId).toBe("credential-c");
    expect(arbiter.isActive("credential-c")).toBe(true);
  });
});
