/**
 * QModal（2026-10-26 新增共享中央弹窗外壳）。
 *
 * 守的是“所有中央弹窗看起来是同一个东西”的硬约束：
 * - 标题栏三列等宽，标题居中；关闭按钮只出现在右槽，不挤偏标题；
 * - 只有一个 role=dialog，且 aria-modal / aria-labelledby 完整；
 * - Esc / 外部点击 / 关闭按钮只在 dismissible 且非 busy 时关闭；
 * - 焦点陷阱、关闭回焦、不抢输入焦点、重复 close 只发一次；
 * - 按钮组走共享 .qio-modal-actions（等宽同高/窄窗纵排由 base.css 提供）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { readFileSync } from "node:fs";
import { createPinia, setActivePinia } from "pinia";
import QModal from "../QModal.vue";
import { useModalArbiterStore } from "../../../stores/modalArbiter";

afterEach(() => {
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

async function flush(): Promise<void> {
  await nextTick();
  await nextTick();
}

function mountModal(props: Record<string, unknown> = {}) {
  return mount(QModal, {
    props: { open: true, title: "确认操作", modalId: "modal-1", ...props },
    attachTo: document.body,
  });
}

function dialogEl(): HTMLElement | null {
  return document.querySelector('[role="dialog"]');
}

function pressEscape(): void {
  window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
}

describe("QModal 结构与可访问性", () => {
  it("只有一个模态对话层：Teleport 到 body、aria 完整、标题与 kicker 同轴", async () => {
    const w = mountModal({ kicker: "危险操作" });
    await flush();
    const dialog = dialogEl();
    expect(dialog).not.toBeNull();
    expect(dialog!.getAttribute("aria-modal")).toBe("true");
    expect(dialog!.getAttribute("aria-labelledby")).toBe("modal-1-title");
    expect(document.getElementById("modal-1-title")?.textContent?.trim()).toBe("确认操作");
    expect(dialog!.getAttribute("data-modal-id")).toBe("modal-1");
    // 文本都在中轴（text-align:center 来自共享类）
    expect(dialog!.querySelector(".qio-modal-title")?.textContent?.trim()).toBe("确认操作");
    expect(dialog!.querySelector(".qio-modal-kicker")?.textContent?.trim()).toBe("危险操作");
    expect(document.querySelectorAll('[role="dialog"]').length).toBe(1);
    w.unmount();
  });

  it("标题栏三列：左槽留空不可聚焦、中间标题、右槽关闭按钮", async () => {
    const w = mountModal();
    await flush();
    const head = document.querySelector(".qio-modal-head")!;
    expect(head.children.length).toBe(3);
    const [left, middle, right] = [...head.children] as HTMLElement[];
    expect(left.className).toContain("qio-modal-head__slot");
    expect(left.getAttribute("aria-hidden")).toBe("true");
    expect(left.querySelectorAll("button, a, input, [tabindex]").length).toBe(0);
    expect(middle.contains(document.querySelector(".qio-modal-title"))).toBe(true);
    const close = right.querySelector("button");
    expect(close).not.toBeNull();
    expect(close!.getAttribute("aria-label")).toBe("关闭");
    expect(close!.hasAttribute("title")).toBe(false);
    w.unmount();
  });

  it("按钮组挂在共享动作容器里；默认内容在正文容器里", async () => {
    const w = mount(QModal, {
      props: { open: true, title: "标题", modalId: "modal-2" },
      slots: { default: "<p class='body-x'>正文内容</p>", footer: "<button class='ok'>确定</button>" },
      attachTo: document.body,
    });
    await flush();
    expect(document.querySelector(".qio-modal-body .body-x")).not.toBeNull();
    expect(document.querySelector(".qio-modal-actions .ok")).not.toBeNull();
    w.unmount();
  });

  it("圆角与进入动效来自唯一 token（面板不被写死半径）", () => {
    const sourcePath = "../QModal.vue";
    const source = readFileSync(new URL(sourcePath, import.meta.url), "utf8");
    expect(source).toContain("border-radius: var(--qio-ui-radius)");
    expect(source).toContain("qio-pop-in");
    expect(source).toContain("qio-modal-scrim");
  });
});

describe("QModal 关闭路径", () => {
  it("关闭按钮 / Esc / 外部点击都发 close，且同一次打开只发一次", async () => {
    const w = mountModal();
    await flush();
    (document.querySelector(".qio-modal-head__slot--end button") as HTMLElement).click();
    expect(w.emitted("close")).toHaveLength(1);

    pressEscape();
    document.querySelector(".qio-modal-scrim")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await flush();
    // 已发过就不再重复：父级不会收到重复 close
    expect(w.emitted("close")).toHaveLength(1);
    w.unmount();
  });

  it("dismissible=false：不渲染关闭按钮，Esc 与外部点击都不关闭", async () => {
    const w = mountModal({ dismissible: false });
    await flush();
    expect(document.querySelector(".qio-modal-head__slot--end button")).toBeNull();
    pressEscape();
    document.querySelector(".qio-modal-scrim")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await flush();
    expect(w.emitted("close")).toBeUndefined();
    w.unmount();
  });

  it("busy：关闭按钮禁用，Esc/外部点击都不关闭（防关闭中的重复提交）", async () => {
    const w = mountModal({ busy: true });
    await flush();
    const close = document.querySelector(".qio-modal-head__slot--end button") as HTMLButtonElement;
    expect(close.disabled).toBe(true);
    expect(close.getAttribute("aria-busy")).toBe("true");
    pressEscape();
    document.querySelector(".qio-modal-scrim")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await flush();
    expect(w.emitted("close")).toBeUndefined();
    w.unmount();
  });
});

describe("QModal 焦点", () => {
  it("打开把焦点放进对话层，关闭归还给触发元素", async () => {
    const trigger = document.createElement("button");
    trigger.textContent = "打开";
    document.body.appendChild(trigger);
    trigger.focus();

    const w = mount(QModal, {
      props: { open: false, title: "标题", modalId: "modal-3" },
      attachTo: document.body,
    });
    await w.setProps({ open: true });
    await flush();
    expect(document.activeElement).toBe(dialogEl());

    await w.setProps({ open: false });
    await flush();
    expect(document.activeElement).toBe(trigger);
    w.unmount();
  });

  it("不抢输入焦点：用户正在输入时弹窗不移动焦点", async () => {
    const input = document.createElement("textarea");
    document.body.appendChild(input);
    input.focus();
    expect(document.activeElement).toBe(input);

    const w = mount(QModal, {
      props: { open: true, title: "标题", modalId: "modal-4" },
      attachTo: document.body,
    });
    await flush();
    expect(document.activeElement).toBe(input);
    w.unmount();
  });

  it("Tab 在对话层内循环，焦点不会跑到页面底下", async () => {
    const outside = document.createElement("button");
    outside.textContent = "页面按钮";
    document.body.appendChild(outside);

    const w = mount(QModal, {
      props: { open: true, title: "标题", modalId: "modal-5" },
      slots: { footer: "<button class='b1'>取消</button><button class='b2'>确定</button>" },
      attachTo: document.body,
    });
    await flush();
    const dialog = dialogEl()!;
    const focusables = [...dialog.querySelectorAll("button")];
    // 焦点从最后一个按钮 Tab → 回到第一个（关闭按钮）
    focusables[focusables.length - 1].focus();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab" }));
    expect(document.activeElement).toBe(focusables[0]);
    // 焦点在外部时 Tab → 拉回对话层
    outside.focus();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab" }));
    expect(dialog.contains(document.activeElement)).toBe(true);
    w.unmount();
  });

  it("打开时记录焦点、卸载时清理监听（不再响应 Esc）", async () => {
    const w = mountModal();
    await flush();
    w.unmount();
    pressEscape();
    expect(w.emitted("close")).toBeUndefined();
  });
});

/**
 * 接入 B 的弹窗仲裁（2026-10-27）：
 * 同一时刻只允许一个中央弹窗；被挡住时不显示、等挡路者关闭后自动显示；
 * 关闭询问等 suspendAll 期间用 v-show 隐藏（DOM/草稿保留），键盘让给更高层。
 */
