/**
 * 凭据弹窗的键盘契约（与 ApprovalModal 保持一致）：
 * Esc 只取消最上层这一个弹窗；Tab 在弹窗内循环，焦点不会跑到遮罩后面的设置页上。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import CredentialModal from "../CredentialModal.vue";

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

async function setup() {
  const w = mount(CredentialModal, {
    props: { mode: "create" as const },
    attachTo: document.body,
  });
  await flushPromises();
  const dialog = w.find(".modal");
  expect(dialog.exists()).toBe(true);
  return { w, dialog };
}

beforeEach(() => {
  document.body.innerHTML = "";
});

describe("凭据弹窗的键盘与无障碍契约", () => {
  it("是带名字的模态对话框，打开后焦点落进弹窗里", async () => {
    const { dialog } = await setup();
    expect(dialog.attributes("role")).toBe("dialog");
    expect(dialog.attributes("aria-modal")).toBe("true");
    expect(dialog.attributes("aria-label")).toBe("添加凭据");
    expect(dialog.element.contains(document.activeElement)).toBe(true);
  });

  it("Tab 到底部会回到弹窗第一个可聚焦元素，不会掉到遮罩后面", async () => {
    const { w, dialog } = await setup();
    const items = Array.from(
      dialog.element.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    );
    expect(items.length).toBeGreaterThan(1);
    const first = items[0];
    const last = items[items.length - 1];
    last.focus();
    await dialog.trigger("keydown", { key: "Tab" });
    expect(document.activeElement).toBe(first);
    expect(dialog.element.contains(document.activeElement)).toBe(true);
    void w;
  });

  it("Shift+Tab 从第一个元素回到最后一个", async () => {
    const { dialog } = await setup();
    const items = Array.from(
      dialog.element.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    );
    const first = items[0];
    const last = items[items.length - 1];
    first.focus();
    await dialog.trigger("keydown", { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
  });

  it("Esc 取消这个弹窗，并且不把事件继续往上冒（只关最上层）", async () => {
    const { w, dialog } = await setup();
    let bubbled = false;
    document.addEventListener("keydown", () => {
      bubbled = true;
    });
    await dialog.trigger("keydown", { key: "Escape" });
    expect(w.emitted("cancel")).toBeTruthy();
    expect(bubbled).toBe(false);
  });
});
