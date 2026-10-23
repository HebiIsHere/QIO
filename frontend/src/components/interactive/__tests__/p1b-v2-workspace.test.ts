/**
 * Phase 1-B 互动页 v2 布局外壳（子智能体 p1-layout 负责）。
 *
 * 只测**结构契约**，不绑定任何业务数据：
 * - 外壳本身不输出标题栏、不输出业务文案（不造假数据）；
 * - 区域由 slot 提供；没有 slot 就不渲染对应区域；
 * - 书签栏在桌面右侧 / 窄窗口下侧，通过 data 属性与 CSS 变量交给样式；
 * - 对话区只在打开且有内容时出现，向上展开的高度上限来自布局纯函数。
 */
import { afterEach, describe, expect, it } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { defineComponent, h, nextTick, ref } from "vue";
import InteractiveWorkspace from "../v2/InteractiveWorkspace.vue";
import { GAP, resolveWorkspaceLayout, type WorkspaceLayout } from "../v2/layout";

function domRect(width: number, height: number): DOMRect {
  return {
    x: 0,
    y: 0,
    left: 0,
    top: 0,
    right: width,
    bottom: height,
    width,
    height,
    toJSON: () => ({}),
  } as DOMRect;
}

const allSlots = {
  board: '<div class="stub-board">板面</div>',
  toolbar: '<div class="stub-toolbar">工具栏</div>',
  composer: '<div class="stub-composer">输入区</div>',
  bookmarks: '<div class="stub-bookmarks">书签</div>',
  chat: '<div class="stub-chat">对话</div>',
};

let wrapper: VueWrapper | null = null;

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
});

function regionHooks(mounted: VueWrapper): string[] {
  return mounted
    .findAll("[data-iw-region]")
    .map((item) => item.attributes("data-iw-region") ?? "");
}

async function setSize(
  mounted: VueWrapper,
  width: number,
  height: number,
): Promise<WorkspaceLayout> {
  const inner = mounted.get(".iw-inner").element as HTMLElement;
  inner.getBoundingClientRect = () => domRect(width, height);
  (mounted.vm as unknown as { measure: () => void }).measure();
  await nextTick();
  return (mounted.vm as unknown as { layout: WorkspaceLayout }).layout;
}

describe("InteractiveWorkspace 结构", () => {
  it("空外壳只保留板面主区：没有第二标题栏、没有业务文案", () => {
    wrapper = mount(InteractiveWorkspace);
    expect(wrapper.find("header").exists()).toBe(false);
    expect(wrapper.find("h1").exists()).toBe(false);
    expect(wrapper.find("[role='banner']").exists()).toBe(false);
    expect(wrapper.text()).toBe("");
    expect(regionHooks(wrapper)).toEqual(["board"]);
  });

  it("没有对应 slot 时不渲染底部工具栏 / 输入区 / 书签栏 / 对话区", () => {
    wrapper = mount(InteractiveWorkspace, { slots: { board: '<div class="stub-board">板面</div>' } });
    expect(regionHooks(wrapper)).toEqual(["board"]);
  });

  it("slot 内容只出现在自己的区域里", () => {
    wrapper = mount(InteractiveWorkspace, { slots: allSlots });
    expect(wrapper.find('[data-iw-region="board"] .stub-board').exists()).toBe(true);
    expect(wrapper.find('[data-iw-region="toolbar"] .stub-toolbar').exists()).toBe(true);
    expect(wrapper.find('[data-iw-region="composer"] .stub-composer').exists()).toBe(true);
    expect(wrapper.find('[data-iw-region="board"] .stub-toolbar').exists()).toBe(false);
  });
});

