/**
 * 设置页组件测试的公共工具（不是测试文件本身：文件名不含 .test.ts，不会被 vitest 收集）。
 */
import { flushPromises, mount, type DOMWrapper, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { nextTick } from "vue";
import SettingsView from "../../SettingsView.vue";

export function makePinia(): Pinia {
  const pinia = createPinia();
  setActivePinia(pinia);
  return pinia;
}

export async function mountSettings(pinia?: Pinia): Promise<VueWrapper> {
  const w = mount(SettingsView, {
    attachTo: document.body,
    global: { plugins: [pinia ?? makePinia()], stubs: { RouterLink: true } },
  });
  await flushPromises();
  return w;
}

/** 当前可见的分区（其余分区用 v-show 保留 DOM） */
export function visibleSection(w: VueWrapper | Queryable): HTMLElement {
  const sections = w.findAll("[data-settings-section]");
  const shown = sections.find(
    (section) => (section.element as HTMLElement).style.display !== "none",
  );
  if (!shown) throw new Error("没有可见的设置分区");
  return shown.element as HTMLElement;
}

/** 既能接 VueWrapper 也能接 DOMWrapper 的查询面（两者都提供 findAll） */
export interface Queryable {
  findAll(selector: string): DOMWrapper<Element>[];
  find(selector: string): DOMWrapper<Element>;
}

export function sectionById(w: Queryable, id: string): DOMWrapper<Element> {
  const found = w.findAll(`[data-settings-section="${id}"]`);
  if (!found.length) throw new Error(`找不到分区 ${id}`);
  return found[0];
}

export async function openCategory(w: VueWrapper, id: string): Promise<void> {
  const tab = w.find(`[data-settings-nav-tab="${id}"]`);
  expectExists(tab, `分类标签 ${id}`);
  await tab.trigger("click");
  await flushPromises();
}

export async function clickSegLabel(root: Queryable, label: string): Promise<void> {
  const options = root.findAll(".set-seg__opt");
  const target = options.find((option) => option.text().includes(label));
  expectExists(target, `分段选项 ${label}`);
  await target!.trigger("click");
  await flushPromises();
}

/** 点一个自绘分段选项（按文案匹配） */
export async function chooseSelectLabel(root: Queryable, label: string): Promise<void> {
  await clickSegLabel(root, label);
}

export async function chooseSelectOption(root: Queryable, label: string): Promise<void> {
  await root.find(".qio-select").trigger("click");
  await nextTick();
  const option = root.findAll(".qio-select-menu .opt").find((item) => item.text().includes(label));
  expectExists(option, `下拉选项 ${label}`);
  await option!.trigger("click");
  await nextTick();
}

export function settingKeys(root: Element): string[] {
  return Array.from(root.querySelectorAll<HTMLElement>("[data-settings-key]")).map(
    (node) => node.dataset.settingsKey ?? "",
  );
}

export function expectExists(value: unknown, what: string): asserts value {
  if (!value) throw new Error(`找不到 ${what}`);
}

/** 让两层 requestAnimationFrame / setTimeout 兜底都跑完（搜索定位用） */
export async function flushFrames(): Promise<void> {
  await nextTick();
  await new Promise((resolve) => setTimeout(resolve, 40));
  await nextTick();
  await flushPromises();
}

export function setViewportWidth(width: number): void {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
  window.dispatchEvent(new Event("resize"));
}
