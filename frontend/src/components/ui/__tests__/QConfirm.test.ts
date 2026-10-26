/**
 * 确认层（第四阶段）。
 *
 * 它取代的是浏览器原生 `window.confirm`，所以这里守的是三件原生框给不了的事：
 * 1) 说清「动作 + 影响 + 后果」（detail 必须能表达出来）；
 * 2) 按危险程度分档（inline / popover / layer），危险档确认按钮不用品牌主操作色；
 * 3) 键盘可用：Esc 取消、焦点先落在「取消」、关闭后焦点归还触发元素。
 */
import { describe, expect, it, afterEach } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { readFileSync } from "node:fs";
import { createPinia, setActivePinia } from "pinia";
import QConfirm from "../QConfirm.vue";
import { useModalArbiterStore } from "../../../stores/modalArbiter";

/**
 * 对称布局（2026-10-26 统一改造）。
 *
 * jsdom 不做排版（所有 getBoundingClientRect 都是 0），所以这里守两件事：
 * ① 结构：三列槽位左右各一、标题在中间、左槽不可聚焦；
 * ② 样式来源：标题居中靠 base.css 的等宽三列 grid（不是空格/全角空格凑），
 *    按钮组等宽同高、窄窗纵排也由共享类提供。
 * 真实像素偏差（左右槽内容不同时标题容器中心与组件中心 ≤1px）在
 * ui-catalog 的 Playwright 截图脚本里用真实 DOM rect 复核（见证据文件）。
 */
describe("QConfirm 对称布局与共享弹窗骨架", () => {
  it("layer 档：三列等宽标题栏、标题居中、左槽只占位", () => {
    const w = mount(QConfirm, { props: { open: true, title: "删除这项凭据？", variant: "layer" } });
    const head = w.find(".qio-modal-head");
    expect(head.exists()).toBe(true);
    expect(head.element.children.length).toBe(3);
    const [left, middle, right] = [...head.element.children] as HTMLElement[];
    expect(left.classList.contains("qio-modal-head__slot")).toBe(true);
    expect(left.getAttribute("aria-hidden")).toBe("true");
    expect(left.querySelectorAll("button, a, input, [tabindex]").length).toBe(0);
    expect(middle.contains(w.find(".qio-modal-title").element)).toBe(true);
    expect(w.find("#qio-confirm-title").text()).toBe("删除这项凭据？");
    expect(right.querySelector("button")?.getAttribute("aria-label")).toBeTruthy();
    w.unmount();
  });

  it("inline / popover 没有关闭按钮，但右槽仍等宽占位（标题不被挤偏）", () => {
    for (const variant of ["inline", "popover"] as const) {
      const w = mount(QConfirm, { props: { open: true, title: "标题", variant } });
      const head = w.find(".qio-modal-head");
      expect(head.element.children.length, variant).toBe(3);
      const right = head.element.children[2] as HTMLElement;
      expect(right.querySelector("button"), variant).toBeNull();
      expect(right.getAttribute("aria-hidden"), variant).toBe("true");
      w.unmount();
    }
  });

  it("layer 档关闭按钮走“取消”语义（不是执行）", async () => {
    const w = mount(QConfirm, { props: { open: true, title: "删除？", variant: "layer" } });
    const close = w.find("[data-qio-confirm-close]");
    expect(close.exists()).toBe(true);
    expect(close.attributes("title")).toBeUndefined();
    await close.trigger("click");
    expect(w.emitted("cancel")).toHaveLength(1);
    expect(w.emitted("confirm")).toBeUndefined();
    w.unmount();
  });

  it("动作按钮组：共享 .qio-modal-actions、两颗按钮同构且带稳定数据属性", () => {
    const w = mount(QConfirm, { props: { open: true, title: "归档？", variant: "inline" } });
    const actions = w.find(".qio-modal-actions");
    expect(actions.exists()).toBe(true);
    const buttons = actions.findAll("button");
    expect(buttons).toHaveLength(2);
    expect(buttons[0].attributes("data-qio-confirm-action")).toBe("cancel");
    expect(buttons[1].attributes("data-qio-confirm-action")).toBe("confirm");
    for (const b of buttons) expect(b.classes()).toContain("qio-btn");
    w.unmount();
  });

  it("居中/等宽/窄窗纵排由 base.css 的共享规则提供（不靠空格或固定坐标）", () => {
    const cssPath = "../../../styles/base.css";
    const css = readFileSync(new URL(cssPath, import.meta.url), "utf8");
    expect(css).toMatch(
      /\.qio-modal-head\{[^}]*grid-template-columns:minmax\(0,1fr\) minmax\(0,2\.4fr\) minmax\(0,1fr\)/,
    );
    expect(css).toMatch(/\.qio-modal-title\{[^}]*text-align:center/);
    expect(css).toMatch(/\.qio-modal-actions > \*\{[^}]*flex:1 1 0[^}]*height:36px/);
    expect(css).toMatch(/@media \(max-width:480px\)[\s\S]{0,200}?\.qio-modal-actions\{flex-direction:column/);
    // 圆角统一 token
    expect(css).toMatch(/\.qio-confirm--layer\{[^}]*border-radius:var\(--qio-ui-radius\)/);
  });
});

afterEach(() => {
  document.body.innerHTML = "";
});

