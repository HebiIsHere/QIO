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
import QConfirm from "../QConfirm.vue";

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
    const buttons = document.querySelectorAll("button");
    expect(document.activeElement).toBe(buttons[0]);
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
