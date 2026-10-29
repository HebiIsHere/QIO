/**
 * 输出速度保存（返修五）：真实组件 + 可控接口响应。
 *
 * 规则：
 * - 分别维护「用户最新选择」（ui.typewriterCps）与「实际已保存值」（初始确认值 + 接口成功返回）；
 * - 同一设置依次保存（不同设置独立）；排队执行较早请求时不得把较早选择写回界面；
 * - 等待期间保持最新选择；最新失败恢复最后实际保存成功的值并保留错误；
 * - 全部失败恢复最初已确认的保存值（不得用未保存的选择，也不得硬编码默认值）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises } from "@vue/test-utils";
import {
  chooseSelectOption,
  flushFrames,
  mountSettings,
  openCategory,
  sectionById,
} from "./settingsTestUtils";

vi.mock("../../../services/api", async () => {
  const { settingsApiModule } = await import("./settingsApiMock");
  return settingsApiModule();
});
vi.mock("../../../services/modelCatalog", async () => {
  const { modelCatalogModule } = await import("./settingsApiMock");
  return modelCatalogModule();
});

type PendingCall = {
  cps: number;
  resolve: (value: { ok: boolean; typewriter_cps: number }) => void;
  reject: (error: Error) => void;
};

const SPEED_LABEL: Record<number, string> = { 25: "慢", 50: "中", 75: "快" };

async function mountSpeed(initial = 50) {
  const { api } = await import("../../../services/api");
  const getUi = api.getUISettings as ReturnType<typeof vi.fn>;
  const updateUi = api.updateUISettings as ReturnType<typeof vi.fn>;
  getUi.mockResolvedValue({ typewriter_cps: initial, close_behavior: "ask" });
  updateUi.mockReset();
  const calls: PendingCall[] = [];
  updateUi.mockImplementation(
    (cps: number) =>
      new Promise((resolve, reject) => {
        calls.push({ cps, resolve, reject });
      }),
  );

  // 先用一次真实的初始读取建立「已确认的初始值」（App.vue 启动时就是这么做的），
  // 再挂载设置页，确保回滚基线不是某个未保存的选择。
  const { createPinia, setActivePinia } = await import("pinia");
  const pinia = createPinia();
  setActivePinia(pinia);
  const { useUiStore } = await import("../../../stores/ui");
  const ui = useUiStore();
  await ui.load();

  const w = await mountSettings(pinia);
  await openCategory(w, "regular");
  await flushFrames();
  return { w, ui, calls, updateUi, api };
}

async function chooseSpeed(w: ReturnType<typeof mountSettings> extends Promise<infer T> ? T : never, cps: number) {
  const regular = sectionById(w as never, "regular");
  await chooseSelectOption(regular as never, SPEED_LABEL[cps]);
  await flushFrames();
}

function settle(call: PendingCall, cps?: number): void {
  call.resolve({ ok: true, typewriter_cps: cps ?? call.cps });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("输出速度：保存失败与连续选择", () => {
  it("单次保存失败：恢复原已保存值（50）并显示错误", async () => {
    const { w, ui, calls } = await mountSpeed(50);
    expect(ui.typewriterCps).toBe(50);
    expect(ui.typewriterCpsSaved).toBe(50);

    await chooseSpeed(w, 25);
    expect(ui.typewriterCps).toBe(25);
    expect(calls).toHaveLength(1);
    calls[0].reject(new Error("网络不可用"));
    await flushFrames();

    expect(ui.typewriterCps).toBe(50);
    expect(sectionById(w, "regular").find(".set-notice.err").text()).toContain("保存输出速度失败");
    w.unmount();
  });

  it("初始 50，连续 25 成功、75 失败：等待时保持 75，失败后恢复 25", async () => {
    const { w, ui, calls, updateUi } = await mountSpeed(50);
    await chooseSpeed(w, 25);
    await chooseSpeed(w, 75);
    // 同一设置依次保存：第二个请求要等第一个结算
    expect(updateUi).toHaveBeenCalledTimes(1);

    settle(calls[0], 25);
    await flushFrames();
    expect(ui.typewriterCps).toBe(75); // 较早请求成功不得把界面改回 25
    expect(updateUi).toHaveBeenCalledTimes(2);

    calls[1].reject(new Error("75 写入失败"));
    await flushFrames();
    expect(ui.typewriterCps).toBe(25); // 恢复最后实际成功的 25
    expect(sectionById(w, "regular").find(".set-notice.err").text()).toContain("保存输出速度失败");
    w.unmount();
  });

  it("连续 25 失败、75 成功：最终为 75（较新成功优先）", async () => {
    const { w, ui, calls, updateUi } = await mountSpeed(50);
    await chooseSpeed(w, 25);
    await chooseSpeed(w, 75);
    expect(updateUi).toHaveBeenCalledTimes(1);

    calls[0].reject(new Error("25 写入失败"));
    await flushFrames();
    expect(ui.typewriterCps).toBe(75); // 较早失败不改变用户最新选择
    expect(updateUi).toHaveBeenCalledTimes(2);

    settle(calls[1], 75);
    await flushFrames();
    expect(ui.typewriterCps).toBe(75);
    w.unmount();
  });

  it("两次都失败：恢复最初已确认的 50（不是 25/75）", async () => {
    const { w, ui, calls } = await mountSpeed(50);
    await chooseSpeed(w, 25);
    await chooseSpeed(w, 75);
    calls[0].reject(new Error("25 失败"));
    await flushFrames();
    calls[1].reject(new Error("75 失败"));
    await flushFrames();

    expect(ui.typewriterCps).toBe(50);
    expect(sectionById(w, "regular").find(".set-notice.err").text()).toContain("保存输出速度失败");
    w.unmount();
  });

  it("非默认初值 75：两次都失败恢复 75，不硬编码默认值", async () => {
    const { w, ui, calls } = await mountSpeed(75);
    expect(ui.typewriterCps).toBe(75);
    expect(ui.typewriterCpsSaved).toBe(75);

    await chooseSpeed(w, 25);
    await chooseSpeed(w, 50);
    calls[0].reject(new Error("25 失败"));
    await flushFrames();
    calls[1].reject(new Error("50 失败"));
    await flushFrames();

    expect(ui.typewriterCps).toBe(75);
    w.unmount();
  });

  it("连续 25、75、50：最新 50 等待期间，较早请求开始/结束都不能把界面改回 25/75", async () => {
    const { w, ui, calls, updateUi } = await mountSpeed(50);
    await chooseSpeed(w, 25);
    await chooseSpeed(w, 75);
    await chooseSpeed(w, 50);
    expect(ui.typewriterCps).toBe(50);
    expect(updateUi).toHaveBeenCalledTimes(1);

    settle(calls[0], 25);
    await flushFrames();
    expect(ui.typewriterCps).toBe(50); // 25 的成功不改界面
    expect(updateUi).toHaveBeenCalledTimes(2);

    settle(calls[1], 75);
    await flushFrames();
    expect(ui.typewriterCps).toBe(50); // 75 的成功也不改界面
    expect(updateUi).toHaveBeenCalledTimes(3);

    settle(calls[2], 50);
    await flushFrames();
    expect(ui.typewriterCps).toBe(50);
    w.unmount();
  });

  it("三次选择：25 成功、75 成功、50 失败 → 恢复最后实际成功的 75 且错误可见", async () => {
    const { w, ui, calls, updateUi } = await mountSpeed(50);
    await chooseSpeed(w, 25);
    await chooseSpeed(w, 75);
    await chooseSpeed(w, 50);

    settle(calls[0], 25);
    await flushFrames();
    expect(updateUi).toHaveBeenCalledTimes(2);
    settle(calls[1], 75);
    await flushFrames();
    expect(updateUi).toHaveBeenCalledTimes(3);
    expect(ui.typewriterCps).toBe(50); // 等最新选择

    calls[2].reject(new Error("50 失败"));
    await flushFrames();
    expect(ui.typewriterCps).toBe(75);
    expect(sectionById(w, "regular").find(".set-notice.err").text()).toContain("保存输出速度失败");
    w.unmount();
  });
});