describe("QConfirm 基本行为", () => {
  it("显示标题、影响说明与两个动作按钮", () => {
    const w = mount(QConfirm, {
      props: {
        open: true,
        title: "归档这条知识？",
        detail: "归档后不再参与回答，可在知识面板里看到它已归档。",
        confirmText: "归档",
        cancelText: "取消",
      },
    });
    expect(w.text()).toContain("归档这条知识？");
    expect(w.text()).toContain("不再参与回答");
    const buttons = w.findAll("button");
    expect(buttons.map((b) => b.text())).toEqual(["取消", "归档"]);
  });

  it("确认与取消分别发出 confirm / cancel，且自己不改变任何状态", async () => {
    const w = mount(QConfirm, { props: { open: true, title: "确定？" } });
    const [cancelBtn, confirmBtn] = w.findAll("button");
    await confirmBtn.trigger("click");
    expect(w.emitted("confirm")).toHaveLength(1);
    expect(w.emitted("cancel")).toBeUndefined();
    await cancelBtn.trigger("click");
    expect(w.emitted("cancel")).toHaveLength(1);
  });

  it("未打开时不渲染任何内容", () => {
    const w = mount(QConfirm, { props: { open: false, title: "确定？" } });
    expect(w.find(".qio-confirm").exists()).toBe(false);
  });

  it("危险动作的确认按钮用中性实心，不用品牌主操作色", () => {
    const w = mount(QConfirm, {
      props: { open: true, title: "删除凭据？", tone: "danger", confirmText: "删除" },
    });
    const confirmBtn = w.findAll("button")[1];
    expect(confirmBtn.classes()).toContain("danger-solid");
    expect(confirmBtn.classes()).not.toContain("primary");
  });
});

describe("QConfirm 三档形态", () => {
  it("inline 档就地展开，不是模态层", () => {
    const w = mount(QConfirm, { props: { open: true, title: "归档？", variant: "inline" } });
    expect(w.find(".qio-confirm--inline").exists()).toBe(true);
    expect(w.find('[role="dialog"]').exists()).toBe(false);
    expect(w.find(".qio-confirm-scrim").exists()).toBe(false);
  });

  it("layer 档是模态对话层：role/aria 齐全、有遮罩", () => {
    const w = mount(QConfirm, {
      props: { open: true, title: "删除凭据？", variant: "layer", titleId: "del-title" },
    });
    const dialog = w.find('[role="dialog"]');
    expect(dialog.exists()).toBe(true);
    expect(dialog.attributes("aria-modal")).toBe("true");
    expect(dialog.attributes("aria-labelledby")).toBe("del-title");
    expect(w.find("#del-title").text()).toBe("删除凭据？");
    expect(w.find(".qio-confirm-scrim").exists()).toBe(true);
  });
});

describe("QConfirm 键盘与焦点", () => {
  it("Esc 取消", async () => {
    const w = mount(QConfirm, { props: { open: true, title: "确定？", variant: "layer" } });
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await nextTick();
    expect(w.emitted("cancel")).toHaveLength(1);
    expect(w.emitted("confirm")).toBeUndefined();
  });

  it("layer 档打开时焦点先落在「取消」上（危险动作不能被一次回车执行）", async () => {
    const w = mount(
      {
        components: { QConfirm },
        template: `<QConfirm :open="open" title="删除凭据？" variant="layer" />`,
        data: () => ({ open: false }),
      },
      { attachTo: document.body },
    );
    (w.vm as unknown as { open: boolean }).open = true;
    await nextTick();
    await nextTick();
    // layer 档新增了右上关闭图标按钮，DOM 里第一个按钮不再是“取消”；
    // 但语义不变：焦点必须落在取消上（危险动作不能被一次回车执行）。
    const cancel = document.querySelector('[data-qio-confirm-action="cancel"]');
    expect(document.activeElement).toBe(cancel);
    w.unmount();
  });

  it("关闭后焦点归还给触发元素", async () => {
    const trigger = document.createElement("button");
    trigger.textContent = "删除";
    document.body.appendChild(trigger);
    trigger.focus();
    expect(document.activeElement).toBe(trigger);

    const w = mount(QConfirm, { props: { open: true, title: "确定？", variant: "layer" } });
    await nextTick();
    await w.setProps({ open: false });
    await nextTick();
    expect(document.activeElement).toBe(trigger);
    w.unmount();
  });
});

/**
 * layer 档接入 B 的弹窗仲裁（2026-10-27）：
 * 审批等更高优先级弹窗在时不得显示；让位后恢复显示并注册 kind=confirm；
 * inline / popover 是就地控件，不占中央弹窗层。
 */
describe("QConfirm 仲裁（仅 layer 档是中央弹窗）", () => {
  it("更高优先级弹窗挡着时 layer 不渲染；让位后渲染并注册 confirm", async () => {
    setActivePinia(createPinia());
    const arbiter = useModalArbiterStore();
    arbiter.open({ id: "approval-x", kind: "approval" });

    const w = mount(QConfirm, {
      props: { open: true, title: "删除这项凭据？", variant: "layer" },
      attachTo: document.body,
    });
    await nextTick();
    await nextTick();
    expect(document.querySelector('[role="dialog"]')).toBeNull();

    arbiter.close("approval-x");
    await nextTick();
    await nextTick();
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(arbiter.stack.some((d) => d.kind === "confirm")).toBe(true);

    w.unmount();
    await nextTick();
    expect(arbiter.stack.length).toBe(0);
  });

  it("inline / popover 不注册中央弹窗", async () => {
    setActivePinia(createPinia());
    const arbiter = useModalArbiterStore();
    for (const variant of ["inline", "popover"] as const) {
      const w = mount(QConfirm, { props: { open: true, title: "归档？", variant } });
      await nextTick();
      expect(arbiter.stack.length, variant).toBe(0);
      w.unmount();
    }
  });
});
