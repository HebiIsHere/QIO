/**
 * 失败文案层级 + 可达重试入口（C 本轮，实机验收点 4）。
 *
 * 触发（实测，亮色 1440×900）：默认失败区把底层原文整条铺出来，里面带
 * `/api/interactive/boards/board_default/submissions -> 500: …`；
 * 同时「保存前的影响预判没完成」这条**会阻塞提交**的状态在界面上没有任何入口（只存在 store 里），
 * 用户点提交只会得到一句「本次未提交」，不知道怎么恢复。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import SubmitCluster from "../SubmitCluster.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import * as board from "../../../interactive/board";

vi.mock("../../services/interactive", () => ({
  fetchBoardState: vi.fn(async () => ({
    boardId: "board_t",
    seq: 1,
    updatedAt: "2026-10-08T00:00:00.000Z",
    cards: [],
    groups: [],
    links: [],
    selection: [],
    pending: { expressions: [] },
  })),
}));

beforeEach(() => {
  setActivePinia(createPinia());
  const store = useInteractiveStore();
  store.board = board.emptyState("board_t");
});

function mountCluster(): VueWrapper {
  return mount(SubmitCluster);
}

describe("默认区只说一句人话，完整原文仍在详情里", () => {
  it("接口路径与状态码前缀不出现在界面上", () => {
    const store = useInteractiveStore();
    store.submitStatus = "failed";
    store.submitError = "/api/interactive/boards/board_t/submissions -> 500: 服务端处理这次提交时出错了";
    store.saveStatus = "saved";
    const w = mountCluster();
    const failure = w.find('[data-im="submit-failure"]').text();
    expect(failure).toContain("服务端处理这次提交时出错了");
    expect(failure).not.toContain("/api/");
    expect(failure).not.toContain("500");
    expect(w.find('[data-im="submit-failure-reason"]').text()).toContain("失败原因：");
  });

  it("长原文在默认区仍然完整（上一轮验收要求），但已去掉实现用语", () => {
    const store = useInteractiveStore();
    const long = "网络中断：" + "这是一段很长的失败原因说明。".repeat(30);
    store.submitStatus = "failed";
    store.submitError = long;
    store.saveStatus = "saved";
    const w = mountCluster();
    expect(w.find('[data-im="submit-failure-reason"]').text()).toContain(long);
    expect(w.find('[data-im="submit-failure-reason"]').text()).not.toContain("/api/");
  });

  it("重复包裹合成一句：同一个说明不再出现两次", () => {
    const store = useInteractiveStore();
    store.submitStatus = "failed";
    store.submitError =
      "保存前的影响预判没有完成，本次未提交（这次保存前的影响预判没有完成，保存已暂停（磁盘写满））";
    store.saveStatus = "saved";
    const w = mountCluster();
    const text = w.find('[data-im="submit-failure-reason"]').text();
    expect(text).toBe("失败原因：保存前的影响预判没有完成；磁盘写满");
  });

  it("重试按钮依然是「重新提交」且可用", () => {
    const store = useInteractiveStore();
    store.submitStatus = "failed";
    store.submitError = "网络中断";
    store.saveStatus = "saved";
    const w = mountCluster();
    const button = w.find('[data-im="submit"]');
    expect(button.attributes("disabled")).toBeUndefined();
    expect(button.text()).toContain("重新提交");
  });
});

describe("保存前的影响预判没完成：一次可达的重试入口", () => {
  it("只有真的有错时才出现，且只调一次 saveNow（不自动重试、不自动提交）", async () => {
    const store = useInteractiveStore();
    const spy = vi.spyOn(store, "saveNow").mockResolvedValue(undefined);
    const clean = mountCluster();
    expect(clean.find('[data-im="submit-recheck"]').exists()).toBe(false);
    clean.unmount();

    store.impactCheckError = "这次保存前的影响预判没有完成，保存已暂停（/api/interactive/boards/board_t/state -> 500: 网络不通）";
    const w = mountCluster();
    const entry = w.find('[data-im="submit-recheck"]');
    expect(entry.exists()).toBe(true);
    // 入口文案是人话：不出现接口路径
    expect(w.find('[data-im="submit-blocked-reason"]').text()).not.toContain("/api/");
    expect(entry.text()).toContain("重新预判并保存");
    await entry.trigger("click");
    expect(spy).toHaveBeenCalledTimes(1);
  });
});
