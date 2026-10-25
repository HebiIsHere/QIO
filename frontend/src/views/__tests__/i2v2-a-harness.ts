/**
 * A 组（F01/F02/F03/F05/F06）页面级行为测试的公共挂载夹具。
 *
 * 目的：把真实的 InteractiveView.vue + 真实的 v2 组件树挂起来，只把「画布 / 会话流 / 输入框」
 * 这些与本次接线无关的重型子组件替换成一样会渲染 slot 的轻量替身 —— 这样断言的是页面
 * 真实的 DOM 行为（按钮点开的是不是真界面、内容是否来自真实 store 事实），
 * 而不是源码字符串。
 */
import { mount, type VueWrapper } from "@vue/test-utils";
import InteractiveView from "../InteractiveView.vue";

/** 只需保留 slot 语义的替身：板面画布必须把 overlay slot 渲染出来（虚拟意图层挂在那里）。 */
export const VIEW_STUBS = {
  BoardCanvas: {
    template: '<div data-stub="canvas"><slot name="overlay" :scale="1" :width="1200" :height="800" /></div>',
  },
  BoardToolbar: { template: '<div data-stub="toolbar" />' },
  ConversationModeSwitch: { template: '<div data-stub="mode-switch" />' },
  MessageStream: { template: '<div data-stub="stream" />' },
  Composer: { template: '<div data-stub="composer" />' },
} as const;

export function mountInteractiveView(): VueWrapper {
  return mount(InteractiveView, {
    attachTo: document.body,
    global: { stubs: VIEW_STUBS as never },
  }) as unknown as VueWrapper;
}

/** 按 data-im 找元素（页面里所有可交互出口都带这个属性）。 */
export function byIm(wrapper: VueWrapper, name: string) {
  return wrapper.findAll(`[data-im="${name}"]`);
}

export function textOf(wrapper: VueWrapper, name: string): string {
  return byIm(wrapper, name)
    .map((item) => item.text())
    .join(" ");
}

/** 触发一次 visibilitychange（切换页面 / 回到前台的真实入口）。 */
export function setVisibility(state: "visible" | "hidden"): void {
  Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
  document.dispatchEvent(new Event("visibilitychange"));
}
