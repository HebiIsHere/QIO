/**
 * 中央弹窗仲裁（B · 公共接口，契约 §3.2）。
 *
 * 规则（硬约束，不靠 z-index 解决遮挡）：
 * - 同一时刻只允许**一个**中央弹窗处于活动状态：业务组件打开前先 `open()`，
 *   `open()` 返回 false 时调用方**不得强行显示**（自己收起或等待）；
 * - 优先级高的弹窗可以叠在低的之上（低优先级被 `hiddenBy` 判为隐藏，用 v-show 保留 DOM/草稿）；
 *   同优先级或更低优先级一律拒绝；
 * - 关闭询问这类最高层触发器调用 `suspendAll(by)`：业务弹窗只隐藏、状态与草稿不丢，
 *   取消/结束后 `resumeAll()` 恢复；
 * - 审批自动弹出、通知显示在 `blocksAutoApproval` / `hasModal` 时排队等待。
 *
 * 中央弹窗的打开/关闭**必须**走 register/unregister（本 store），不得用增大 z-index 抢层。
 */
import { defineStore } from "pinia";

export type ModalKind =
  | "approval"
  | "confirm"
  | "impact"
  | "recovery"
  | "close-ask"
  | "credential"
  | "onboarding"
  | "task-drawer"
  | "editor"
  | "other";

export interface ModalDescriptor {
  /** 稳定身份（同一弹窗重复 open 幂等）。 */
  id: string;
  kind: ModalKind;
  /** 是否允许用户直接收起（外部点击 / Esc）；不影响仲裁，只作为调用方的提示。 */
  dismissible?: boolean;
  /** 显式优先级；缺省按 kind 的默认优先级。 */
  priority?: number;
}

/**
 * 各类型弹窗的默认层级（数字越大越靠上）。
 *
 * 只表达「谁必须让位给谁」，与视觉 z-index 无关：
 * 关闭询问/引导是整窗级触发器；影响确认与恢复是阻塞决定的确认；审批是常规单次授权；
 * 凭据与确认层属于局部弹窗；抽屉/编辑器最低。
 */
export const MODAL_PRIORITY: Record<ModalKind, number> = {
  "close-ask": 100,
  onboarding: 90,
  impact: 40,
  recovery: 35,
  approval: 30,
  credential: 20,
  confirm: 10,
  editor: 5,
  "task-drawer": 5,
  other: 0,
};

/** 一个描述符的真实优先级（显式值优先）。 */
export function descriptorPriority(descriptor: ModalDescriptor | null | undefined): number {
  if (!descriptor) return Number.NEGATIVE_INFINITY;
  if (typeof descriptor.priority === "number" && Number.isFinite(descriptor.priority)) {
    return descriptor.priority;
  }
  return MODAL_PRIORITY[descriptor.kind] ?? MODAL_PRIORITY.other;
}

export interface ModalSuspended {
  /** 触发挂起的一方（例如 "close-ask"）。 */
  by: string;
}

export const useModalArbiterStore = defineStore("modalArbiter", {
  state: () => ({
    /** 打开中的中央弹窗（按优先级升序；最后一个 = 活动项）。 */
    stack: [] as ModalDescriptor[],
    /** 被最高层触发器整体挂起（关闭询问等）。null = 没有挂起。 */
    suspended: null as ModalSuspended | null,
  }),
  getters: {
    /** 当前活动的中央弹窗（最高优先级、最后打开的那一个）。 */
    active(state): ModalDescriptor | null {
      return state.stack.length ? state.stack[state.stack.length - 1] ?? null : null;
    },
    /** 是否存在任何打开的中央弹窗（被挂起时仍然算打开：状态与草稿还在）。 */
    hasModal(state): boolean {
      return state.stack.length > 0;
    },
    count(state): number {
      return state.stack.length;
    },
    isOpen(state) {
      return (id: string): boolean =>
        Boolean(id) && state.stack.some((descriptor) => descriptor.id === id);
    },
    /**
     * 这个弹窗是否被更高层触发器隐藏（DOM 保留、用 v-show 收起）。
     * 返回触发挂起的一方；没有挂起 / 这个弹窗没打开 → null。
     */
    hiddenBy(state) {
      return (id: string): string | null => {
        if (!state.suspended) return null;
        const open = Boolean(id) && state.stack.some((descriptor) => descriptor.id === id);
        return open ? state.suspended.by : null;
      };
    },
    suspendedBy(state): string | null {
      return state.suspended?.by ?? null;
    },
    /** 审批自动弹出必须等待：有中央弹窗、或整体被挂起。 */
    blocksAutoApproval(state): boolean {
      return state.stack.length > 0 || state.suspended !== null;
    },
    /** 通知显示必须排队：有中央弹窗、或整体被挂起。 */
    blocksNotices(state): boolean {
      return state.stack.length > 0 || state.suspended !== null;
    },
  },
  actions: {
    /**
     * 打开 / 更新一个中央弹窗。
     *
     * @returns true = 已经可以显示（新打开或本来就开着）；false = 被更高/同优先级的活动弹窗挡住，
     *          调用方不得强行显示。
     */
    open(descriptor: ModalDescriptor): boolean {
      const id = String(descriptor?.id ?? "").trim();
      if (!id) return false;
      const existing = this.stack.find((item) => item.id === id);
      if (existing) {
        // 幂等：同一个弹窗重复注册只更新描述（不改变层级次序）
        Object.assign(existing, descriptor, { id });
        return true;
      }
      const active = this.active;
      if (active && descriptorPriority(active) >= descriptorPriority(descriptor)) {
        return false;
      }
      this.stack.push({ ...descriptor, id });
      // 按优先级升序排列：最后一个永远是活动项（同级不会并存：上面已拒绝）
      this.stack.sort((a, b) => descriptorPriority(a) - descriptorPriority(b));
      return true;
    },
    /** 关闭一个中央弹窗（没有打开时是安全的 no-op）。 */
    close(id: string): void {
      const target = String(id ?? "").trim();
      if (!target) return;
      this.stack = this.stack.filter((descriptor) => descriptor.id !== target);
    },
    /**
     * 关闭询问等最高层触发器：把业务弹窗整体挂起（只隐藏，不卸载、不丢状态）。
     * 重复调用时以**第一个**触发器为准（不允许中途换成别的挂起者）。
     */
    suspendAll(by: string): void {
      const source = String(by ?? "").trim();
      if (!source) return;
      if (this.suspended) return;
      this.suspended = { by: source };
    },
    /** 取消/结束最高层触发器：恢复被挂起的业务弹窗。 */
    resumeAll(): void {
      this.suspended = null;
    },
    /** 状态重置（登出 / 测试收口用）。 */
    reset(): void {
      this.stack = [];
      this.suspended = null;
    },
  },
});
