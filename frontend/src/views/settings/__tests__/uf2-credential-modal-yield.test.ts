/**
 * uf2 · F01：凭据弹窗的让位（它是中央弹窗，credential 优先级 20）。
 *
 * 缺陷：此前只看 hiddenBy()（仅识别 suspendAll），被更高优先级的审批/影响确认盖住时仍然可见、
 * 仍然接收键盘。修复后统一用 arbiter.isVisible/isActive：
 * 被盖住 → v-show 隐藏 + inert（草稿保留），Esc 不生效；盖住者关闭后恢复可见并把焦点放回表单。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

vi.mock("../../../services/api", () => {
  class ApiError extends Error {
    constructor(
      readonly status: number,
      readonly path: string,
      detail: string,
    ) {
      super(`${path} -> ${status}: ${detail}`);
      this.name = "ApiError";
    }
  }
  return {
    ApiError,
    api: {
      listProviders: vi.fn(async () => ({ providers: [], model_note: "" })),
      listCredentialModels: vi.fn(async () => ({ models: [] })),
      createCredential: vi.fn(),
      verifyCredential: vi.fn(),
      verifyCredentialDraft: vi.fn(),
      updateCredentialMeta: vi.fn(),
    },
  };
});

import CredentialModal from "../CredentialModal.vue";
import { useModalArbiterStore } from "../../../stores/modalArbiter";

async function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const w = mount(CredentialModal, {
    props: { mode: "create" as const },
    attachTo: document.body,
    global: { plugins: [pinia] },
  });
  await flushPromises();
  return { w, arbiter: useModalArbiterStore() };
}

function maskDisplay(w: Awaited<ReturnType<typeof setup>>["w"]): string {
  return (w.find(".modal-mask").element as HTMLElement).style.display;
}

beforeEach(() => {
  document.body.innerHTML = "";
  try {
    localStorage.clear();
  } catch {
    // 存储不可用不影响本用例
  }
});

describe("uf2-F01 凭据弹窗让位：被更高优先级盖住时不可见、不可交互", () => {
  it("审批/影响确认盖住：v-show 隐藏 + inert；Esc 不触发取消；关闭后恢复可见与焦点", async () => {
    const { w, arbiter } = await setup();
    expect(arbiter.isOpen("qio-credential-modal")).toBe(true);
    expect(maskDisplay(w)).toBe("");

    // 更高优先级审批打开：凭据弹窗必须让位（此前仍可见）
    expect(arbiter.open({ id: "unified-approval", kind: "approval" })).toBe(true);
    await flushPromises();
    expect(maskDisplay(w)).toBe("none");
    expect(w.find(".modal-mask").attributes("inert")).toBeDefined();
    expect(w.find(".modal-mask").attributes("aria-hidden")).toBe("true");
    // 表单 DOM/已填草稿保留（密钥输入框仍在 DOM 里）
    expect(w.find(".modal").exists()).toBe(true);

    // 被隐藏层不处理键盘：Esc 不会再从这层冒出去取消（否则等于隐藏窗口抢按键）
    const cancelEvents: unknown[] = [];
    const keyEvent = new KeyboardEvent("keydown", { key: "Escape", bubbles: true });
    w.find(".modal").element.dispatchEvent(keyEvent);
    await flushPromises();
    expect(cancelEvents.length).toBe(0);
    expect(w.emitted("cancel")).toBeUndefined();

    // 盖住者关闭：凭据弹窗恢复可见，焦点回到弹窗内
    arbiter.close("unified-approval");
    await flushPromises();
    expect(maskDisplay(w)).toBe("");
    expect(w.find(".modal").element.contains(document.activeElement)).toBe(true);
    w.unmount();
  });
});
