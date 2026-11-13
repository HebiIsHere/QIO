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
  pickBrowserFile: vi.fn(),
}));

vi.mock("../../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/attachments")>();
  return {
    ...actual,
    uploadAttachment: mocks.uploadAttachment,
    waitUntilSettled: mocks.waitUntilSettled,
    registerSourceAttachment: mocks.registerSourceAttachment,
    pickBrowserFile: mocks.pickBrowserFile,
  };
});

import AddMenu from "../AddMenu.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { addCard, emptyState, removeCard, updateCard } from "../../../interactive/board";
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
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
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
  mocks.pickBrowserFile.mockResolvedValue(null);
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

describe("#46 离开互动页后继续上传和关联", () => {
  function mountMenu(): VueWrapper {
    wrapper = mount(AddMenu, { attachTo: document.body });
    return wrapper;
  }
  function unmountMenu(): void {
    wrapper!.unmount();
    wrapper = null;
  }
  function liveCards() {
    return (store.board?.cards ?? []).filter((card) => !card.deleted);
  }
  function linkedAttachmentId(cardId: string): string {
    const card = liveCards().find((item) => item.id === cardId);
    const block = (card?.meta ?? {}).file as { attachmentId?: string } | undefined;
    return block?.attachmentId ?? "";
  }

  it("重新挂载（打开设置再回来）后仍能看到本批真实进度与未接收项", async () => {
    await dropFiles(Array.from({ length: 6 }, (_, index) => file("batch-" + (index + 1) + ".txt")));
    unmountMenu();
    mountMenu();
    await settle();
    const panel = statusEl();
    expect(panel.exists(), "返回后批次进度消失了").toBe(true);
    expect(panel.text()).toContain("已接收 5 份 / 未接收 1 份");
    expect(panel.text()).toContain("batch-6.txt");
  });

  it("发起上传 → 去设置 → 上传成功 → 回互动页：原卡已关联、只关联一次、批次为成功、没有要求重选文件", async () => {
    const flying = deferred<AttachmentRef>();
    mocks.uploadAttachment.mockReturnValueOnce(flying.promise);
    await dropFiles([file("跨页上传.txt")]);
    expect(batchItems()[0].status, "上传应已发出").toBe("uploading");

    // 去设置：互动页卸载（批次事实与执行器都在 store，不应因此丢失回执）
    unmountMenu();

    // 离开期间上传成功并落定
    flying.resolve(attachment("跨页上传.txt", { id: "att_flight" }));
    await settle();

    const batch = store.currentImportBatch!;
    expect(batch.items[0].status, "离开期间到达的成功回执被丢掉了").toBe("succeeded");
    expect(batch.items[0].attachmentId).toBe("att_flight");
    expect(batch.items[0].error).toBeNull();

    // 回互动页
    mountMenu();
    await settle();

    // 原卡：恰好一张、只关联一次、绑到真实附件 id
    const live = liveCards();
    expect(live, "原板面没有建卡，或建了重复卡").toHaveLength(1);
    expect(live[0].kind).toBe("file");
    expect(linkedAttachmentId(live[0].id)).toBe("att_flight");
    expect(mocks.uploadAttachment, "回执被重复处理（重复上传）").toHaveBeenCalledTimes(1);

    const panel = statusEl();
    expect(panel.exists(), "返回后看不到本批真实状态").toBe(true);
    expect(panel.text()).toContain("已接收 1 份 / 未接收 0 份");
    expect(panel.find('[data-im="file-card-error"]').exists(), "成功项不该有失败原因").toBe(false);
    expect(panel.find('[data-im="card-attach-retry"]').exists(), "成功项不该要求重新选择文件").toBe(false);
    expect(mocks.pickBrowserFile, "成功回执之后被要求重新选文件").not.toHaveBeenCalled();
  });

  it("回互动页后才落定：先显示真实进度，收到回执后显示已接收（不重复建卡）", async () => {
    const flying = deferred<AttachmentRef>();
    mocks.uploadAttachment.mockReturnValueOnce(flying.promise);
    await dropFiles([file("返回后完成.txt")]);
    unmountMenu();
    mountMenu();
    await settle();

    // 返回时是真实进度，不是「结果未知」
    const panel = statusEl();
    expect(panel.exists()).toBe(true);
    expect(panel.text()).toContain("上传中");
    expect(panel.text()).toContain("进行中 1 份");
    expect(batchItems()[0].status).toBe("uploading");

    flying.resolve(attachment("返回后完成.txt", { id: "att_later" }));
    await settle();

    expect(batchItems()[0].status).toBe("succeeded");
    expect(statusEl().text()).toContain("已接收 1 份 / 未接收 0 份");
    const live = liveCards();
    expect(live).toHaveLength(1);
    expect(linkedAttachmentId(live[0].id)).toBe("att_later");
  });

  it("离开期间首件失败、后续照常：返回后逐项真实状态，重试只重传这一项", async () => {
    const first = deferred<AttachmentRef>();
    mocks.uploadAttachment.mockReturnValueOnce(first.promise);
    await dropFiles([file("失败-1.txt"), file("正常-2.txt")]);
    unmountMenu();
    first.reject(new Error("上传超时"));
    await settle();
    mountMenu();
    await settle();

    const items = batchItems();
    expect(items[0].status, "首件失败没有如实落到批次").toBe("failed");
    expect(items[0].error || "").toContain("上传超时");
    expect(items[1].status, "首件失败影响了后续文件").toBe("succeeded");
    expect(liveCards()).toHaveLength(1);

    const retries = statusEl().findAll('[data-im="card-attach-retry"]');
    expect(retries, "失败项没有单独的重试入口").toHaveLength(1);
    mocks.uploadAttachment.mockResolvedValueOnce(attachment("失败-1.txt", { id: "att_retry_1" }));
    await retries[0].trigger("click");
    await settle();

    expect(mocks.uploadAttachment).toHaveBeenCalledTimes(3);
    expect(batchItems()[0].status).toBe("succeeded");
    expect(batchItems()[1].status).toBe("succeeded");
    expect(liveCards()).toHaveLength(2);
    expect(mocks.pickBrowserFile, "源还在内存里，重试不该要求重新选文件").not.toHaveBeenCalled();
  });

  it("关闭批次提示不取消已发出的上传：回执仍按原批次完成建卡与关联", async () => {
    const flying = deferred<AttachmentRef>();
    mocks.uploadAttachment.mockReturnValueOnce(flying.promise);
    await dropFiles([file("关掉提示.txt")]);
    expect(batchItems()[0].status).toBe("uploading");

    await wrapper!.get('[data-im="add-material-dismiss"]').trigger("click");
    await settle();
    expect(statusEl().exists(), "批次提示没有真的关掉").toBe(false);

    flying.resolve(attachment("关掉提示.txt", { id: "att_closed" }));
    await settle();
    const live = liveCards();
    expect(live, "关闭提示等于取消了上传").toHaveLength(1);
    expect(linkedAttachmentId(live[0].id)).toBe("att_closed");
  });

  it("切到另一块板面：迟到回执不写进当前板面；回到原板面重试不需要重选文件", async () => {
    const flying = deferred<AttachmentRef>();
    mocks.uploadAttachment.mockReturnValueOnce(flying.promise);
    await dropFiles([file("跨板.txt")]);
    const originalBoard = store.board!;
    expect(batchItems()[0].status).toBe("uploading");

    // 用户打开另一块板面时回执才到
    store.boardId = "board_other";
    store.board = emptyState("board_other");
    flying.resolve(attachment("跨板.txt", { id: "att_cross" }));
    await settle();

    expect(store.board!.cards, "迟到回执写进了当前碰巧打开的板面").toHaveLength(0);
    expect(batchItems()[0].status).toBe("failed");
    expect(batchItems()[0].error || "").toContain("原板面");

    // 回到原板面后重试：源还在内存里，不需要重新选择文件
    store.boardId = "board_default";
    store.board = originalBoard;
    mocks.uploadAttachment.mockResolvedValueOnce(attachment("跨板.txt", { id: "att_cross_retry" }));
    const retries = statusEl().findAll('[data-im="card-attach-retry"]');
    expect(retries, "回到原板面后没有恢复入口").toHaveLength(1);
    await retries[0].trigger("click");
    await settle();

    expect(batchItems()[0].status).toBe("succeeded");
    const live = liveCards();
    expect(live).toHaveLength(1);
    expect(linkedAttachmentId(live[0].id)).toBe("att_cross_retry");
    expect(mocks.pickBrowserFile).not.toHaveBeenCalled();
  });

  it("单卡关联：上传发出后去设置，回执仍完成原卡关联；返回时显示真实结果", async () => {
    mocks.pickBrowserFile.mockResolvedValueOnce(new File(["x"], "单卡.txt"));
    const flying = deferred<AttachmentRef>();
    mocks.uploadAttachment.mockReturnValueOnce(flying.promise);
    await wrapper!.get('[data-im="add-menu"]').trigger("click");
    await wrapper!.get('[data-im="add-file"]').trigger("click");
    await settle();
    const card = store.board!.cards[store.board!.cards.length - 1];
    expect(card, "添加文件没有建卡").toBeTruthy();
    expect(mocks.uploadAttachment).toHaveBeenCalledTimes(1);

    unmountMenu();
    flying.resolve(attachment("单卡.txt", { id: "att_card" }));
    await settle();

    const linked = store.board!.cards.find((item) => item.id === card.id)!;
    expect((linked.meta.file as { attachmentId?: string }).attachmentId, "离开页面后回执没有完成关联").toBe(
      "att_card",
    );
    expect(liveCards()).toHaveLength(1);

    // 回互动页：按真实结果展示（同一个提示入口），不要求重选文件
    mountMenu();
    await settle();
    const status = wrapper!.find('[data-im="add-material-status-text"]');
    expect(status.exists(), "返回后没有显示这次关联的真实结果").toBe(true);
    // 跨页完成与当前页完成用**同一句**：已关联附件『文件名』
    expect(status.text()).toBe("已关联附件「单卡.txt」");
    expect(mocks.pickBrowserFile, "回执完成后不应要求重新选文件").toHaveBeenCalledTimes(1);
  });

  it("单卡关联：离开期间完成 → 返回后仍能看到真实终态（不依赖离开期间就过期的瞬时提示）", async () => {
    mocks.pickBrowserFile.mockResolvedValueOnce(new File(["x"], "离开完成.txt"));
    const flying = deferred<AttachmentRef>();
    mocks.uploadAttachment.mockReturnValueOnce(flying.promise);
    await wrapper!.get('[data-im="add-menu"]').trigger("click");
    await wrapper!.get('[data-im="add-file"]').trigger("click");
    await settle();
    const card = store.board!.cards[store.board!.cards.length - 1];
    expect(card).toBeTruthy();

    unmountMenu(); // 去设置
    flying.resolve(attachment("离开完成.txt", { id: "att_away_ok" }));
    await settle(); // 终态在离开期间落定，页面上没有任何提示在显示
    mountMenu();
    await settle();

    // 回挂后按既有提示入口显示这条导入的真实终态（不是第二个常驻通知条）
    const status = wrapper!.find('[data-im="add-material-status-text"]');
    expect(status.exists(), "返回后看不到离开期间完成的导入结果").toBe(true);
    // 与当前页完成同一句（同一份提示入口，只显示一次）
    expect(status.text()).toBe("已关联附件「离开完成.txt」");
    expect(wrapper!.findAll('[data-im="add-material-status-text"]').length, "成功提示显示了不止一次").toBe(1);
    const linked = store.board!.cards.find((item) => item.id === card.id)!;
    expect((linked.meta.file as { attachmentId?: string }).attachmentId).toBe("att_away_ok");

    // 呈现方式沿用既有成功提示：约 3 秒后自动消失（消失的是提示，卡片关联保留）
    await new Promise((resolve) => setTimeout(resolve, 3200));
    expect(wrapper!.find('[data-im="add-material-status-text"]').exists()).toBe(false);
    const stillLinked = store.board!.cards.find((item) => item.id === card.id)!;
    expect((stillLinked.meta.file as { attachmentId?: string }).attachmentId).toBe("att_away_ok");

    // 已经展示过的终态不再在下次返回时重复弹出
    unmountMenu();
    mountMenu();
    await settle();
    expect(wrapper!.find('[data-im="add-material-status-text"]').exists(), "已看过的终态又弹了一次").toBe(
      false,
    );
  });

  it("单卡关联：离开期间失败 → 返回后看到具体失败原因；重试沿用内存里的文件（不要求重选）", async () => {
    mocks.pickBrowserFile.mockResolvedValueOnce(new File(["x"], "离开失败.txt"));
    const flying = deferred<AttachmentRef>();
    mocks.uploadAttachment.mockReturnValueOnce(flying.promise);
    await wrapper!.get('[data-im="add-menu"]').trigger("click");
    await wrapper!.get('[data-im="add-file"]').trigger("click");
    await settle();
    const card = store.board!.cards[store.board!.cards.length - 1];

    unmountMenu();
    flying.reject(new Error("服务端拒绝：磁盘配额不足"));
    await settle();
    mountMenu();
    await settle();

    const status = wrapper!.find('[data-im="add-material-status-text"]');
    expect(status.exists(), "返回后看不到离开期间的失败").toBe(true);
    expect(status.text(), "失败没有具体原因").toContain("磁盘配额不足");
    expect(wrapper!.find('[data-im="add-material-retry"]').exists(), "失败没有重试入口").toBe(true);

    // 重试关联：文件还在内存里，不该要求用户重新选择
    mocks.uploadAttachment.mockResolvedValueOnce(attachment("离开失败.txt", { id: "att_away_retry" }));
    await wrapper!.get('[data-im="add-material-retry"]').trigger("click");
    await settle();
    expect(mocks.pickBrowserFile, "仅因离开页面就要求重选文件").toHaveBeenCalledTimes(1);
    const linked = store.board!.cards.find((item) => item.id === card.id)!;
    expect((linked.meta.file as { attachmentId?: string }).attachmentId).toBe("att_away_retry");

    // 结果被用户看过（重试后已经变成成功）之后，返回不再重复弹出
    unmountMenu();
    mountMenu();
    await settle();
    expect(wrapper!.find('[data-im="add-material-status-text"]').exists()).toBe(false);
  });

  it("单卡关联：原卡已删除 → 迟到回执不重建卡、不如实声称成功", async () => {
    const seeded = addCard(emptyState("board_t"), { kind: "file", meta: { name: "旧卡" } });
    store.board = seeded;
    const cardId = seeded.cards[0].id;
    const flying = deferred<AttachmentRef>();
    mocks.uploadAttachment.mockReturnValueOnce(flying.promise);
    const started = store.startCardAttachment({ cardId, source: { file: file("删卡.txt") } });
    await settle();
    expect(mocks.uploadAttachment).toHaveBeenCalledTimes(1);

    store.commit(removeCard(store.board!, cardId), "删除卡片");
    await settle();
    flying.resolve(attachment("删卡.txt", { id: "att_deleted" }));
    const result = await started.done;
    await settle();

    expect(result.ok).toBe(false);
    expect(result.error || "").toContain("原文件卡已不存在");
    expect(liveCards(), "迟到回执把删掉的卡重建了").toHaveLength(0);
  });

  it("单卡关联：原卡已换成另一份文件 → 迟到回执不覆盖新关联", async () => {
    const seeded = addCard(emptyState("board_t"), {
      kind: "file",
      meta: { name: "旧.txt", file: { attachmentId: "att_old", name: "旧.txt" } },
    });
    store.board = seeded;
    const cardId = seeded.cards[0].id;
    const flying = deferred<AttachmentRef>();
    mocks.uploadAttachment.mockReturnValueOnce(flying.promise);
    const started = store.startCardAttachment({ cardId, source: { file: file("新.txt") } });
    await settle();

    store.commit(
      updateCard(store.board!, cardId, {
        meta: { name: "别的.txt", file: { attachmentId: "att_other", name: "别的.txt" } },
      }),
      "换附件",
    );
    flying.resolve(attachment("新.txt", { id: "att_late" }));
    const result = await started.done;
    await settle();

    expect(result.ok).toBe(false);
    expect(result.error || "").toContain("已经被另一次操作替换");
    expect(linkedAttachmentId(cardId), "迟到回执覆盖了用户已换上的附件").toBe("att_other");
  });

  it("同一批次项重试产生更新尝试后：旧回执不覆盖新结果、不重复建卡", async () => {
    const batch = store.beginImportBatch([{ name: "迟到.txt" }]);
    const itemKey = batch.items[0].key;
    const first = deferred<AttachmentRef>();
    const second = deferred<AttachmentRef>();
    mocks.uploadAttachment.mockReturnValueOnce(first.promise);
    mocks.uploadAttachment.mockReturnValueOnce(second.promise);
    store.setImportItemSource(batch.id, itemKey, { file: file("迟到.txt") });

    const firstRun = store.runImportItem(batch.id, itemKey);
    await settle();
    expect(batchItems()[0].status).toBe("uploading");

    // 第一次尝试失败，用户立刻重试（产生更新的尝试）
    store.noteImportItem(batch.id, itemKey, { status: "failed", error: "第一次尝试超时" });
    const secondRun = store.runImportItem(batch.id, itemKey);
    await settle();
    expect(batchItems()[0].status).toBe("uploading");

    second.resolve(attachment("迟到.txt", { id: "att_new" }));
    await secondRun;
    await settle();
    expect(batchItems()[0].status).toBe("succeeded");
    expect(liveCards()).toHaveLength(1);

    // 旧回执此刻才到：不覆盖、不重建、不改附件
    first.resolve(attachment("迟到.txt", { id: "att_old" }));
    const firstResult = await firstRun;
    await settle();
    expect(firstResult.supersededBy).toBe("item");
    expect(batchItems()[0].status).toBe("succeeded");
    expect(batchItems()[0].attachmentId).toBe("att_new");
    const live = liveCards();
    expect(live).toHaveLength(1);
    expect(linkedAttachmentId(live[0].id)).toBe("att_new");
  });

  it("批次项绑定已有文件卡（添加 / 替换）：在原卡上关联，不新建卡", async () => {
    const seeded = addCard(emptyState("board_t"), {
      kind: "file",
      meta: { name: "旧.txt", file: { attachmentId: "att_old", name: "旧.txt" } },
    });
    store.board = seeded;
    const cardId = seeded.cards[0].id;
    const batch = store.beginImportBatch([{ name: "新.txt", cardId }]);
    const itemKey = batch.items[0].key;
    store.setImportItemSource(batch.id, itemKey, { file: file("新.txt") });
    mocks.uploadAttachment.mockResolvedValueOnce(attachment("新.txt", { id: "att_new" }));

    const result = await store.runImportItem(batch.id, itemKey);
    await settle();

    expect(result.ok).toBe(true);
    expect(result.cardId).toBe(cardId);
    expect(liveCards(), "绑定已有卡的导入又建了一张卡").toHaveLength(1);
    expect(linkedAttachmentId(cardId)).toBe("att_new");
    expect(batchItems()[0].status).toBe("succeeded");
    expect(batchItems()[0].cardId).toBe(cardId);
  });

  it("桌面原路径登记：沿用既有 registerSourceAttachment，回执后建卡并关联", async () => {
    const registering = deferred<AttachmentRef>();
    mocks.registerSourceAttachment.mockReturnValueOnce(registering.promise);
    const batch = store.beginImportBatch([{ name: "桌面文件.txt" }]);
    const itemKey = batch.items[0].key;
    store.setImportItemSource(batch.id, itemKey, { path: "C:\\tmp\\桌面文件.txt", sourceKind: "desktop_path" });

    const run = store.runImportItem(batch.id, itemKey);
    await settle();
    expect(batchItems()[0].status).toBe("uploading");
    expect(mocks.registerSourceAttachment).toHaveBeenCalledWith("C:\\tmp\\桌面文件.txt", "desktop_path");

    registering.resolve(attachment("桌面文件.txt", { id: "att_desktop" }));
    const result = await run;
    await settle();

    expect(result.ok).toBe(true);
    const live = liveCards();
    expect(live).toHaveLength(1);
    expect(linkedAttachmentId(live[0].id)).toBe("att_desktop");
    expect(batchItems()[0].status).toBe("succeeded");
  });

  it("同一张卡上更新的尝试：旧回执不覆盖新关联", async () => {
    const seeded = addCard(emptyState("board_t"), { kind: "file", meta: { name: "卡" } });
    store.board = seeded;
    const cardId = seeded.cards[0].id;
    const first = deferred<AttachmentRef>();
    const second = deferred<AttachmentRef>();
    mocks.uploadAttachment.mockReturnValueOnce(first.promise);
    mocks.uploadAttachment.mockReturnValueOnce(second.promise);

    const firstStarted = store.startCardAttachment({ cardId, source: { file: file("第一次.txt") } });
    const secondStarted = store.startCardAttachment({ cardId, source: { file: file("第二次.txt") } });
    await settle();
    second.resolve(attachment("第二次.txt", { id: "att_second" }));
    const secondResult = await secondStarted.done;
    await settle();
    expect(secondResult.ok).toBe(true);

    first.resolve(attachment("第一次.txt", { id: "att_first" }));
    const firstResult = await firstStarted.done;
    await settle();
    expect(firstResult.superseded).toBe(true);
    expect(firstResult.supersededBy).toBe("card");
    expect(linkedAttachmentId(cardId), "旧回执覆盖了更新的关联").toBe("att_second");
  });
});