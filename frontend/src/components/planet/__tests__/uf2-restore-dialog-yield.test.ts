/**
 * uf2 · F01：整批撤回对话框（recovery 优先级 35）的让位。
 *
 * 缺陷：此前只看 hiddenBy()，被更高优先级的影响确认（40）/引导（90）盖住时仍然可见。
 * 修复后统一用 arbiter.isVisible：让位只 v-show 隐藏（展开状态/滚动位置保留），
 * Esc 不落在隐藏层；盖住者关闭后自动回来并聚焦。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import MaintenanceRestoreDialog from "../MaintenanceRestoreDialog.vue";
import { useModalArbiterStore } from "../../../stores/modalArbiter";

async function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const w = mount(MaintenanceRestoreDialog, {
    props: {
      open: true,
      modalId: "memory-batch-restore:mb_1",
      targetLabel: "批次 mb_1",
      summary: "可恢复 1 项",
      items: [
        { key: "op_merge", label: "可恢复", tone: "ok", title: "合并", reason: "无冲突", before: "旧", after: "新" },
      ],
    },
    attachTo: document.body,
    global: { plugins: [pinia] },
  });
  await flushPromises();
  return { w, arbiter: useModalArbiterStore() };
}

function scrimDisplay(w: Awaited<ReturnType<typeof setup>>["w"]): string {
  const scrim = w.find(".restore-scrim");
  return (scrim.element as HTMLElement).style.display;
}

beforeEach(() => {
  document.body.innerHTML = "";
});

describe("uf2-F01 整批撤回弹窗让位：被更高优先级盖住时隐藏且不抢 Esc，关闭后恢复", () => {
  it("影响确认盖住：隐藏；Esc 不取消；关闭后恢复可见并聚焦取消按钮", async () => {
    const { w, arbiter } = await setup();
    expect(arbiter.isOpen("memory-batch-restore:mb_1")).toBe(true);
    expect(scrimDisplay(w)).toBe("");

    expect(arbiter.open({ id: "impact-1", kind: "impact" })).toBe(true);
    await flushPromises();
    expect(scrimDisplay(w)).toBe("none");
    expect(w.find(".restore-scrim").attributes("inert")).toBeDefined();

    // 窗口级 Esc 监听仍在，但隐藏层必须自己让路（不能 preventDefault 抢更高层按键）
    const esc = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    window.dispatchEvent(esc);
    await flushPromises();
    expect(esc.defaultPrevented).toBe(false);
    expect(w.emitted("cancel")).toBeUndefined();

    arbiter.close("impact-1");
    await flushPromises();
    expect(scrimDisplay(w)).toBe("");
    expect(w.find(".restore-dialog").element.contains(document.activeElement)).toBe(true);

    // 恢复后 Esc 语义恢复：只关最上层这一个
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await flushPromises();
    expect(w.emitted("cancel")?.length).toBe(1);
    w.unmount();
  });
});
