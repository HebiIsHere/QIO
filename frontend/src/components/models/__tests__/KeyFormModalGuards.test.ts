/**
 * Key 表单弹窗关闭保护：问题 6（Esc 只关闭最内层）与问题 7（保存中不能关掉表单）。
 *
 * 守住的边界：
 * - 服务商下拉展开时一次 Esc 只收起列表，表单与已填内容保留；列表关闭后 Esc 才关表单；
 * - 保存请求未完成时，关闭图标 / 遮罩 / Esc 都不能让表单消失（表单被藏起来不等于取消），
 *   重复提交只发一次请求；成功后才关闭；
 * - 失败时表单保留、填写内容不丢、错误可读且不包含 Key 原文；失败后可以正常关闭；
 * - 保存期间不出现「表单已关闭但请求还在跑」被误读成取消的情况。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { nextTick } from "vue";
import KeyForm from "../KeyForm.vue";
import KeyAccessSection from "../KeyAccessSection.vue";
import type { CredentialMeta, ProviderPreset } from "../../../services/api";

const mocks = vi.hoisted(() => ({
  api: {
    listCredentialModels: vi.fn(async () => ({ models: [] as string[] })),
    createCredential: vi.fn(),
    updateCredentialMeta: vi.fn(),
    verifyCredential: vi.fn(),
    setCredentialEnabled: vi.fn(),
    revokeCredential: vi.fn(),
    deleteCredential: vi.fn(),
    getCredentialAudit: vi.fn(async () => ({ audit: [] })),
  },
  catalog: {
    discover: vi.fn(),
    getCatalog: vi.fn(async () => ({ connections: [], default: null, usages: [], discovery: {} })),
    verify: vi.fn(),
  },
}));

vi.mock("../../../services/api", () => {
  class ApiError extends Error {
    constructor(readonly status: number, readonly path: string, detail: string) {
      super(path + " -> " + status + ": " + detail);
      this.name = "ApiError";
    }
  }
  return { ApiError, api: mocks.api };
});
vi.mock("../../../services/modelCatalog", () => ({ modelCatalogApi: mocks.catalog }));

const SECRET = "sk-fake-modal-guard-secret";

const provider = (overrides: Partial<ProviderPreset> = {}): ProviderPreset => ({
  id: "deepseek",
  name: "DeepSeek",
  kind: "openai",
  base_url: "https://api.deepseek.com/v1",
  suggested_model: "deepseek-v4-flash",
  category: "official",
  category_label: "官方服务",
  note: "",
  ...overrides,
});
const PROVIDERS: ProviderPreset[] = [provider()];

function meta(overrides: Partial<CredentialMeta> = {}): CredentialMeta {
  return {
    key_id: "key_new",
    version: 1,
    tags: ["main-loop"],
    endpoint: "https://api.deepseek.com/v1",
    default_model: "deepseek-v4-flash",
    budget: null,
    budget_used: 0,
    status: "active",
    enabled: true,
    note: "DeepSeek",
    kind: "openai",
    verify_state: "unverified",
    provider_id: "deepseek",
    provider_name: "DeepSeek",
    ...overrides,
  };
}

function discoveryResult(overrides: Record<string, unknown> = {}) {
  return {
    credentialId: "key_new",
    providerId: "deepseek",
    providerName: "DeepSeek",
    source: "provider",
    models: [],
    page: 1,
    pageSize: 50,
    total: 0,
    hasMore: false,
    error: null,
    reasonCode: null,
    stale: false,
    publicCatalog: false,
    note: "",
    ...overrides,
  };
}

const mounted: VueWrapper[] = [];
afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount();
  mounted.length = 0;
  document.body.innerHTML = "";
});
beforeEach(() => {
  vi.clearAllMocks();
});

function mountSection() {
  const wrapper = mount(KeyAccessSection, {
    props: {
      connections: [],
      credentials: [],
      catalog: { connections: [], default: null, usages: [], discovery: {} },
      providers: PROVIDERS,
    },
    attachTo: document.body,
  });
  mounted.push(wrapper);
  return wrapper;
}

function buttonByText(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll("button").find((item) => item.text().trim().includes(text));
  expect(button, "找不到按钮：" + text).toBeTruthy();
  return button!;
}

async function openForm(wrapper: VueWrapper) {
  await buttonByText(wrapper, "添加 Key").trigger("click");
  await flushPromises();
  const form = wrapper.findComponent(KeyForm);
  expect(form.exists()).toBe(true);
  return form;
}

type FormWrapper = Awaited<ReturnType<typeof openForm>>;

async function fillCreateForm(form: FormWrapper) {
  await form.find(".qio-combo-trigger").trigger("click");
  await nextTick();
  const option = form.findAll(".qio-combo-opt").find((item) => item.text().includes("DeepSeek"));
  expect(option, "找不到服务商选项").toBeTruthy();
  await option!.trigger("click");
  await nextTick();
  await form.find("#key-secret").setValue(SECRET);
}

function dialogEl(): HTMLElement | null {
  return document.querySelector('[role="dialog"]');
}

function closeButton(): HTMLButtonElement {
  const button = document.querySelector(".qio-modal-head__slot--end button");
  expect(button).toBeTruthy();
  return button as HTMLButtonElement;
}

function pressEscape(target: EventTarget = window) {
  target.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
}

describe("问题 6：下拉列表内的 Esc 不连外层 Key 表单一起关闭", () => {
  it("第一次 Esc 收起服务商列表并保留表单与已填内容，第二次才关闭表单", async () => {
    const wrapper = mountSection();
    const form = await openForm(wrapper);
    await fillCreateForm(form);

    await form.find(".qio-combo-trigger").trigger("click");
    await nextTick();
    expect(form.find(".qio-combo-menu").exists()).toBe(true);

    pressEscape();
    await nextTick();
    await nextTick();
    expect(form.find(".qio-combo-menu").exists()).toBe(false);
    expect(wrapper.findComponent(KeyForm).exists()).toBe(true);
    expect(dialogEl()).not.toBeNull();
    // 已填内容没有被一起带走
    expect((form.find("#key-secret").element as HTMLInputElement).value).toBe(SECRET);

    pressEscape();
    await flushPromises();
    expect(wrapper.findComponent(KeyForm).exists()).toBe(false);
    expect(dialogEl()).toBeNull();
  });
});

describe("问题 7：保存期间不能关闭表单弹窗", () => {
  it("请求未完成时关闭图标 / 遮罩 / Esc 都无效，重复提交只发一次；成功后才关闭", async () => {
    let resolveSave: (value: unknown) => void = () => {};
    mocks.api.createCredential.mockImplementationOnce(
      () => new Promise((resolve) => { resolveSave = resolve as never; }),
    );
    mocks.catalog.discover.mockResolvedValueOnce(discoveryResult());

    const wrapper = mountSection();
    const form = await openForm(wrapper);
    await fillCreateForm(form);

    const firstSubmit = form.trigger("submit");
    const secondSubmit = form.trigger("submit");
    await Promise.all([firstSubmit, secondSubmit]);
    await flushPromises();

    expect(mocks.api.createCredential).toHaveBeenCalledTimes(1);
    // 处理中状态确实传到了弹窗本身：aria-busy + 关闭按钮禁用
    expect(dialogEl()?.getAttribute("aria-busy")).toBe("true");
    expect(closeButton().disabled).toBe(true);

    pressEscape();
    await nextTick();
    expect(wrapper.findComponent(KeyForm).exists()).toBe(true);
    expect(wrapper.emitted("changed")).toBeUndefined();

    const scrim = document.querySelector(".qio-modal-scrim") as HTMLElement;
    scrim.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await nextTick();
    expect(wrapper.findComponent(KeyForm).exists()).toBe(true);

    closeButton().click();
    await nextTick();
    expect(wrapper.findComponent(KeyForm).exists()).toBe(true);

    // 再按一次提交：请求还在跑，不得重复提交
    await form.trigger("submit");
    await flushPromises();
    expect(mocks.api.createCredential).toHaveBeenCalledTimes(1);

    // 请求完成：表单才关闭（不是提前藏起来被当成取消）
    resolveSave({
      ok: true,
      saved: true,
      key_id: "key_new",
      version: 1,
      credential: meta(),
      verify: null,
    });
    await flushPromises();
    expect(wrapper.findComponent(KeyForm).exists()).toBe(false);
    expect(dialogEl()).toBeNull();
    expect(mocks.api.createCredential).toHaveBeenCalledTimes(1);
  });

  it("保存失败：保留表单与填写内容、显示可读错误且不泄露 Key，之后可以正常关闭", async () => {
    mocks.api.createCredential.mockRejectedValueOnce(new Error("network down"));

    const wrapper = mountSection();
    const form = await openForm(wrapper);
    await fillCreateForm(form);

    // 同一帧提交两次：只发一次
    const first = form.trigger("submit");
    const second = form.trigger("submit");
    await Promise.all([first, second]);
    await flushPromises();
    expect(mocks.api.createCredential).toHaveBeenCalledTimes(1);

    // 失败后表单还在、内容没丢、错误可读
    expect(wrapper.findComponent(KeyForm).exists()).toBe(true);
    expect((form.find("#key-secret").element as HTMLInputElement).value).toBe(SECRET);
    const alert = form.find('[role="alert"]');
    expect(alert.exists()).toBe(true);
    expect(alert.text()).toContain("network down");
    expect(alert.text()).not.toContain(SECRET);

    // 处理中状态已解除：关闭图标恢复、弹窗不再是 busy
    expect(dialogEl()?.getAttribute("aria-busy")).toBeNull();
    expect(closeButton().disabled).toBe(false);

    pressEscape();
    await flushPromises();
    expect(wrapper.findComponent(KeyForm).exists()).toBe(false);
    expect(dialogEl()).toBeNull();
  });

  it("请求进行中弹窗 aria-busy 为真，结束后恢复（成功或失败都不会卡在 busy）", async () => {
    let rejectSave: (error: Error) => void = () => {};
    mocks.api.createCredential.mockImplementationOnce(
      () => new Promise((_resolve, reject) => { rejectSave = reject as never; }),
    );

    const wrapper = mountSection();
    const form = await openForm(wrapper);
    await fillCreateForm(form);
    await form.trigger("submit");
    await flushPromises();
    expect(dialogEl()?.getAttribute("aria-busy")).toBe("true");

    rejectSave(new Error("service offline"));
    await flushPromises();
    expect(dialogEl()?.getAttribute("aria-busy")).toBeNull();
    expect(wrapper.findComponent(KeyForm).exists()).toBe(true);
  });
});
