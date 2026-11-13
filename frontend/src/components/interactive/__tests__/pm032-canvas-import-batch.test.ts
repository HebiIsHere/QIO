/**
 * #44/#45（PM-QIO032）：文件拖放 / 多文件导入的批次反馈 UI。
 *
 * 验收（BRIEF #44/#45 + Lead E2E ⑤）：
 * 1. 单批 5 份上限：第 6 份起如实标「未接收」并保留在清单里（不静默丢）；
 * 2. 明确列出已接收 / 未接收明细与每个文件的真实状态；
 * 3. 同批后续成功不覆盖前面的失败；重试只操作对应文件；
 * 4. 首件上传失败：有可见失败原因 + 重试入口，后续文件不受影响；
 * 5. 「未接收」项可以重新导入（成功后建卡并关联）。
 *
 * 组件测试用真实的 Pinia store + i-state 的 ImportBatch 数据层，只 mock 附件服务。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

const mocks = vi.hoisted(() => ({
  uploadAttachment: vi.fn(),
  waitUntilSettled: vi.fn(),
  registerSourceAttachment: vi.fn(),
}));

vi.mock("../../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/attachments")>();
  return {
    ...actual,
    uploadAttachment: mocks.uploadAttachment,
    waitUntilSettled: mocks.waitUntilSettled,
    registerSourceAttachment: mocks.registerSourceAttachment,
  };
});

import AddMenu from "../AddMenu.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState } from "../../../interactive/board";
import { publishBoardArea } from "../../../interactive/viewport";
import type { AttachmentRef } from "../../../services/attachments";
import type { BoardState } from "../../../interactive/types";

let wrapper: VueWrapper | null = null;
let store: ReturnType<typeof useInteractiveStore>;

function attachment(name: string, over: Partial<AttachmentRef> = {}): AttachmentRef {
  return { id: "att_" + name, name, sizeBytes: 16, kind: "copy", display: "已保存副本", state: "ready", error: null, ...over };
}

function file(name: string): File {
  return new File(["BATCH"], name, { type: "text/plain" });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

async function settle(times = 4): Promise<void> {
  for (let i = 0; i < times; i += 1) {
    await flushPromises();
    await nextTick();
  }
}

async function dropFiles(files: File[]): Promise<void> {
  await wrapper!.get(".add").trigger("drop", { dataTransfer: { files } });
  await settle();
}

function batchItems() {
  const batch = store.currentImportBatch;
  if (!batch) throw new Error("没有创建导入批次");
  return batch.items;
}

function statusEl() {
  return wrapper!.find('[data-im="add-material-status"]');
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  store = useInteractiveStore();
  store.board = emptyState("board_t");
  publishBoardArea(null);
  for (const key of Object.keys(mocks) as (keyof typeof mocks)[]) mocks[key].mockReset();
  vi.spyOn(store, "commit").mockImplementation((next: BoardState, label: string) => {
    void label;
    store.board = { ...next, boardId: store.boardId };
  });
  mocks.uploadAttachment.mockImplementation(async (upload: File) => attachment(upload.name));
  mocks.waitUntilSettled.mockImplementation(async (item: AttachmentRef) => item);
  mocks.registerSourceAttachment.mockImplementation(async (path: string) => attachment(path.split(/[\\/]/).pop() || "source"));
  wrapper = mount(AddMenu, { attachTo: document.body });
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  document.body.innerHTML = "";
  publishBoardArea(null);
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("#44 单批 5 份上限与未接收明细", () => {
  it("六件拖放：只接收 5 件，第 6 件标「未接收」并保留在清单里", async () => {
    const names = Array.from({ length: 6 }, (_, index) => "batch-" + (index + 1) + ".txt");
    await dropFiles(names.map(file));

    const items = batchItems();
    expect(items).toHaveLength(6);
    expect(items.filter((item) => item.status === "succeeded")).toHaveLength(5);
    expect(items[5].status).toBe("unreceived");
    expect(items[5].name).toBe("batch-6.txt");
    // 只建了 5 张文件卡：第 6 份没有落盘
    expect(store.board!.cards.filter((card) => !card.deleted)).toHaveLength(5);

    const panel = statusEl();
    expect(panel.exists(), "批次区没有渲染").toBe(true);
    const text = panel.text();
    expect(text, "没有列出已接收/未接收明细").toContain("已接收 5 份 / 未接收 1 份");
    expect(text, "未接收项没有列出文件名").toContain("batch-6.txt");
    expect(text).toContain("未接收");
  });
});

describe("#45 每文件真实状态与重试只作用于对应文件", () => {
  it("首件上传失败：失败有可见原因与重试入口，后续文件照常成功；重试只重传该文件", async () => {
    mocks.uploadAttachment.mockRejectedValueOnce(new Error("磁盘写满了"));
    await dropFiles([file("bad-1.txt"), file("good-2.txt")]);

    const items = batchItems();
    expect(items[0].status).toBe("failed");
    expect(items[0].error).toContain("磁盘写满了");
    expect(items[1].status).toBe("succeeded");
    // 失败项不覆盖同批成功项：只有成功的那份建了卡
    expect(store.board!.cards.filter((card) => !card.deleted)).toHaveLength(1);

    // 失败原因 + 重试入口都在批次区可见
    const panel = statusEl();
    expect(panel.find('[data-im="file-card-error"]').exists()).toBe(true);
    expect(panel.find('[data-im="file-card-error"]').text()).toContain("磁盘写满了");
    const retries = panel.findAll('[data-im="card-attach-retry"]');
    expect(retries).toHaveLength(1);

    // 重试：只处理这一个文件（第 2 次上传 = 首件重试），成功后建卡关联
    mocks.uploadAttachment.mockResolvedValueOnce(attachment("bad-1.txt"));
    await retries[0].trigger("click");
    await settle();
    const after = batchItems();
    expect(after[0].status).toBe("succeeded");
    expect(after[1].status).toBe("succeeded");
    expect(mocks.uploadAttachment).toHaveBeenCalledTimes(3);
    expect(store.board!.cards.filter((card) => !card.deleted)).toHaveLength(2);
    expect(panel.text()).toContain("已接收 2 份 / 未接收 0 份");
  });

  it("已接收项不能被后续回执覆盖成失败（后续成功不覆盖前面的失败 / 反之亦然）", async () => {
    await dropFiles([file("a.txt"), file("b.txt")]);
    const items = batchItems();
    expect(items.every((item) => item.status === "succeeded")).toBe(true);
    // 模拟迟到回执：对已成功的项写 failed，不应覆盖终态
    store.noteImportItem(store.currentImportBatch!.id, items[0].key, { status: "failed", error: "迟到失败" });
    expect(batchItems()[0].status).toBe("succeeded");
  });

  it("「未接收」的第六份可重新导入：点击重新导入后建卡并标为已接收", async () => {
    const names = Array.from({ length: 6 }, (_, index) => "batch-" + (index + 1) + ".txt");
    await dropFiles(names.map(file));
    const sixth = batchItems()[5];
    expect(sixth.status).toBe("unreceived");

    const retry = statusEl().findAll('[data-im="card-attach-retry"]');
    expect(retry, "未接收项没有重新导入入口").toHaveLength(1);
    expect(retry[0].text()).toContain("重新导入");
    await retry[0].trigger("click");
    await settle();

    expect(batchItems()[5].status).toBe("succeeded");
    expect(store.board!.cards.filter((card) => !card.deleted)).toHaveLength(6);
    expect(statusEl().text()).toContain("已接收 6 份 / 未接收 0 份");
  });
});

describe("#44/#45 批次反馈的可见性规则", () => {
  it("批次区不自动消失（未接收项必须一直可操作），关闭入口可收起", async () => {
    await dropFiles(Array.from({ length: 6 }, (_, index) => file("batch-" + (index + 1) + ".txt")));
    await new Promise((resolve) => setTimeout(resolve, 3500));
    await settle();
    expect(statusEl().exists(), "批次结果被自动清掉了").toBe(true);

    await wrapper!.get('[data-im="add-material-dismiss"]').trigger("click");
    await settle();
    expect(statusEl().exists()).toBe(false);
  });

  it("拖放文件名与状态都渲染在批次区（每项有 data-status / data-name 供 E2E 核对）", async () => {
    await dropFiles([file("solo.txt")]);
    const item = wrapper!.get('[data-im="add-batch-item"]');
    expect(item.attributes("data-status")).toBe("succeeded");
    expect(item.attributes("data-name")).toBe("solo.txt");
    expect(item.text()).toContain("solo.txt");
    expect(item.text()).toContain("已接收");
  });
});

describe("#46 跨路由 / 卸载恢复", () => {
  it("重新挂载（打开设置再回来）后仍能看到本批真实进度与未接收项", async () => {
    await dropFiles(Array.from({ length: 6 }, (_, index) => file("batch-" + (index + 1) + ".txt")));
    wrapper!.unmount();
    wrapper = mount(AddMenu, { attachTo: document.body });
    await settle();
    const panel = statusEl();
    expect(panel.exists(), "返回后批次进度消失了").toBe(true);
    expect(panel.text()).toContain("已接收 5 份 / 未接收 1 份");
    expect(panel.text()).toContain("batch-6.txt");
  });

  it("离开页面时仍在飞的上传：如实记「结果未知」并给重试入口，不永远转圈", async () => {
    const uploading = deferred<AttachmentRef>();
    mocks.uploadAttachment.mockReturnValueOnce(uploading.promise);
    await dropFiles([file("flying.txt")]);
    expect(batchItems()[0].status).toBe("uploading");

    wrapper!.unmount();
    wrapper = mount(AddMenu, { attachTo: document.body });
    await settle();
    const items = batchItems();
    expect(items[0].status).toBe("failed");
    expect(items[0].error || "").toContain("结果未知");
    const panel = statusEl();
    expect(panel.text()).toContain("失败");
    expect(panel.find('[data-im="card-attach-retry"]').exists()).toBe(true);
    // 迟到的上传回执不再写板面（alive=false）
    uploading.resolve(attachment("flying.txt"));
    await settle();
    expect(batchItems()[0].status).toBe("failed");
  });
});