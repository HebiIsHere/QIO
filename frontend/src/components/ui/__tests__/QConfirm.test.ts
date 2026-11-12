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
import { join } from "node:path";
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
  it("layer 档：三列等宽标题栏、标题居中、左槽只占位", async () => {
    const w = mount(QConfirm, {
      props: { open: true, title: "删除这项凭据？", variant: "layer" },
      attachTo: document.body,
    });
    await nextTick();
    const head = document.querySelector(".qio-modal-head");
    expect(head).not.toBeNull();
    expect(head!.children.length).toBe(3);
    const [left, middle, right] = [...head!.children] as HTMLElement[];
    expect(left.classList.contains("qio-modal-head__slot")).toBe(true);
    expect(left.getAttribute("aria-hidden")).toBe("true");
    expect(left.querySelectorAll("button, a, input, [tabindex]").length).toBe(0);
    expect(middle.contains(document.querySelector(".qio-modal-title"))).toBe(true);
    expect(document.getElementById("qio-confirm-title")?.textContent?.trim()).toBe("删除这项凭据？");
    // 关闭按钮视觉在右上，但 DOM 顺序在动作按钮之后（保证既有调用方的按钮顺序不变）；
    // 右槽因此只承担等宽占位，不承载可聚焦控件。
    expect(right.querySelector("button")).toBeNull();
    expect(right.getAttribute("aria-hidden")).toBe("true");
    const close = document.querySelector("[data-qio-confirm-close]");
    expect(close).not.toBeNull();
    expect(close!.getAttribute("aria-label")).toBeTruthy();
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
    const w = mount(QConfirm, {
      props: { open: true, title: "删除？", variant: "layer" },
      attachTo: document.body,
    });
    await nextTick();
    const close = document.querySelector("[data-qio-confirm-close]") as HTMLElement | null;
    expect(close).not.toBeNull();
    expect(close!.getAttribute("title")).toBeNull();
    close!.click();
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

  it("layer 档是模态对话层：role/aria 齐全、有遮罩", async () => {
    const w = mount(QConfirm, {
      props: { open: true, title: "删除凭据？", variant: "layer", titleId: "del-title" },
      attachTo: document.body,
    });
    await nextTick();
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog).not.toBeNull();
    expect(dialog!.getAttribute("aria-modal")).toBe("true");
    expect(dialog!.getAttribute("aria-labelledby")).toBe("del-title");
    expect(document.getElementById("del-title")?.textContent?.trim()).toBe("删除凭据？");
    expect(document.querySelector(".qio-confirm-scrim")).not.toBeNull();
    w.unmount();
  });
});

/**
 * #82 / #87：设置页窗口级确认框。
 *
 * 根因（基线 7445170d 实测）：设置页 .set-main 有 backdrop-filter，会为 position:fixed
 * 后代创建包含块 + stacking context；layer 档就地渲染时遮罩 rect 变成 y=-1169 / height=2847，
 * 800x600 下确认框标题与关闭按钮落到 z-index 240 的 WindowChrome 下面（elementFromPoint
 * 命中 .drag-surface）。修法：layer 档 Teleport 到 body，与 QModal 一致；遮罩继续用
 * --window-frame-inset 定位到内容视口，面板高度上限取遮罩内容盒（max-height: 100%）。
 */
describe("QConfirm 窗口层（#82 / #87）", () => {
  it("layer 档 Teleport 到 body，面板/遮罩带窗口层钩子类", async () => {
    const w = mount(QConfirm, {
      props: { open: true, title: "删除凭据？", variant: "layer" },
      attachTo: document.body,
    });
    await nextTick();
    const scrim = document.querySelector(".qio-confirm-scrim") as HTMLElement | null;
    const panel = document.querySelector(".qio-confirm--layer") as HTMLElement | null;
    expect(scrim).not.toBeNull();
    expect(panel).not.toBeNull();
    // 脱离设置页的 backdrop-filter 包含块：Teleport 目标就是 body（VTU 的 Transition
    // stub 可能再包一层，因此用 body.contains 而不是直接比较 parentElement）
    expect(document.body.contains(scrim!)).toBe(true);
    expect(scrim!.closest("body")).toBe(document.body);
    expect(panel!.parentElement).toBe(scrim);
    expect(scrim!.getAttribute("data-qio-confirm-layer")).toBe("window");
    expect(panel!.getAttribute("data-qio-confirm-layer")).toBe("window");
    w.unmount();
  });

  it("inline / popover 仍就地渲染，不进入窗口层", async () => {
    for (const variant of ["inline", "popover"] as const) {
      const w = mount(QConfirm, { props: { open: true, title: "标题", variant } });
      const root = w.find(".qio-confirm");
      expect(root.exists(), variant).toBe(true);
      expect(root.attributes("data-qio-confirm-layer"), variant).toBeUndefined();
      expect(document.querySelector(".qio-confirm-scrim"), variant).toBeNull();
      w.unmount();
    }
  });

  it("危险确认按钮用真实危险令牌实心，且选择器不靠 !important 压过设置页普通按钮规则", () => {
    // 说明：这里要读源码本身（jsdom 不应用 SFC scoped 样式），因此按测试进程的 cwd 定位。
    const sourcePath = join(process.cwd(), "src", "components", "ui", "QConfirm.vue");
    const source = readFileSync(sourcePath, "utf8");
    // 危险态：既有令牌 + 明确类名 + data 钩子，选择器层级高于 .qio-settings .qio-btn.qio-btn
    expect(source).toMatch(
      /\.qio-confirm--layer\[data-qio-confirm-layer="window"\] \[data-qio-confirm-action="confirm"\]\.danger-solid\s*\{[^}]*background:\s*var\(--danger\)/,
    );
    // 注释里可以提到「不依赖 !important」；这里守的是没有任何一条声明真的用到它
    expect(source).not.toMatch(/!important\s*;/);
    // 高度上限相对内容视口（遮罩内容盒），不是写死 top/100vh 魔法数字
    expect(source).toMatch(/\.qio-confirm--layer\s*\{[^}]*max-height:\s*100%/);
    expect(source).not.toMatch(/max-height:\s*calc\(100vh/);
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

  it("layer 已显示时更高优先级弹窗打开：v-show 隐藏，让位后恢复", async () => {
    setActivePinia(createPinia());
    const arbiter = useModalArbiterStore();
    const w = mount(QConfirm, {
      props: { open: true, title: "删除？", variant: "layer" },
      attachTo: document.body,
    });
    await nextTick();
    await nextTick();
    const scrim = document.querySelector(".qio-confirm-scrim") as HTMLElement;
    expect(scrim.style.display).not.toBe("none");

    expect(arbiter.open({ id: "approval-later", kind: "approval" })).toBe(true);
    await nextTick();
    expect(scrim.style.display).toBe("none");
    // 弹层 DOM 还在（取消/确认按钮与文案不会丢）
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();

    arbiter.close("approval-later");
    await nextTick();
    await nextTick();
    expect(scrim.style.display).not.toBe("none");
    w.unmount();
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