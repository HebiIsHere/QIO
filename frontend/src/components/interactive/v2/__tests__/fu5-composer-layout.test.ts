/**
 * FU5 布局守卫 2：互动页输入列真正居中、模式切换在左侧且不占输入列宽度。
 *
 * 现场：.ic-composer-row 是 flex（max-width:860 + margin:auto），第一个 flex item 是
 * ConversationModeSwitch.im-mode-switch，占掉宽度后输入列整体右偏。
 *
 * 新结构（Lead 指定）：三列 grid
 *   minmax(0, 1fr) | minmax(0, 860px) | minmax(0, 1fr)
 * switch 在第一列 justify-self:end（紧贴输入列左侧、不占输入列宽度），
 * 输入列在第二列；窄窗（≤700）改为单列：switch 在上、输入在下且左对齐。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { defineComponent, h } from "vue";
import InteractiveConversation from "../InteractiveConversation.vue";

const SRC = resolve(process.cwd(), "src");
const conversation = readFileSync(
  resolve(SRC, "components/interactive/v2/InteractiveConversation.vue"),
  "utf8",
);
const view = readFileSync(resolve(SRC, "views/InteractiveView.vue"), "utf8");

function block(selector: string): string {
  const start = conversation.indexOf(selector + " {");
  if (start < 0) return "";
  const end = conversation.indexOf("\n}", start);
  return end < 0 ? "" : conversation.slice(start, end + 2);
}

function viewBlock(selector: string): string {
  const start = view.indexOf(selector + " {");
  if (start < 0) return "";
  const end = view.indexOf("\n}", start);
  return end < 0 ? "" : view.slice(start, end + 2);
}

describe("输入列居中：三列 grid，模式切换不占输入列宽度", () => {
  it(".ic-composer-row 是三列 grid，中间列最大 860px", () => {
    const row = block(".ic-composer-row");
    expect(row, "找不到 .ic-composer-row 样式块").toContain("display: grid");
    expect(row.replace(/\s+/g, " ")).toContain(
      "grid-template-columns: minmax(0, 1fr) minmax(0, 860px) minmax(0, 1fr)",
    );
  });

  it("输入列固定在第二列；switch 在第一列靠右（不占输入列宽度）", () => {
    const input = block(".ic-composer-input");
    expect(input).toContain("grid-column: 2");
    expect(input).toContain("min-width: 0");

    const slotRuleIndex = conversation.indexOf(":slotted(.im-mode-switch)");
    expect(slotRuleIndex, "缺少针对模式切换槽位的布局规则").toBeGreaterThan(-1);
    const slotRule = conversation.slice(slotRuleIndex, slotRuleIndex + 260);
    expect(slotRule).toContain("grid-column: 1");
    expect(slotRule).toContain("justify-self: end");
  });

  it("窄窗（≤700）单列：switch 在输入上方且左对齐", () => {
    const mediaIndex = conversation.indexOf("@media (max-width: 700px)");
    expect(mediaIndex, "缺少 ≤700 窄窗规则").toBeGreaterThan(-1);
    const narrow = conversation.slice(mediaIndex, mediaIndex + 420);
    expect(narrow).toContain(".ic-composer-row");
    expect(narrow.replace(/\s+/g, " ")).toContain("grid-template-columns: minmax(0, 1fr)");
    expect(narrow).toContain("justify-self: start");
  });

  it("外层不再用 860 限制整行：.ic-composer 与 InteractiveView 的 deep 覆盖都铺满面板", () => {
    const composer = block(".ic-composer");
    expect(composer).toContain("width: 100%");
    expect(composer).toContain("max-width: none");
    expect(composer).not.toContain("max-width: 860px");

    const deepComposer = viewBlock(".im-floating-chat :deep(.ic-composer)");
    expect(deepComposer, "找不到 InteractiveView 的 .ic-composer 覆盖").not.toBe("");
    expect(deepComposer).not.toContain("max-width: 860px");
    expect(deepComposer).toContain("max-width: none");

    // 消息列仍是 860 内容列（只改输入列外层宽度，不改对话流列宽）
    expect(viewBlock(".im-floating-chat :deep(.ic-stream)")).toContain("max-width: 860px");
  });
});

describe("DOM 结构：模式切换是输入列的兄弟，不在输入列内部", () => {
  const StreamStub = defineComponent({ name: "MessageStream", setup: () => () => h("div") });
  const EntriesStub = defineComponent({ name: "ComposerEntries", setup: () => () => h("div") });
  const ComposerStub = defineComponent({
    name: "Composer",
    setup: () => () => h("div", { "data-stub": "composer" }),
  });

  it("composer-leading 槽位渲染为行的第一个子节点；输入列是第二个子节点", () => {
    setActivePinia(createPinia());
    const w = mount(InteractiveConversation, {
      slots: {
        "composer-leading": '<span class="im-mode-switch" data-slot="mode">模式</span>',
      },
      global: {
        plugins: [createPinia()],
        stubs: { MessageStream: StreamStub, ComposerEntries: EntriesStub, Composer: ComposerStub },
      },
    });

    const row = w.get(".ic-composer-row");
    const children = Array.from(row.element.children);
    expect(children[0]?.className).toContain("im-mode-switch");
    expect(children[1]?.className).toContain("ic-composer-input");
    expect(w.find(".ic-composer-input .im-mode-switch").exists()).toBe(false);
    expect(w.find(".ic-composer-input [data-stub='composer']").exists()).toBe(true);
    w.unmount();
  });
});
