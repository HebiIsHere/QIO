/**
 * 发布前第三轮：设置保存可靠性（F08 / F14）。
 *
 * F08：同一区域不同设置的保存在途互不作废；A 失败回滚并保留错误，B 成功保持成功；
 *      同一设置仍只认最新响应。
 * F14：迟到的初始读取不得覆盖用户较新的编辑、保存值和「当前生效参数」；
 *      请求被作废后保存状态必须正确结束（不能永久停在「保存中」）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent, nextTick } from "vue";
import { flushPromises, mount } from "@vue/test-utils";
import { resetFloatPositions } from "../../../composables/floatingState";
import { useGroupDraft } from "../../../composables/useQioSettingsSave";
import { effectiveFacts, resetEffectiveFacts } from "../effectiveFacts";
import {
  flushFrames,
  mountSettings,
  openCategory,
  sectionById,
  setViewportWidth,
} from "./settingsTestUtils";

vi.mock("../../../services/api", async () => {
  const { settingsApiModule } = await import("./settingsApiMock");
  return settingsApiModule();
});

vi.mock("../../../services/modelCatalog", async () => {
  const { modelCatalogModule } = await import("./settingsApiMock");
  return modelCatalogModule();
});

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  resetFloatPositions();
  resetEffectiveFacts();
  document.body.innerHTML = "";
  setViewportWidth(1280);
});

describe("F08 同一区域不同设置的保存互不取消", () => {
  it("A（工作区）失败、B（输出保留期）成功：A 回滚并保留错误，B 的值保持成功", async () => {
    const { api } = await import("../../../services/api");
    const updateComputer = api.updateComputerSettings as ReturnType<typeof vi.fn>;
    const updateTools = api.updateToolHistorySettings as ReturnType<typeof vi.fn>;
    const rejectWorkspace: Array<(e: Error) => void> = [];
    updateComputer.mockImplementation(
      () => new Promise((_resolve, reject) => { rejectWorkspace.push(reject); }),
    );
    updateTools.mockResolvedValueOnce({
      record_outputs: true,
      output_retention_days: 30,
      record_retention_days: 0,
    });

    const w = await mountSettings();
    await openCategory(w, "tools");
    const tools = sectionById(w, "tools");

    // A：保存工作区根目录（请求挂起）
    const workspace = tools.find('input[aria-label="工作区根目录"]');
    await workspace.setValue("D:\\new-workspace");
    await workspace.trigger("change");
    await flushFrames();
    expect(rejectWorkspace.length).toBeGreaterThan(0);

    // B：另一个独立设置保存成功（不能被 A 的在途请求作废）
    const retention = tools.find('input[aria-label="输出保留（天）"]');
    await retention.setValue("30");
    await retention.trigger("blur");
    await flushFrames();
    expect(updateTools).toHaveBeenCalledTimes(1);

    // A 此时才失败：必须回滚自己的工作区输入，并留下可读错误；B 的保存不能被取消。
    for (const reject of rejectWorkspace) reject(new Error("PUT /api/settings/computer -> 500: boom"));
    await flushFrames();

    expect((workspace.element as HTMLInputElement).value).toBe("");
    expect(tools.find(".set-notice.err").text()).toContain("保存工作区根目录失败");
    expect((tools.find('input[aria-label="输出保留（天）"]').element as HTMLInputElement).value).toBe(
      "30",
    );
    expect(tools.text()).not.toContain("保存中");
    w.unmount();
  });

  it("同一设置连续保存只认最新一次：旧响应迟到不覆盖新选择", async () => {
    const { api } = await import("../../../services/api");
    const update = api.updateMaintenanceSettings as ReturnType<typeof vi.fn>;
    let releaseFirst: (v: { enabled: boolean; interval_hours: number }) => void = () => {};
    update.mockImplementationOnce(
      () => new Promise((resolve) => { releaseFirst = resolve as never; }),
    );
    update.mockImplementationOnce(async (body: { enabled?: boolean; interval_hours?: number }) => ({
      enabled: body.enabled ?? true,
      interval_hours: body.interval_hours ?? 24,
    }));

    const w = await mountSettings();
    await openCategory(w, "memory");
    const memory = sectionById(w, "memory");

    const interval = memory.find('input[aria-label="整理间隔（小时）"]');
    await interval.setValue("30");
    await interval.trigger("blur");
    await flushFrames();

    await interval.setValue("40");
    await interval.trigger("blur");
    await flushFrames();
    expect(update).toHaveBeenCalledTimes(2);

    releaseFirst({ enabled: true, interval_hours: 24 });
    await flushFrames();
    expect((interval.element as HTMLInputElement).value).toBe("40");
    w.unmount();
  });
});

describe("F14 迟到的初始读取不覆盖较新的编辑 / 保存值 / 生效摘要", () => {
  it("记忆分段：迟到的 getMemorySettings 不改回已保存值，也不覆盖生效摘要", async () => {
    const { api } = await import("../../../services/api");
    let resolveLoad: (v: { fragment_max_turns: number; fragment_max_tokens: number }) => void = () => {};
    (api.getMemorySettings as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((resolve) => { resolveLoad = resolve as never; }),
    );

    const w = await mountSettings();
    await openCategory(w, "memory");
    const memory = sectionById(w, "memory");

    const maxTokens = memory.find('input[aria-label="单段长度目标"]');
    await maxTokens.setValue("8000");
    await maxTokens.trigger("blur");
    await nextTick();
    await memory
      .find('[data-settings-group="memory.fragment"] [data-settings-action="save"]')
      .trigger("click");
    await flushFrames();
    expect(effectiveFacts.memoryTokens).toBe(8000);

    // 初始读取此刻才返回（旧值）：不得覆盖保存值，也不得把生效摘要改回旧值。
    resolveLoad({ fragment_max_turns: 10, fragment_max_tokens: 4096 });
    await flushFrames();
    expect((maxTokens.element as HTMLInputElement).value).toBe("8000");
    expect(effectiveFacts.memoryTokens).toBe(8000);

    await openCategory(w, "tools");
    expect(sectionById(w, "tools").text()).toContain("8000");
    w.unmount();
  });

  it("联网搜索：迟到的 getSearchSettings 不改回已保存的搜索参数", async () => {
    const { api } = await import("../../../services/api");
    let resolveLoad: (v: {
      searxng_url: string;
      bocha_has_key: boolean;
      keyless_fallback: boolean;
      top_k_default: number;
      max_fetch_chars: number;
    }) => void = () => {};
    (api.getSearchSettings as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((resolve) => { resolveLoad = resolve as never; }),
    );

    const w = await mountSettings();
    await openCategory(w, "models");
    const models = sectionById(w, "models");
    await models.find('[data-settings-fold-toggle="models.search.advanced"]').trigger("click");
    await nextTick();

    const topK = models.find('input[aria-label="返回条数"]');
    await topK.setValue("8");
    await nextTick();
    await models
      .find('[data-settings-group="models.search"] [data-settings-action="save"]')
      .trigger("click");
    await flushFrames();
    expect(effectiveFacts.searchTopK).toBe(8);

    resolveLoad({
      searxng_url: "",
      bocha_has_key: false,
      keyless_fallback: true,
      top_k_default: 5,
      max_fetch_chars: 15000,
    });
    await flushFrames();
    expect((topK.element as HTMLInputElement).value).toBe("8");
    expect(effectiveFacts.searchTopK).toBe(8);
    w.unmount();
  });
});

describe("F14 请求被作废后保存状态正确结束", () => {
  it("组保存在途时被 reset 作废：结算后 saving=false，仍能再次保存", async () => {
    const releases: Array<(v: { a: number }) => void> = [];
    const Harness = defineComponent({
      setup() {
        const draft = useGroupDraft<{ a: number }>({
          initial: { a: 1 },
          commit: () =>
            new Promise<{ a: number }>((resolve) => {
              releases.push(resolve);
            }),
          failure: "保存草稿",
        });
        return { draft };
      },
      template: `<div>
        <button type="button" data-save @click="draft.save()">save</button>
        <span data-saving>{{ draft.saving.value ? "saving" : "idle" }}</span>
      </div>`,
    });
    const w = mount(Harness);
    await w.find("[data-save]").trigger("click");
    await nextTick();
    expect(w.find("[data-saving]").text()).toBe("saving");

    // 迟到的初始读取落地，作废这次在途保存
    (w.vm as unknown as { draft: ReturnType<typeof useGroupDraft> }).draft.reset({ a: 2 });
    await nextTick();
    releases[0]({ a: 1 });
    await flushPromises();
    expect(w.find("[data-saving]").text()).toBe("idle");

    // 状态已结束 → 还能继续保存（否则会永久停在「保存中」）
    await w.find("[data-save]").trigger("click");
    await nextTick();
    expect(w.find("[data-saving]").text()).toBe("saving");
    releases[1]({ a: 3 });
    await flushPromises();
    expect(w.find("[data-saving]").text()).toBe("idle");
    w.unmount();
  });
});

describe("返修：失败回滚到真实已保存值（不能拿输入值当基线）", () => {
  it("输出保留期 90 改成 30、保存失败后回到 90", async () => {
    const { api } = await import("../../../services/api");
    (api.updateToolHistorySettings as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("500 boom"),
    );
    const w = await mountSettings();
    await openCategory(w, "tools");
    const tools = sectionById(w, "tools");
    const input = tools.find('input[aria-label="输出保留（天）"]');
    await input.setValue("30");
    await input.trigger("blur");
    await flushFrames();

    expect((input.element as HTMLInputElement).value).toBe("90");
    expect(tools.find(".set-notice.err").text()).toContain("保存输出保留期失败");
    w.unmount();
  });

  it("后台整理间隔 24 改成 30、保存失败后回到 24", async () => {
    const { api } = await import("../../../services/api");
    (api.updateMaintenanceSettings as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("500 boom"),
    );
    const w = await mountSettings();
    await openCategory(w, "memory");
    const memory = sectionById(w, "memory");
    const input = memory.find('input[aria-label="整理间隔（小时）"]');
    await input.setValue("30");
    await input.trigger("blur");
    await flushFrames();

    expect((input.element as HTMLInputElement).value).toBe("24");
    expect(memory.find(".set-notice.err").text()).toContain("保存整理间隔失败");
    w.unmount();
  });
});

describe("返修：两个设置先后失败，只修好第二个时第一个错误仍可见", () => {
  it("工作区失败 + 输出保留期失败 → 只重试保留期成功，工作区错误保持可见", async () => {
    const { api } = await import("../../../services/api");
    const updateComputer = api.updateComputerSettings as ReturnType<typeof vi.fn>;
    const updateTools = api.updateToolHistorySettings as ReturnType<typeof vi.fn>;
    const rejects: Array<(e: Error) => void> = [];
    updateComputer.mockImplementation(
      () => new Promise((_resolve, reject) => { rejects.push(reject); }),
    );
    updateTools.mockRejectedValueOnce(new Error("500 boom"));
    updateTools.mockResolvedValueOnce({
      record_outputs: true,
      output_retention_days: 35,
      record_retention_days: 0,
    });

    const w = await mountSettings();
    await openCategory(w, "tools");
    const tools = sectionById(w, "tools");

    const workspace = tools.find('input[aria-label="工作区根目录"]');
    await workspace.setValue("D:\\failed-workspace");
    await workspace.trigger("change");
    await flushFrames();

    const retention = tools.find('input[aria-label="输出保留（天）"]');
    await retention.setValue("30");
    await retention.trigger("blur");
    await flushFrames();
    expect(tools.find(".set-notice.err").text()).toContain("保存输出保留期失败");

    for (const reject of rejects) reject(new Error("workspace 500"));
    await flushFrames();

    await retention.setValue("35");
    await retention.trigger("blur");
    await flushFrames();

    expect((retention.element as HTMLInputElement).value).toBe("35");
    expect(tools.find(".set-notice.err").text()).toContain("保存工作区根目录失败");
    w.unmount();
  });
});

describe("返修：实际输入即建立保护，迟到初始读取不覆盖未提交输入", () => {
  it("工作区：只输入未失焦时，迟到 getComputerSettings 不覆盖", async () => {
    const { api } = await import("../../../services/api");
    let resolveLoad: (v: { root_dir: string; permission_mode: string }) => void = () => {};
    (api.getComputerSettings as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((resolve) => { resolveLoad = resolve as never; }),
    );
    const w = await mountSettings();
    await openCategory(w, "tools");
    const tools = sectionById(w, "tools");
    const workspace = tools.find('input[aria-label="工作区根目录"]');
    await workspace.setValue("D:\\typed-not-blurred");
    await nextTick();
    resolveLoad({ root_dir: "D:\\server-workspace", permission_mode: "default" });
    await flushFrames();
    expect((workspace.element as HTMLInputElement).value).toBe("D:\\typed-not-blurred");
    w.unmount();
  });

  it("迭代上限：只输入未失焦时，迟到 getLoopSettings 不覆盖", async () => {
    const { api } = await import("../../../services/api");
    let resolveLoad: (v: { max_iterations: number; output_token_budget: number }) => void = () => {};
    (api.getLoopSettings as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((resolve) => { resolveLoad = resolve as never; }),
    );
    const w = await mountSettings();
    await openCategory(w, "memory");
    const memory = sectionById(w, "memory");
    const input = memory.find('input[aria-label="迭代上限"]');
    await input.setValue("200");
    await nextTick();
    resolveLoad({ max_iterations: 128, output_token_budget: 51200 });
    await flushFrames();
    expect((input.element as HTMLInputElement).value).toBe("200");
    w.unmount();
  });
});

describe("返修：取消回到服务端实际保存值（不是加载前占位默认值）", () => {
  it("记忆分段：初始读取期间已编辑，取消回到服务端保存的 12000", async () => {
    const { api } = await import("../../../services/api");
    let resolveLoad: (v: { fragment_max_turns: number; fragment_max_tokens: number }) => void = () => {};
    (api.getMemorySettings as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((resolve) => { resolveLoad = resolve as never; }),
    );
    const w = await mountSettings();
    await openCategory(w, "memory");
    const memory = sectionById(w, "memory");
    const maxTokens = memory.find('input[aria-label="单段长度目标"]');
    await maxTokens.setValue("9000");
    await nextTick();
    resolveLoad({ fragment_max_turns: 10, fragment_max_tokens: 12000 });
    await flushFrames();

    await memory
      .find('[data-settings-group="memory.fragment"] [data-settings-action="cancel"]')
      .trigger("click");
    await nextTick();
    expect((maxTokens.element as HTMLInputElement).value).toBe("12000");
    w.unmount();
  });

  it("联网搜索：初始读取期间已编辑，取消回到服务端保存的 9", async () => {
    const { api } = await import("../../../services/api");
    let resolveLoad: (v: {
      searxng_url: string;
      bocha_has_key: boolean;
      keyless_fallback: boolean;
      top_k_default: number;
      max_fetch_chars: number;
    }) => void = () => {};
    (api.getSearchSettings as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((resolve) => { resolveLoad = resolve as never; }),
    );
    const w = await mountSettings();
    await openCategory(w, "models");
    const models = sectionById(w, "models");
    await models.find('[data-settings-fold-toggle="models.search.advanced"]').trigger("click");
    await nextTick();
    const topK = models.find('input[aria-label="返回条数"]');
    await topK.setValue("12");
    await nextTick();
    resolveLoad({
      searxng_url: "",
      bocha_has_key: false,
      keyless_fallback: true,
      top_k_default: 9,
      max_fetch_chars: 15000,
    });
    await flushFrames();
    await models
      .find('[data-settings-group="models.search"] [data-settings-action="cancel"]')
      .trigger("click");
    await nextTick();
    expect((topK.element as HTMLInputElement).value).toBe("9");
    w.unmount();
  });
});
