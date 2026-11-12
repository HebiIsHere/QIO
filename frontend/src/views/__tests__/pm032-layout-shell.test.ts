/**
 * PM-QIO032 · i-layout 页面级接线与外壳回归（#05/#14/#15/#16/#20/#40/#55/#71/#73/#80/#81/#86/#88 + 文案 1-5/13）
 *
 * 口径：
 * - 纯函数（面板空间 / 模式入口布局 / 收起态状态文案）直接跑边界值；
 * - 组件级契约用源码断言（结构、文案、唯一入口、层级、inert 门禁）——
 *   真实窗口 E2E 由 Lead 在 800×600、1121/1120、Tab/Esc、主题下做最终验收。
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  CONVERSATION_INLINE_MIN_WIDTH,
  conversationComposerLayout,
  conversationStatusText,
} from "../../components/interactive/v2/conversationLayout";
import {
  PANEL_SIDE_BY_SIDE_MIN_WIDTH,
  panelSpaceMode,
} from "../../components/interactive/v2/layout";

const SRC = resolve(process.cwd(), "src");
const read = (path: string): string => readFileSync(resolve(SRC, path), "utf8");

const view = read("views/InteractiveView.vue");
const conversation = read("components/interactive/v2/InteractiveConversation.vue");
const toolbar = read("components/interactive/BoardToolbar.vue");
const submit = read("components/interactive/SubmitCluster.vue");
const changes = read("components/interactive/BoardChangeList.vue");
const bookmarks = read("components/interactive/v2/InteractiveBookmarks.vue");
const chrome = read("components/interactive/v2/InteractiveChromeTools.vue");
const stream = read("components/MessageStream.vue");

function count(haystack: string, needle: string): number {
  return haystack.split(needle).length - 1;
}

describe("#05/#71 面板空间规则：宽窗并排、窄窗明确切换", () => {
  it("1120/1121 是实际边界（窄窗 switch，宽窗 side-by-side）", () => {
    expect(PANEL_SIDE_BY_SIDE_MIN_WIDTH).toBe(1121);
    expect(panelSpaceMode(800)).toBe("switch");
    expect(panelSpaceMode(1120)).toBe("switch");
    expect(panelSpaceMode(1121)).toBe("side-by-side");
    expect(panelSpaceMode(1920)).toBe("side-by-side");
    expect(panelSpaceMode(0)).toBe("switch");
    expect(panelSpaceMode(Number.NaN)).toBe("switch");
  });

  it("页面壳窄窗时任务面板与提交详情互相明确切换（各自状态仍由自己保留）", () => {
    expect(view).toContain("onSubmitPanelOpen");
    expect(view).toContain("store.closeTaskCenter()");
    expect(view).toContain("toolbar.closeSubmitDetails()");
    expect(view).toContain(":data-im-panel-space");
    expect(toolbar).toContain("closeSubmitDetails");
    expect(submit).toContain("closePanel");
  });

  it("执行记录层级高于聊天与任务面板（只开任务和聊天也遮不住执行记录，#71）", () => {
    // 聊天：dialog + 20；任务面板：dialog + 10；执行记录：dialog + 24/25
    const chat = /--im-z-dialog\)\s*\+\s*(\d+)/.exec(view)?.[1];
    const task = /--im-z-dialog\)\s*\+\s*(\d+)/.exec(
      read("components/interactive/v2/TaskCenterPanel.vue"),
    )?.[1];
    const record = /--im-z-dialog\)\s*\+\s*(\d+)/.exec(
      read("components/interactive/ExecutionRecordPanel.vue"),
    )?.[1];
    expect(Number(chat)).toBeGreaterThan(Number(task));
    expect(Number(record)).toBeGreaterThan(Number(chat));
  });
});

describe("#14 常态工具栏移除未开放标注入口", () => {
  it("工具栏源码里不再有标注入口 / 未开放菜单", () => {
    expect(toolbar).not.toContain('data-im="annotate');
    expect(toolbar).not.toContain("annotateOpen");
    expect(toolbar).not.toContain("未开放");
  });

  it("添加 / 撤销重做 / 书签 / 提交仍在（移除的是未开放入口，不是工具）", () => {
    expect(toolbar).toContain("<AddMenu");
    expect(toolbar).toContain('data-im="undo"');
    expect(toolbar).toContain('data-im="redo"');
    expect(toolbar).toContain('data-im="bookmarks-toggle"');
    expect(toolbar).toContain("<SubmitCluster");
  });
});

describe("#15/#86 底部两层：一排板面工具 + 一排聊天输入；聊天只有一个开合入口", () => {
  it("对话区结构：工具栏在输入区上方，输入区始终挂载", () => {
    const toolbarIndex = conversation.indexOf('class="ic-toolbar"');
    const composerIndex = conversation.indexOf('class="ic-composer"');
    expect(toolbarIndex).toBeGreaterThan(-1);
    expect(composerIndex).toBeGreaterThan(toolbarIndex);
  });

  it("消息滚动区上方不再有重复的关闭按钮；唯一入口在输入区旁", () => {
    expect(conversation).not.toContain("data-ic-close");
    expect(count(conversation, 'data-ic-toggle')).toBe(1);
    const barIndex = conversation.indexOf('class="ic-bar"');
    const composerIndex = conversation.indexOf('class="ic-composer"');
    expect(barIndex).toBeGreaterThan(-1);
    expect(composerIndex).toBeGreaterThan(barIndex);
  });

  it("板面「添加」与随消息附件入口是两个用途（工具槽 vs 输入区入口行）", () => {
    expect(view).toContain("<template #toolbar>");
    expect(conversation).toContain("<ComposerEntries");
    expect(conversation).toContain("context=\"interactive\"");
  });
});

describe("文案 4 / 13：展开 / 收起只写「展开对话」「收起对话」，折叠后说真实状态", () => {
  it("收起态状态文案只用真实会话状态，没有状态时安静", () => {
    expect(conversationStatusText({ turnRunning: true })).toBe("正在回复…");
    expect(conversationStatusText({ turnRunning: true, queuedCount: 2 })).toBe("正在回复 · 已排队 2 条");
    expect(conversationStatusText({ hasDraft: true })).toBe("草稿已保留");
    expect(conversationStatusText({})).toBe("");
    expect(conversationStatusText({ queuedCount: -3 })).toBe("");
  });

  it("入口文案去掉「点开继续对话」与展开方向说明", () => {
    expect(conversation).not.toContain("点开继续对话");
    expect(conversation).not.toContain("从输入区向上展开");
    expect(conversation).not.toContain("不影响正在进行的回复");
    expect(conversation).toContain(":aria-label=\"expandedState ? '收起对话' : '展开对话'\"");
    // 折叠有进行中回复时真实状态仍要显示（不是只有展开才看得到）
    expect(conversation).toContain("v-if=\"statusText\"");
  });
});

describe("#88 模式入口布局按实际可用宽度提前切换", () => {
  it("纯函数边界：量不到宽度时先 stacked（圆点完整可见），够宽才 inline", () => {
    expect(CONVERSATION_INLINE_MIN_WIDTH).toBeGreaterThan(860);
    expect(conversationComposerLayout(0)).toBe("stacked");
    expect(conversationComposerLayout(Number.NaN)).toBe("stacked");
    expect(conversationComposerLayout(CONVERSATION_INLINE_MIN_WIDTH - 1)).toBe("stacked");
    expect(conversationComposerLayout(CONVERSATION_INLINE_MIN_WIDTH)).toBe("inline");
    expect(conversationComposerLayout(1600)).toBe("inline");
  });

  it("组件用实测宽度（data-ic-layout）而不是视口媒体查询", () => {
    expect(conversation).toContain("conversationComposerLayout");
    expect(conversation).toContain(':data-ic-layout="composerLayout"');
    expect(conversation).not.toContain("@media (max-width: 1120px)");
    // 窄档不再用 padding-left 硬挪工具组：工具组与输入框同一条 860 列
    expect(conversation).not.toContain("padding-left: 92px");
    expect(conversation).toContain('[data-ic-layout="stacked"] .ic-toolbar');
  });
});

describe("#16 互动空态只提示发消息或提交板面", () => {
  it("MessageStream 增加互动专属空态，其它模式空态保持原样", () => {
    expect(stream).toContain("emptyVariant");
    expect(stream).toContain("发消息，或提交板面");
    expect(stream).toContain("今天想聊点什么？");
    expect(stream).toContain("话题星球在右下角");
    expect(conversation).toContain('empty-variant="interactive"');
  });
});

describe("#55 板内搜索无命中：保留结果层并显示「没有找到卡片」", () => {
  it("结果层不再以命中数作为渲染条件；无命中时给明确空态", () => {
    expect(chrome).toContain("没有找到卡片");
    expect(chrome).toContain('v-if="resultsOpen"');
    expect(chrome).not.toContain('v-if="resultsOpen && hits.length"');
    expect(chrome).toContain('data-iw-chrome-search-empty');
  });
});

describe("#73 书签栏：紧凑、Esc/工具栏可收起、长列表单独滚动", () => {
  it("打开时注册 Esc 层；关闭入口在头部；列表自己滚动", () => {
    expect(bookmarks).toContain("useEscapeLayer");
    expect(bookmarks).toContain("interactive-bookmarks");
    expect(bookmarks).toContain("data-im=\"bookmarks-panel\"");
    expect(bookmarks).toContain(".bk-close");
    expect(bookmarks).toMatch(/\.bk-list\s*\{[^}]*overflow-y:\s*auto/);
    expect(bookmarks).toMatch(/\.bk-list\s*\{[^}]*flex:\s*1/);
  });
});

describe("#20 整板读取失败：只读展示 / 首次失败两个入口 / 失败期间不编辑不提交", () => {
  it("首次失败给「重试」「返回对话」", () => {
    expect(view).toContain("boardFirstLoadFailed");
    expect(view).toContain('data-im="board-load-failed"');
    expect(view).toContain('data-im="board-load-retry"');
    expect(view).toContain('data-im="board-load-back"');
    expect(view).toContain("返回对话");
    expect(view).toContain("backToConversation");
  });

  it("有上次成功数据只读展示 + 重试；画布与工具栏在失败期间 inert", () => {
    expect(view).toContain("boardReadOnly");
    expect(view).toContain("只读展示上次成功读取的内容");
    expect(view).toContain(":inert=\"boardReadOnly");
    expect(view).toContain("retryBoardLoad");
    // 事实来源：i-state 的 boardLoadedOnce/boardReadOnly/retryBoardLoad（可选读取，落地即生效）
    expect(view).toContain("boardEditBlockedReason");
  });

  it("重试只读路径不重建板面（按钮只调 retryBoardLoad，不调 submit）", () => {
    const failedBlock = view.slice(view.indexOf("board-load-failed"));
    expect(failedBlock).not.toContain("store.submit()");
  });
});

describe("#40 Esc 收起最上层可关闭的非模态面板（不清草稿、不停回复、输入法优先）", () => {
  it("对话区：展开时注册共用 Esc 层，只收起显示", () => {
    expect(conversation).toContain("registerEscapeLayer");
    expect(conversation).toContain('id: "interactive-conversation"');
    expect(conversation).toContain("collapse();");
  });

  it("提交详情：面板打开时注册 Esc 层（焦点在聊天输入框也能收起）", () => {
    expect(submit).toContain("registerEscapeLayer");
    expect(submit).toContain('id: "submit-cluster-panel"');
    expect(submit).toContain("closePanel();");
  });

  it("任务面板：页面壳注册一层；书签栏自己注册一层（同一套层级栈，输入法在公共层处理）", () => {
    expect(view).toContain("registerEscapeLayer");
    expect(view).toContain('id: "interactive-task-center"');
    expect(bookmarks).toContain("useEscapeLayer");
    expect(bookmarks).toContain("onEscape");
  });
});

describe("#03/#13/#80/#81 提交区主状态与门禁（数据由 i-state 提供，UI 只消费）", () => {
  it("#03 主状态一处呈现，轮次状态只从 store 读", () => {
    expect(submit).toContain("primaryStatusLabel");
    expect(submit).toContain("roundStatusLabel");
    expect(submit).toContain("roundStatusDetail");
    expect(count(submit, 'data-im="submit-status"')).toBe(1);
  });

  it("#13 无有效内容时禁用提交并提示原因（canSubmit / submitBlockedReason 来自 store）", () => {
    expect(submit).toContain("canSubmit");
    expect(submit).toContain("submitBlockedReason");
    expect(submit).toContain(':disabled="busy || !canSubmit"');
    expect(submit).toContain("没有可提交的有效改动");
  });

  it("#80 重复提交显示「已提交过」并可查看原进度", () => {
    expect(submit).toContain('data-im="submit-duplicate"');
    expect(submit).toContain("已提交过");
    expect(submit).toContain("查看原进度");
    expect(submit).toContain('submitStatus === "duplicate"');
  });

  it("#81 回执待确认：底部一行短状态 +「查提交结果」，细节按需展开", () => {
    expect(submit).toContain("提交结果待确认，改动已保留");
    expect(submit).toContain("查提交结果");
    expect(submit).toContain('data-im="submit-unknown-details"');
    // 不再把查证与重新读取两个按钮堆在底部一行
    const unknownBlock = submit.slice(
      submit.indexOf('data-im="submit-unknown"'),
      submit.indexOf('data-im="submit-duplicate"'),
    );
    expect(unknownBlock).not.toContain("submit-unknown-refresh");
  });
});

describe("#76 工具区连续、轻量的承载面（文字/连线不从按钮间隙穿出）", () => {
  it("悬浮工具栏有条连续承载面，按钮退为透明表面", () => {
    expect(view).toContain("#76");
    expect(view).toContain(".ic-toolbar .board-toolbar");
    expect(view).toMatch(/border-radius:\s*var\(--r-pill\)/);
    expect(view).toContain("backdrop-filter: blur(10px)");
    // 承载面不再是完全透明（否则卡片文字会从按钮间隙透出）
    expect(view).not.toMatch(/\.ic-toolbar \.board-toolbar\) \{\s*padding: 0;\s*border: 0;\s*border-radius: 0;\s*background: transparent;/);
  });
});

describe("#41 提交详情三态：有效空态 / 读取失败 / 上次结果（数据由 i-state 提供）", () => {
  it("读取失败三态 + 重试入口；只有成功读取为空才允许空态文案", () => {
    expect(submit).toContain("understandingLoadState");
    expect(submit).toContain("understandingLoadedOnce");
    expect(submit).toContain('data-im="submit-last-read-error"');
    expect(submit).toContain('data-im="submit-last-read-retry"');
    expect(submit).toContain('data-im="submit-last-result-empty"');
    expect(submit).toContain("store.refreshUnderstanding()");
    // 本次改动列表只在读取成功后渲染；失败处给「重试读取」而不是「本次没有改动」
    expect(submit).toContain('v-if="!pendingError"');
    expect(submit).toContain('data-im="submit-details-retry"');
    expect(submit).toContain("本次没有改动");
  });
});

describe("文案 1/2/3：回执/改动/详情", () => {
  it("文案 1：待确认只写「提交结果待确认，改动已保留」；查证无记录用「暂未查到这次提交」", () => {
    expect(submit).toContain("提交结果待确认，改动已保留");
    // 查证无记录文案由 store（i-state）给出；UI 不改写。这里守住 UI 不自己拼旧文案。
    expect(submit).not.toContain("查证受理结果（按请求身份）");
  });

  it("文案 2：改动标题用「本次改动 · N 项」，每行只出现一次改动类别", () => {
    expect(changes).toContain('"本次改动 · " + summary.value.total + " 项"');
    expect(changes).toContain("rowSummary");
    expect(count(changes, 'data-im="change-kind"')).toBe(1);
  });

  it("文案 3：提交详情图标提示只写「提交详情」", () => {
    expect(submit).toContain('aria-label="提交详情"');
    expect(submit).toContain("v-qio-tip=\"'提交详情'\"");
    expect(submit).not.toContain("提交状态与详情");
    expect(submit).not.toContain("点击查看详情");
  });

  it("文案 5：任务入口悬停只写「互动任务」", () => {
    expect(view).toContain('"互动任务"');
    expect(view).toContain("tasksEntryTip");
    expect(view).not.toContain("互动任务：执行中可暂停");
  });
});