describe("QModal 与弹窗仲裁", () => {
  it("更高优先级弹窗挡着时不显示；挡路者关闭后自动显示并注册", async () => {
    setActivePinia(createPinia());
    const arbiter = useModalArbiterStore();
    expect(arbiter.open({ id: "impact-1", kind: "impact" })).toBe(true);

    const w = mount(QModal, {
      props: { open: true, title: "影响确认", modalId: "modal-impact" },
      attachTo: document.body,
    });
    await flush();
    // 不得强行显示
    expect(dialogEl()).toBeNull();
    expect(arbiter.isOpen("modal-impact")).toBe(false);

    arbiter.close("impact-1");
    await flush();
    expect(dialogEl()).not.toBeNull();
    expect(arbiter.isOpen("modal-impact")).toBe(true);

    await w.setProps({ open: false });
    await flush();
    expect(arbiter.isOpen("modal-impact")).toBe(false);
    w.unmount();
  });

  it("已显示时更高优先级弹窗打开：v-show 隐藏保留 DOM，对方关闭后恢复", async () => {
    setActivePinia(createPinia());
    const arbiter = useModalArbiterStore();
    const w = mount(QModal, {
      props: { open: true, title: "确认", modalId: "modal-low" },
      attachTo: document.body,
    });
    await flush();
    const scrim = document.querySelector(".qio-modal-scrim") as HTMLElement;
    expect(scrim.style.display).not.toBe("none");

    // 影响确认（优先级 40）晚于确认层（other=0）打开：低优先级只隐藏不卸载
    expect(arbiter.open({ id: "impact-later", kind: "impact" })).toBe(true);
    await flush();
    expect(scrim.style.display).toBe("none");
    expect(dialogEl()).not.toBeNull();

    arbiter.close("impact-later");
    await flush();
    expect(scrim.style.display).not.toBe("none");
    w.unmount();
  });

  it("suspendAll 期间 v-show 隐藏但保留 DOM；resumeAll 恢复，挂起时 Esc 不关闭", async () => {
    setActivePinia(createPinia());
    const arbiter = useModalArbiterStore();
    const w = mount(QModal, {
      props: { open: true, title: "标题", modalId: "modal-suspended" },
      attachTo: document.body,
    });
    await flush();
    const scrim = document.querySelector(".qio-modal-scrim") as HTMLElement;
    expect(scrim).not.toBeNull();
    expect(scrim.style.display).not.toBe("none");

    arbiter.suspendAll("close-ask");
    await flush();
    expect(scrim.style.display).toBe("none");
    expect(dialogEl()).not.toBeNull();

    pressEscape();
    await flush();
    expect(w.emitted("close")).toBeUndefined();

    arbiter.resumeAll();
    await flush();
    expect(scrim.style.display).not.toBe("none");
    w.unmount();
    expect(arbiter.stack.length).toBe(0);
  });
});