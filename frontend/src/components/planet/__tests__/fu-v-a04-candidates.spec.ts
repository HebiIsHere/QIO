/**
 * V 组独立验证 · 前端 A04：实体候选必须能被用户管理。
 *
 * 原缺陷（基线 `da0436b`，见 _contracts §3.4）
 *
 *   `components/planet/EntityPanel.vue` 完全不提「候选」：既没有「待处理候选（n）」
 *   区块，也没有「采纳 / 丢弃」按钮，冲突（409）没有任何就地提示，归档卡也没有
 *   「该实体已归档」这类禁用说明。`services/entityCandidatesApi.ts` 不存在。
 *   后端登记下来的冲突候选因此**永远到不了用户手里**。
 *
 * 验证手段
 *
 *   * 打桩全局 `fetch`（浏览器模式下 `resolveBackend()` 解析到本机默认地址）；
 *   * 把 EntityPanel 挂起来（`openCardId` 直接进入卡片阅读态），只看渲染结果里的
 *     冻结文案与按钮可用性 —— 不依赖任何内部 class / 变量名；
 *   * 交付物存在性用**文件系统**判断（字面量动态 import 会让 Vite 在导入分析阶段
 *     就把整份 suite 变红，那样基线就只剩收集错误、没有「用例红」的证据）。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia } from "pinia";
import EntityPanel from "../EntityPanel.vue";

const CARD_ID = "card_verify_1";

interface FetchCall {
  url: string;
  method: string;
  body: unknown;
}

type Route = { status?: number; body?: unknown };

const calls: FetchCall[] = [];

function installFetch(handler: (url: string, init: RequestInit) => Route) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: unknown, init: RequestInit = {}) => {
      const url = String(input);
      const method = String(init.method ?? "GET").toUpperCase();
      let body: unknown = null;
      try {
        body = init.body ? JSON.parse(String(init.body)) : null;
      } catch {
        body = init.body ?? null;
      }
      calls.push({ url, method, body });
      const route = handler(url, init) ?? {};
      const status = route.status ?? 200;
      const payload = route.body ?? {};
      return {
        ok: status >= 200 && status < 300,
        status,
        async text() {
          return JSON.stringify(payload);
        },
        async json() {
          return payload;
        },
      } as unknown as Response;
    }),
  );
}

function entityCard(overrides: Record<string, unknown> = {}) {
  return {
    id: CARD_ID,
    node_id: null,
    name: "我家的鹅",
    aliases: ["大白"],
    kind: "动物",
    summary: "用户写下的摘要",
    attributes: [{ key: "年龄", value: "2 岁", confidence: 0.9 }],
    relations: [],
    state: "active",
    created_at: "2026-10-10T00:00:00+00:00",
    updated_at: "2026-10-10T00:00:00+00:00",
    ...overrides,
  };
}

function candidate(overrides: Record<string, unknown> = {}) {
  return {
    candidate_id: "pc_verify_1",
    entity_id: CARD_ID,
    entity_name: "我家的鹅",
    card_state: "active",
    field: "summary",
    field_label: "摘要",
    kind: "summary",
    current_value: "用户写下的摘要",
    candidate_value: "自动提炼的摘要",
    reason: "user_value",
    reason_label: "你在这一点上有明确说法，自动提炼没有覆盖它",
    created_at: "2026-10-10T00:00:00+00:00",
    card_revision: 3,
    adoptable: true,
    blocked_reason: "",
    ...overrides,
  };
}

function mountPanel(handler: (url: string, init: RequestInit) => Route) {
  installFetch(handler);
  return mount(EntityPanel, {
    props: { openCardId: CARD_ID },
    global: { plugins: [createPinia()] },
  });
}

/** 候选区间是默认收起的：先按冻结标题找到可点的表头（summary / button）。 */
async function expandCandidates(wrapper: VueWrapper): Promise<boolean> {
  for (const selector of ["summary", "button", "[role='button']"]) {
    for (const node of wrapper.findAll(selector)) {
      if (node.text().includes("待处理候选")) {
        await node.trigger("click");
        await flushPromises();
        return true;
      }
    }
  }
  return false;
}

function buttonsWithText(wrapper: VueWrapper, text: string) {
  return wrapper.findAll("button").filter((node) => node.text().trim() === text);
}

