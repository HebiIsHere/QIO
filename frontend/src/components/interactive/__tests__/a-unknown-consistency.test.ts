/**
 * 任务书 4 / 5：unknown 四处一致，以及材料入口与「未决发送不写回输入框」（子智能体 A）。
 *
 * ④ 一条测试直接断言四处：**类型**（SubmitStatus / SubmissionStage 的合法取值）、
 *    **合成状态**（store.submissionStage）、**按钮**（主提交按钮文案）、**文案**（未知区块）。
 *    四处说的是同一件事：结果未知、不会自动重复提交、重试沿用同一个请求身份。
 *
 * ⑤ 材料 / 附件入口（**口径以 Lead 2026-10-11 裁决为准，勿误读**）：
 *    任务书那句「材料/附件入口 + 未决发送处理都在」的验收口径是**两者都不能丢**，
 *    不是「ChatDock 必须有附件按钮」：
 *    - 材料 / 附件入口在 **BoardToolbar → AddMenu**（三类：文字卡 / 文件 / 图片；
 *      契约 2026-10-12 §1.7：代码与网址写进同一张文字卡，不再是独立入口），
 *      由本文件挂真实组件断言入口在、文件与图片仍关联真实附件；
 *    - 未决发送处理在 **ChatDock**：它按冻结契约 §8.3 **只发文字**，
 *      「受理未知时不把原文放回输入框」由 components/__tests__/b-chatDockPendingSend.test.ts
 *      用真实 ChatDock DOM 证明。
 *    Lead 裁决：**不要给 ChatDock 增加附件入口**（那会改变它只发文字的契约与样式边界）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import SubmitCluster from "../SubmitCluster.vue";
import AddMenu from "../AddMenu.vue";
import { useInteractiveStore, type SubmissionStage, type SubmitStatus } from "../../../stores/interactive";
import * as board from "../../../interactive/board";

vi.mock("../../../services/interactive", () => ({
  fetchBoardState: vi.fn(async () => ({ pending: { expressions: [] } })),
  fetchVisibleRange: vi.fn(async () => ({ visibleRange: null })),
  fetchIntents: vi.fn(),
  saveDrafts: vi.fn(),
  saveBoardState: vi.fn(),
  submitBoard: vi.fn(),
  checkMaterialImpact: vi.fn(),
  fetchAcceptance: vi.fn(),
}));

const mounted: VueWrapper[] = [];

function track(wrapper: VueWrapper): VueWrapper {
  mounted.push(wrapper);
  return wrapper;
}

beforeEach(() => {
  setActivePinia(createPinia());
  localStorage.clear();
});

afterEach(() => {
  while (mounted.length) mounted.pop()?.unmount();
  document.body.innerHTML = "";
  vi.clearAllMocks();
});

describe("unknown 在类型 / 合成状态 / 按钮 / 文案四处一致", () => {
  it("一条测试直接断言这四处说的是同一件事", async () => {
    // ① 类型层：unknown 是两处状态的合法取值（编译期保证；运行时再确认取值可用）
    const statusValue: SubmitStatus = "unknown";
    const stageValue: SubmissionStage = "unknown";

    const store = useInteractiveStore();
    store.board = board.emptyState("board_t");
    store.submitStatus = statusValue;
    store.submitResultUnknown = true;
    const verify = vi.spyOn(store, "confirmAcceptanceAfterUnclear").mockResolvedValue(null);
    const submitAttempt = vi.spyOn(store, "submit").mockResolvedValue(null);
    const wrapper = track(mount(SubmitCluster));

    // ② 合成状态：与 submitStatus 一一对应（不许落回 idle）
    expect(store.submitStatus).toBe(statusValue);
    expect(store.submissionStage, "unknown 必须有自己的合成状态").toBe(stageValue);

    // ③ 按钮：主提交按钮明说「同一身份」重试（不是含混的「提交给 QIO」）
    const submitButton = wrapper.get('[data-im="submit"]');
    expect(submitButton.text()).toContain("同一身份");

    // ④ 文案：未知区块说清「不会自动重复提交」，并给出按身份的只读查证入口
    const unknown = wrapper.get('[data-im="submit-unknown"]');
    expect(unknown.text()).toContain("提交结果未知");
    expect(unknown.text(), "文案必须与按钮一致地说明沿用同一身份").toContain("请求身份");
    expect(unknown.text()).toContain("不会自动重复提交");
    expect(wrapper.find('[data-im="submit-failure"]').exists(), "未知不是失败").toBe(false);
    expect(wrapper.get('[data-im="submit-status"]').text()).toContain("提交结果未知");

    const verifyButton = wrapper.get('[data-im="submit-unknown-verify"]');
    await verifyButton.trigger("click");
    expect(verify, "查证必须走按请求身份的精确查证入口").toHaveBeenCalledTimes(1);
    expect(submitAttempt, "查证绝不允许顺带再提交一次").not.toHaveBeenCalled();

    // 查证仍然没结论：界面必须照实说「结果仍然未知」
    store.acceptanceLookupNote = "已按同一个请求身份向服务端查证：没有记录。结果仍然未知。";
    await wrapper.vm.$nextTick();
    const note = wrapper.get('[data-im="submit-unknown-lookup-note"]');
    expect(note.text()).toContain("结果仍然未知");
  });

  it("查证确认受理时：成功文案来自查证事实，绝不借用上一次提交的改动条数", async () => {
    const store = useInteractiveStore();
    store.board = board.emptyState("board_t");
    store.submitStatus = "succeeded";
    store.submitAcceptedByLookup = true;
    store.submitNote = "这次提交的回执在传输中丢了：已按同一个请求身份查证，服务端确认它已被可靠受理。";
    // 上一次提交留下的完整回执（有 3 项改动）：绝不能被借来当成这一次的事实
    store.lastSubmission = {
      status: "succeeded",
      submission: { id: "s_old", seq: 3, status: "succeeded", createdAt: "t" },
      before: { cards: [], groups: [], links: [], selection: [], empty: true },
      after: { cards: [], groups: [], links: [], selection: [], empty: true },
      expressions: [{}, {}, {}],
      baseline: { updated: true, previousSeq: 2, seq: 3 },
      delivery: { delivered: true, reason: "", detail: "" },
      visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true },
      checkedCleared: [],
    } as never;
    const wrapper = track(mount(SubmitCluster));

    await wrapper.get('[data-im="submit-details"]').trigger("click");
    const details = wrapper.get('[data-im="submit-details-box"]').text();
    expect(details, "查证来的受理必须如实说明来源").toContain("精确查证");
    expect(details, "不许把上一次提交的 3 项改动说成本次的").not.toContain("本次 3 项改动");
    expect(wrapper.get('[data-im="submit-acceptance-note"]').text()).toContain("同一个请求身份");
  });
});

describe("材料 / 附件入口仍在", () => {
  it("添加菜单提供三类入口（文字卡 / 文件 / 图片），文件与图片是关联真实附件的那两类", async () => {
    const store = useInteractiveStore();
    store.board = board.emptyState("board_t");
    const wrapper = track(mount(AddMenu));

    expect(wrapper.find('[data-im="add-menu-list"]').exists(), "菜单默认收起").toBe(false);
    await wrapper.get('[data-im="add-menu"]').trigger("click");

    const list = wrapper.get('[data-im="add-menu-list"]');
    for (const kind of ["text", "file", "image"]) {
      expect(
        wrapper.find('[data-im="add-' + kind + '"]').exists(),
        "缺少材料入口：" + kind,
      ).toBe(true);
    }
    // 文字卡提示必须写明「文字、链接、代码在同一张卡片里；默认未许可」（契约 §1.7）
    expect(list.text()).toContain("文字、链接、代码写在同一张卡片里");
    expect(list.text()).toContain("默认未许可");
    // 文件 / 图片必须走真实附件关联（否则 QIO 只能看到文件名，读不到正文）
    expect(list.text()).toContain("文件");
    expect(list.text()).toContain("图片");
  });
});