describe("InteractiveWorkspace 响应式布局", () => {
  it("桌面宽档：书签栏是右侧窄栏，板面宽度扣掉它，绝不覆盖", async () => {
    wrapper = mount(InteractiveWorkspace, { slots: allSlots });
    const layout = await setSize(wrapper, 1280, 800);

    expect(wrapper.attributes("data-iw-band")).toBe("wide");
    expect(wrapper.attributes("data-iw-bookmark-dock")).toBe("side");
    const bookmarks = wrapper.get('[data-iw-region="bookmarks"]');
    expect(bookmarks.classes()).toContain("iw-bookmarks--side");
    expect(bookmarks.attributes("aria-label")).toBe("书签栏");

    expect(layout.bookmarkSize).toBe(205);
    expect(layout.board.w).toBe(1280 - 205 - GAP);
    expect(layout.board.h).toBe(800 - 64 - GAP - 48 - GAP);
    // 宽度 / 高度上限交给样式（只用 var(--*) 取用）
    expect(wrapper.attributes("style")).toContain("--iw-bookmark-size: 205px");
    expect(wrapper.attributes("style")).toContain("--iw-gap: " + GAP + "px");
  });

  it("窄窗口：书签栏改为受限高度的下侧区域，板面宽度不再被挤", async () => {
    wrapper = mount(InteractiveWorkspace, { slots: allSlots });
    const layout = await setSize(wrapper, 720, 800);

    expect(wrapper.attributes("data-iw-band")).toBe("narrow");
    expect(wrapper.attributes("data-iw-bookmark-dock")).toBe("bottom");
    const bookmarks = wrapper.get('[data-iw-region="bookmarks"]');
    expect(bookmarks.classes()).toContain("iw-bookmarks--bottom");
    expect(wrapper.attributes("style")).toContain("--iw-bookmark-size: 144px");
    expect(layout.board.w).toBe(720);
    expect(layout.bookmarks).toEqual({ x: 0, y: 520, w: 720, h: 144 });
  });

  it("对话区关闭时不占位；打开时位于输入区一侧、有高度上限", async () => {
    wrapper = mount(InteractiveWorkspace, { slots: allSlots });
    let layout = await setSize(wrapper, 1280, 800);
    expect(layout.chat).toBeNull();
    expect(wrapper.find('[data-iw-region="chat"]').exists()).toBe(false);

    await wrapper.setProps({ chatOpen: true });
    layout = await setSize(wrapper, 1280, 800);
    expect(layout.chat).not.toBeNull();
    const chat = wrapper.get('[data-iw-region="chat"]');
    expect(chat.attributes("aria-label")).toBe("对话区");
    expect(wrapper.attributes("style")).toContain("--iw-chat-max-h: " + layout.chatMaxHeight + "px");
    expect(layout.chatMaxHeight).toBeGreaterThan(0);
    // 面板在 DOM 中位于板面之后、工具栏之前（向上展开的落点）
    const hooks = regionHooks(wrapper);
    expect(hooks.indexOf("chat")).toBeGreaterThan(hooks.indexOf("board"));
    expect(hooks.indexOf("chat")).toBeLessThan(hooks.indexOf("toolbar"));
    expect(hooks.indexOf("toolbar")).toBeLessThan(hooks.indexOf("composer"));
  });

  it("书签栏收起时不渲染区域，板面恢复整宽", async () => {
    wrapper = mount(InteractiveWorkspace, { slots: allSlots });
    await wrapper.setProps({ bookmarksOpen: false });
    const layout = await setSize(wrapper, 1280, 800);
    expect(wrapper.find('[data-iw-region="bookmarks"]').exists()).toBe(false);
    expect(layout.board.w).toBe(1280);
  });

  it("对话区 prop 为 true 但没有 slot 时不渲染空对话区", async () => {
    wrapper = mount(InteractiveWorkspace, {
      props: { chatOpen: true },
      slots: { board: '<div class="stub-board">板面</div>', toolbar: '<div>t</div>', composer: '<div>c</div>' },
    });
    await setSize(wrapper, 1280, 800);
    expect(wrapper.find('[data-iw-region="chat"]').exists()).toBe(false);
    expect(wrapper.find('[data-iw-region="bookmarks"]').exists()).toBe(false);
  });

  it("父级后来才提供 slot 时，对应区域会出现（不是只在首帧判断）", async () => {
    const showToolbar = ref(false);
    const Harness = defineComponent({
      setup() {
        return () =>
          h(InteractiveWorkspace, null, {
            board: () => h("div", { class: "stub-board" }, "板面"),
            composer: () => h("div", { class: "stub-composer" }, "输入区"),
            ...(showToolbar.value
              ? { toolbar: () => h("div", { class: "stub-toolbar" }, "工具栏") }
              : {}),
          });
      },
    });
    wrapper = mount(Harness);
    const workspace = wrapper.findComponent(InteractiveWorkspace);
    const inner = workspace.get(".iw-inner").element as HTMLElement;
    inner.getBoundingClientRect = () => domRect(1280, 800);
    (workspace.vm as unknown as { measure: () => void }).measure();
    await nextTick();
    expect(wrapper.find('[data-iw-region="toolbar"]').exists()).toBe(false);

    showToolbar.value = true;
    await nextTick();
    expect(wrapper.find('[data-iw-region="toolbar"]').exists()).toBe(true);
  });

  it("布局结果与布局纯函数同源（Lead 可据此校验）", async () => {
    wrapper = mount(InteractiveWorkspace, { slots: allSlots });
    const layout = await setSize(wrapper, 900, 700);
    expect(layout).toEqual(
      resolveWorkspaceLayout({
        width: 900,
        height: 700,
        bookmarksOpen: true,
        chatOpen: false,
        toolbarHeight: 48,
        composerHeight: 64,
      }),
    );
  });
});