beforeEach(() => {
  calls.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("A04 前端：实体候选的用户管理入口", () => {
  it("候选存在时渲染「待处理候选（n）」区块，且候选值默认收起", async () => {
    const wrapper = mountPanel((url) => {
      if (url.includes(`/api/entities/${CARD_ID}/candidates`)) {
        return { body: { entity: entityCard(), candidates: [candidate()] } };
      }
      if (url.includes("/api/entities")) return { body: { entities: [entityCard()] } };
      return { body: {} };
    });
    await flushPromises();

    expect(wrapper.html()).toContain("待处理候选");
    expect(wrapper.html()).toMatch(/待处理候选\s*[（(]\s*1\s*[）)]/);
    expect(wrapper.html()).not.toContain("自动提炼的摘要");
  });

  it("展开后能看到当前值 / 候选值 / 一句话原因，并有「采纳」「丢弃」按钮", async () => {
    const wrapper = mountPanel((url) => {
      if (url.includes(`/api/entities/${CARD_ID}/candidates`)) {
        return { body: { entity: entityCard(), candidates: [candidate()] } };
      }
      if (url.includes("/api/entities")) return { body: { entities: [entityCard()] } };
      return { body: {} };
    });
    await flushPromises();
    await expandCandidates(wrapper);

    const html = wrapper.html();
    expect(html).toContain("自动提炼的摘要");
    expect(html).toContain("用户写下的摘要");
    expect(html).not.toContain("user_value");
    expect(buttonsWithText(wrapper, "采纳").length, "必须有「采纳」按钮").toBeGreaterThan(0);
    expect(buttonsWithText(wrapper, "丢弃").length, "必须有「丢弃」按钮").toBeGreaterThan(0);
  });

  it("采纳遇到 409 时就地提示「已被改动」，且候选仍在列表里（不假装成功）", async () => {
    let adoptCalls = 0;
    const wrapper = mountPanel((url) => {
      if (url.includes("/adopt")) {
        adoptCalls += 1;
        return { status: 409, body: { ok: false, conflict: true, current_revision: 9, reason: "revision" } };
      }
      if (url.includes(`/api/entities/${CARD_ID}/candidates`)) {
        return { body: { entity: entityCard(), candidates: [candidate()] } };
      }
      if (url.includes("/api/entities")) return { body: { entities: [entityCard()] } };
      return { body: {} };
    });
    await flushPromises();
    await expandCandidates(wrapper);

    const adopt = buttonsWithText(wrapper, "采纳");
    expect(adopt.length, "必须有「采纳」按钮").toBeGreaterThan(0);
    await adopt[0].trigger("click");
    await flushPromises();

    expect(adoptCalls, "必须真的打了采纳端点").toBeGreaterThan(0);
    const html = wrapper.html();
    expect(html, "409 必须就地说明「这条实体已被改动」").toContain("已被改动");
    expect(html, "失败不得把候选从界面里抹掉").toContain("自动提炼的摘要");
  });

  it("采纳保存失败（500）不假装成功：候选还在，且给出失败原因", async () => {
    const wrapper = mountPanel((url) => {
      if (url.includes("/adopt")) {
        return { status: 500, body: { detail: "boom" } };
      }
      if (url.includes(`/api/entities/${CARD_ID}/candidates`)) {
        return { body: { entity: entityCard(), candidates: [candidate()] } };
      }
      if (url.includes("/api/entities")) return { body: { entities: [entityCard()] } };
      return { body: {} };
    });
    await flushPromises();
    await expandCandidates(wrapper);

    const adopt = buttonsWithText(wrapper, "采纳");
    expect(adopt.length).toBeGreaterThan(0);
    await adopt[0].trigger("click");
    await flushPromises();

    // 只看**可见文本**：`wrapper.html()` 里带着 Vue 模板注释
    // （`<!-- …也不翻成「已采纳」 -->`，注释会留在 outerHTML），对原始 html 串做
    // 「已采纳」正则会命中注释而不是渲染结果。组件实际渲染的是「采纳没有成功：…」。
    const visible = wrapper.text();
    expect(visible, "失败不得把候选从界面里抹掉").toContain("自动提炼的摘要");
    expect(visible, "必须就地给出失败原因").toContain("采纳没有成功");
    expect(visible, "500 不许假装成功").not.toMatch(/已采纳|采纳成功/);
  });

  it("归档卡上的候选不可采纳，并说明「该实体已归档」", async () => {
    const archived = candidate({
      card_state: "archived",
      adoptable: false,
      blocked_reason: "card_archived",
    });
    const wrapper = mountPanel((url) => {
      if (url.includes(`/api/entities/${CARD_ID}/candidates`)) {
        return { body: { entity: entityCard({ state: "archived" }), candidates: [archived] } };
      }
      if (url.includes("/api/entities")) {
        return { body: { entities: [entityCard({ state: "archived" })] } };
      }
      return { body: {} };
    });
    await flushPromises();
    await expandCandidates(wrapper);

    const html = wrapper.html();
    expect(html, "必须说明该实体已归档").toContain("已归档");

    const adopt = buttonsWithText(wrapper, "采纳");
    expect(adopt.length, "归档卡上仍应显示采纳按钮（但不可用）").toBeGreaterThan(0);
    for (const node of adopt) {
      const disabled =
        node.attributes("disabled") !== undefined || node.classes().includes("disabled");
      expect(disabled, "归档卡上的「采纳」必须是禁用状态").toBe(true);
    }
  });

  it("交付物存在性：services/entityCandidatesApi.ts", () => {
    const apiPath = resolve(process.cwd(), "src/services/entityCandidatesApi.ts");
    expect(existsSync(apiPath), `services/entityCandidatesApi.ts 必须存在（${apiPath}）`).toBe(
      true,
    );
    // 只断言冻结的**端点形状**（函数名不在契约里，钉死名字会变成假失败）
    const source = readFileSync(apiPath, "utf8");
    for (const fragment of ["/api/entities/candidates", "/candidates", "/adopt", "/dismiss"]) {
      expect(source.includes(fragment), `entityCandidatesApi.ts 必须调用 ${fragment}`).toBe(true);
    }
  });
});
