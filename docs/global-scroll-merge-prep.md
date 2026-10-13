# QIO 全局滚动 / 对话横向溢出 / 表格布局 —— 合并准备材料

> 状态：**独立分支开发完成，合并材料已准备，未开始合并。**
> 未合并 main，未合并另一修复任务，未打标签、未打包发布。

## 1. 分支、基线与工作树

| 项 | 值 |
| --- | --- |
| 分支 | `fix/global-scroll-dialog-layout` |
| worktree | `D:\qio-dev\qio-gs-lead` |
| 冻结基线 BASE_SHA | `17f28f7bee410def54ee0cfcc72853cbc99e0507`（开工时核对取得的 origin/main，全程未变） |
| 最终 HEAD_SHA | 见本文末尾「最终状态」一节（提交后写入） |
| 工作树状态 | 干净（`git status --porcelain` 为空） |
| 远端 | `git@github.com:HebiIsHere/QIO.git` |
| 开发期冻结 | 未 pull / 未 rebase / 未合并 main；另一修复分支从未被写入 |

子任务 worktree（各自独立，等价于内部评审后收拢的分支）：

| 子任务 | 分支 | worktree | 负责范围 |
| --- | --- | --- | --- |
| 共享原语（Lead） | `fix/global-scroll-dialog-layout` | `qio-gs-lead` | 令牌 / 全局样式 / 指示条引擎 / 表格适配 / MarkdownContent / 文档 |
| 对话与输入 | `wt/gs-chat` | `qio-gs-chat` | MessageStream / MessageItem / Composer / TurnProcess / ContinueBar / QueueChip / AttachmentChip / InterruptedTurnEntry / ConversationView |
| 互动对话面板 | `wt/gs-dock` | `qio-gs-dock` | ChatDock / interactive-shell.css |
| 板面 / 卡片 / 预览 | `wt/gs-board` | `qio-gs-board` | BoardCanvas / BoardCard / CardInlinePreview / CardAttachmentPreview / TextCardEditor / 组框 / 搜索 / 改动列表 / SelectionMenu / 板面工具条 |
| 其它页面与浮层 | `wt/gs-pages` | `qio-gs-pages` | 设置 / 星球 / 调试 / 审批 / 凭据 / 引导 / 意图与失败恢复 |
| 独立复核 | `wt/gs-verify` | `qio-gs-verify` | 只写 `D:\qio-dev\_gs-verify\`，不改源码 |

## 2. 提交清单（相对冻结基线，18 个非合并提交 + 6 个收拢合并）

```
69eec7d7 fix(scroll): 零尺寸宿主不算可滚动范围（折叠面板的假溢出与假轨道）
1b909a4b fix(scroll): 两处折叠裁剪盒改 overflow:clip，不再建立可滚动容器
d74ad1b3 fix(markdown): 表格身份只取决于源码（同一 source 同一 HTML）
618f48c0 fix(scroll): 其它页面与浮层接入统一滚动策略 + 横向边界修复（task-4 D6）
b19ef2c6 docs(scroll): 预览表格的交付口径写进代码注释（记录模式仅用于有真实表头的表格）
fe8165b4 fix(scroll): 失败恢复浮层的滚动层收敛（task-7）
3a62be76 test(scroll): 横向不溢出的真实测量口径 + 复核钩子（Lead 收尾条件 2）
5e153a4f feat(scroll): 设置页新增「QIO 滚动指示条：自动显示 / 始终显示」
8e8689bf test(scroll): 对话/输入链滚动与宽度的行为用例 + 既有用例补断言
3830f38b fix(scroll): 对话与输入链统一滚动策略 + 宽度链（普通对话不得横向滚动）
2d248aee fix(scroll): 上翻时一步可达待办 + 纯裁剪盒子改 clip + 输入区指示条改 hidden
d3d2c1f1 docs(scroll): 补齐两条实测边界（clip 与 auto 的计算组合、记录模式的适用范围）
e8846da0 fix(scroll): 板面滚动区 hidden 策略 + 卡片单层纵向视口 + 预览显式渲染上下文
07a8ee43 fix(scroll): 互动对话面板收敛为一层纵向滚动 + 全程无横向滚动
9879205d docs(scroll): 统一滚动规则写入设计规范、组件说明与互动契约
bcbe26ae fix(scroll): 指示条在滚动容器内随内容滚走（反向平移补偿）+ 下拉浮层不再撑宽
bd6eab5c test(scroll): 共享滚动原语的判定/行为测试 + 表格两态与代码软换行用例
21c62827 feat(scroll): 统一滚动原语（原生滚动条消除 + QIO 浮动指示条 + 表格适配基础）
```

规模：**95 个文件，+6136 / −471**（相对冻结基线）。逐文件说明按区域归并如下。

### 2.1 共享原语与全局规则（Lead）

| 文件 | 改动说明 |
| --- | --- |
| `frontend/src/styles/scroll.css`（新） | 全局消除原生滚动条外观（含按钮/轨道/角落），**不设** `overflow:hidden`、**不禁用**滚轮；`[data-qio-fit]` / `[data-qio-scope~="chat"]` 内容宽度约束；指示条样式；表格两态（表格 / 纵向记录）；`.qio-scroll-area` 与 `.qio-sr-only` |
| `frontend/src/styles/tokens.css` | 新增 `--scroll-*` 令牌（暗/亮两套）：视觉宽度、操作带、内缩、三级对比色、淡出时长、保留时长 |
| `frontend/src/styles/base.css` | 下拉浮层不再用 `min-width:max-content` 撑宽（改 `min-width:0` + `max-width:min(420px, 100vw − 2·sp3)`），选项由 `nowrap` 改为可换行 |
| `frontend/src/composables/useQioScrollbars.ts`（新） | 指示条引擎：auto/hidden 两策略、边缘唤出、按需淡出、可拖动且与卡片拖动/连线/平移隔离、`role="scrollbar"` + `aria-*`、`tabindex="-1"`、ResizeObserver + MutationObserver 合并到一帧一次测量、destroy 全释放；**滚动容器内 absolute 子节点的反向平移补偿**；零尺寸宿主不判可滚 |
| `frontend/src/composables/useQioTables.ts`（新） | 表格两态的 DOM 判定与应用（真实可用宽度 + 列数 + min-content 探针，带滞回），记录模式补显式表格语义 |
| `frontend/src/utils/qioTable.ts`（新） | 纯判定函数与备用列名（空/重复表头用位置身份） |
| `frontend/src/utils/scrollIndicator.ts`（新） | 指示条偏好（auto / always）读写，落 `html[data-scrollbar]` + `localStorage(qio-scrollbar)` |
| `frontend/src/components/ui/QioScrollArea.vue`（新） | 共享滚动容器原语（thin wrapper，布局仍由调用方决定） |
| `frontend/src/components/MarkdownContent.vue` | 表格标记与列身份（`th[id|scope|data-col|data-label]`、`td[headers|data-col|data-label]`、隐藏 caption）、代码块软换行、去掉全部 `overflow-x:auto`、表格身份改为按源码偏移（同一 source → 同一 HTML） |
| `frontend/src/main.ts` | 导入 `styles/scroll.css`；启动时应用指示条偏好 |

### 2.2 对话与输入（wt/gs-chat，9 个源文件）

`.stream` 成为普通对话唯一主要纵向滚动视口（`data-qio-scope="chat"` + `v-qio-scrollbar(auto, y)`，
删掉 `scrollbar-width: thin`）；消息/气泡/附件/工具卡/错误与恢复详情全链 `data-qio-fit` + `min-width:0`；
媒体按可用宽度等比适配；按钮换行重排；纯裁剪盒 `overflow:hidden` → `clip`；
`Composer` autosize 重写（空草稿清高度、边框偏移、量后复位 scrollTop、四个重算触发点），
textarea 走 hidden 策略；`TurnProcess` 不再自建滚动视口（去掉 `max-height:160px` 小窗）；
`ConversationView` 的候选列表接统一策略、长错误原文不再单行截断。

### 2.3 互动对话面板（wt/gs-dock，2 个文件）

把 `.process` 与 `.panel-notices` 从 `.panel-body` 的兄弟节点**搬进** `.stream-inner`：
原来它们各带 `max-height + overflow:auto`，会把同一段阅读流切成三层小滚动窗口；只去掉限高而不搬，
展开后的长详情又会被 `.panel-body` 的 `overflow:hidden` 裁掉。搬进 stream 后消息 + 过程 + 提示 +
失败恢复共用一个纵向视口。附件默认 2 条 + 展开其余；警告「简述 + 展开全文」；
上翻时固定入口显示「回到最新 · 待审批 N 项 / 有错误待查看」（该入口本身在滚动内容之外）；
输入区策略改为 hidden。实测：面板内 `[data-qio-scroll]` 只有 2 个（stream auto / composer hidden）。

### 2.4 板面 / 卡片 / 预览（wt/gs-board，15 个文件）

板面滚动区 hidden（不建轨道、`range=none`，平移/缩放/坐标换算不变）；
`.card-body` 是这张卡唯一主要纵向视口；`.code-body` 不再自己滚（软换行、逐字保真）；
`CardInlinePreview` 新增**必填** `context="material" | "chat"`，用 `data-qio-viewport="content|self|viewer"`
把「纵向视口恰好一个」写死；图片/PDF/表格/代码在 material 下按 `data-qio-pan="both"` 保留二维浏览，
chat 下一律不横向；`ResizeHandles` 的 `@pointerdown.stop` 让尺寸调整与宿主拖动不再同时触发；
新增 `horizontalFit.ts` 作为横向不溢出的**唯一判定口径**（溢出 + 真能滚，两项缺一不可）与复核钩子
（`data-qio-h-fit` / `verifyHorizontalFit()`）。

### 2.5 其它页面与浮层（wt/gs-pages，34 个文件）

设置页新增「QIO 滚动指示条：自动显示 / 始终显示」（默认自动，文案写明不作用于板面与对话横向）；
下拉/浮层/审批/凭据/引导/调试/意图与失败恢复全部接入统一策略；
`DevTaskEntry` 删除自绘 `::-webkit-scrollbar{width:8px}` 一套；
正文滚动与固定操作分离（审批操作行、影响确认、凭据弹窗、待审批卡的 sticky 操作）；
D6 横向边界：`.planet-view` / `.panel-inner` / `.panel-body` 改两轴 `clip`，
`.topic-scroll` / `.detail-scroll` 保留纵向滚动 + 横向锁死 + 内容换行。
**口径更正（2026-10-14，独立复核提出）**：面板**展开 + 选中话题**态实测四处 `over=0 / moved=0`；
面板**收起**态这些元素 `clientWidth=0`、不可见，其 `over/moved` 数字无意义（基线里观察到的
182/144/129 正是这种收起态读数，**不构成"修掉了一个隐藏可滚缺陷"** —— 本材料不再这样表述）。
判定口径因此明确为：零尺寸（折叠/隐藏）元素不参与横向判定。

## 3. 对话规则实现说明与具体例子

### 3.1 代码软换行（复制逐字不变）

`MarkdownContent` 的 `pre`：`white-space: pre-wrap; overflow-wrap: anywhere; overflow-x: clip; tab-size: 4`。
**不改字符序列**：DOM 文本、复制按钮、手动选择复制、下载内容都还是原文。
例：源码里一行 400 字符的 `const veryLongLine = "xxx…"` → 视觉折行；复制出来仍是那一行（无插入换行）。
用例：`gs-qio-chat-scroll-fit.test.ts` 与 `MarkdownContent.test.ts`（长行 + 缩进行逐字比对）。

### 3.2 表格两态（表格 / 纵向记录）

渲染标记（由 MarkdownContent 产出）：

```
<div class="table-wrap qio-table-wrap" data-qio-table="1" data-qio-fit data-mode="table" data-cols="8" data-rows="3">
  <table class="qio-table"><caption class="qio-sr-only">表格，8 列 3 行</caption>
    <thead><tr><th id="qio-t128-c1" scope="col" data-col="1" data-label="编号">编号</th>…</tr></thead>
    <tbody><tr><td data-col="1" data-label="编号" headers="qio-t128-c1">1</td>…</tr></tbody>
  </table>
</div>
```

判定：`每列宽 = 实际可用宽度 / 列数`；每列 < 88px 进记录模式；≥112px 且内容真实最小宽度放得下才退回表格
（滞回带 88–112px，避免临界振荡）。记录模式下 `table/tr/td` 改为块级，「列名 + 值」由 `td[data-label]`
经 `::before` 呈现；表头行只做视觉隐藏（仍在无障碍树里）；补齐 `role` 语义。
**重排只改呈现**：原数据、列顺序、记录顺序、表头文字都不变（复核的内容指纹用例跨 6 档视口逐字比对）。

实测例子（1200×800 与 800×600，正文可用宽度 726px）：
2 列 → 表格；8 列 → 800×600 下自动切记录；22 列 → 记录；引用内表格 → 表格且不超出引用正文边界。
5 张注入表格在两种尺寸下 `tableOverflowPx = 0`、`bodyOverflowPx = 0`。

### 3.3 输入区

`textarea` 软换行（`wrap="soft"`）、随内容增高、到上限（`MAX_INPUT_PX`）后只在自己内部纵向滚；
空/短内容不产生滚动范围、不预留轨道（hidden 策略）；草稿切换、话题切换、窗口 resize、输入行宽度变化
四个触发点都会重算；量高度不碰 `value` / 选区 / 撤销栈。

### 3.4 对话内预览

同一渲染器按上下文分叉：`context="chat"` 不建任何内层滚动容器、无缩放、软换行/按宽适应；
`context="material"` 的查看区允许二维浏览并走统一指示条，但外层卡片/正文仍 `over ≤ 1`。

## 4. 测试、门禁与唯一全量 CI

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 类型检查 | `cd frontend; npx vue-tsc --noEmit` | **0 错误** |
| 前端全量测试（唯一一次） | `npm test` | 365 文件 / 3220 用例 → **1 failed**（`MarkdownStreaming`：表格 id 每次渲染自增，破坏「同一源码同一 HTML」） |
| 失败项定向重跑 | `npx vitest run MarkdownStreaming MarkdownContent acc_c_markdown acc_f_13_markdown_lists gs-qio-chat-scroll-fit useQioTables` | 6 文件 / 55 用例 **全绿**（未重新启动全套） |
| 构建 | `npm run build` | 成功 |
| 文档一致性 | `python scripts/check_docs.py` + `--selftest` | 通过（48 个里程碑条目；selftest 6/6） |
| 后端全量 | `cd backend; uv run --frozen pytest -q` | **exit 0** |
| 独立复核（冻结基线） | 见 `_gs-verify/baseline.md` | 基线 75 通过 / 36 失败 / 3 跳过（对话消息区横向可滚 5459–5881px、60 次表格测量 26 次越界） |
| 独立复核（集成快照） | `_gs-verify/tools/audit.mjs` 等（快照 `c9cb8035`） | 全量审计 **134 条 → 133 通过 / 1 失败 / 0 跳过**（唯一失败是复核工具自身未唤醒指示条，已修）；第二阶段补充 11/11、交互 8/8、指示条聚焦 2/2 |
| 复核发现的真实缺陷 | 用户气泡纯文本不换行（F1） | 已修（`overflow-wrap` 两处）并在浏览器复测 `dx=0`；定向重跑 `gs-qio-chat-scroll-fit` + `scroll.test.ts` 16 用例全绿 |
| 唯一全量 CI | push `fix/global-scroll-dialog-layout` 触发 `CI`（push 到任意分支都会触发；未建 PR） | 结果见「最终状态」 |

**触发规则核对**：`.github/workflows/ci.yml` 是 `on: push: branches: ["**"]` + `pull_request`，
并以 `github.sha` 作 concurrency 去重键。因此**只推一次**是本任务唯一的全量 CI ——
开发期所有提交都停在本地，收尾时一次性推送；本任务未修改任何 CI workflow。

## 5. 真实证据（浏览器 / 交互）

Lead 侧独立实例（前端 5396 / 后端 8746 / 数据目录 `_gs-verify\lead\data`），真实 Chrome（非无头 shell）：

| 证据 | 位置 | 结论 |
| --- | --- | --- |
| Lead 探针 18 条 | `_gs-verify/lead/probe.mjs`、`lead-probe.json` | **18/18 通过**（原始滚动条消除、无轨道预留、对话根 scope、对话内横向可滚 0、横向手势不移、表格边界、代码软换行、指示条按需出现并贴合） |
| 截图（暗/亮 × 1200×800 / 800×600） | `_gs-verify/lead/lead-{1200x800,800x600}-{dark,light}.png` | 视觉对照 |
| 星球页 D6 复测 | `_gs-verify/lead/planet-probe.mjs`、`lead-planet.json`、`lead-planet-expanded.png` | 四处 `over=0 / moved=0` |
| 指示条随内容滚走的原始缺陷与修复 | `_gs-verify/lead/clip-probe.mjs`、`overflow-combo-probe.mjs` | 修复后 `transform = translate(0px, scrollTop)` 贴合 |
| 独立复核（基线） | `_gs-verify/baseline.md`、`evidence/baseline/` | 冻结基线：对话消息区横向可滚 5459–5881px；60 次表格测量 26 次越界（最大 2861px） |
| Edge（WebView2 同源内核）100% / 125% / 150% | `_gs-verify/lead/lead-probe-edge-{100,125,150}.json` | Lead 探针三档均 **18/18 通过** |
| 用户气泡长 token（F1） | `_gs-verify/lead/userbubble-probe.mjs`、`lead-userbubble.json/.png` | 修复后 `dx=0`、`overflow-wrap=anywhere`、折行 7 行 |
| 独立复核（集成版） | `_gs-verify/acceptance.md` + `evidence/`（16 个目录） | 6 条硬条件在可测范围内全部成立；有头真实窗口实测滚动条 gutter=0（原生滚动条不是 overlay 假象） |

### 5.1 复核方法与更正记录（独立复核子智能体）

判定口径（本任务最终采用，写入 `_gs-verify/README.md` 与 `baseline.md`）：

- **内容溢出信号**：`scrollWidth − clientWidth`（≤ 1px 合格）；
- **可滚动信号**：赋值 `scrollLeft = 500` 后是否真的移动 —— **不可只用 wheel deltaX**（基线里会假通过）；
- **零尺寸元素**（`clientWidth/clientHeight ≤ 0`，折叠/隐藏）不参与判定；
- **测量必须钉住提交**：用 `git archive` 导出快照再跑，避免 dev server 读活工作树造成数字漂移；
- **反自欺**：扫不到真实内容（无 table/pre/img）时记 SKIP 而非 PASS。

复核自己修正并公开记录的 9 处口径问题（供后续复用）：iframe 内校准滚动条信号、
`::-webkit-scrollbar` 计算值判定、折叠元素单列、`clip` 不再一律判失败、指示条等帧 + 先唤醒、
多实例 rail 取宿主自己的、互动对话表格与**它自己所属的** markdown 正文比、八向尺寸每向换新卡、
手势用例重载清视图。

**两条被撤回的结论**（本材料不再引用）：

1. 「`overflow-x: clip` 会同时裁掉纵向溢出」—— 探针取样点被相邻元素盖住造成的假象；
   独立复测：`clip` + `visible` 不裁纵向，`overflow-x: hidden` 才会（其 `overflow-y` 计算成 `auto`）。
2. 「星球页 `.panel-inner` 在基线上仍能滚 182px」—— 收起态零尺寸元素的读数，
   不构成真实缺陷（见 §2.5 的口径更正）。

**已补测通过的两项**（复核第二轮，报告 §3.10 / §3.11）：
星球页「面板展开 + 选中话题」态 —— `.planet-view` / `.panel-inner` / `.panel-body` / `.topic-scroll` /
`.detail-scroll` 全部 `dx=0`、赋值 `scrollLeft` 不动、全页可见 offender 0；
独立材料预览的二维查看区 —— 60 列 xlsx 素材下 `[data-qio-pan="both"]` 查看区 `dx=11 / moved=11`（横向确实可滚，
已到上限），外层 `.cip` / `.cip-xlsx` / `.card-body` / `.card` 与页面全为 0。
（强度提醒：该素材的横向溢出只有 11px，属「能滚」而非「大幅横向可用」的强证据。）

**未验证项（复核报告 §6，如实保留）**：卡片最小尺寸下 xlsx 预览的可用性、
真实输入法会话（IME 为 CDP 合成）、真实 Windows 显示缩放设置、打包 Tauri 外壳、
Firefox/WebKit、框选矩形语义与连线创建、泄漏阈值只看趋势、表格无障碍只验证属性未做读屏、
撤销栈边界与跨话题草稿等深层语义（由单测与门禁覆盖）。

## 6. 与另一修复任务的实际重叠（以真实分支 diff 为准）

另一任务：`fix/interactive-attachment-notice-connect-resize`（worktree `qio-fix-int` 等），
观察到的 tip `5fa278ee`（2026-10-14 03:12），其与 origin/main 的 merge-base 同样是
`17f28f7bee410def54ee0cfcc72853cbc99e0507` —— **与本任务同基线**，因此不存在「谁落后谁」的自动消解。

实际相交文件（两个分支各自 diff 的交集，**3 个**）：

| 文件 | 另一任务的改动 | 本任务的改动 | 合并注意 |
| --- | --- | --- | --- |
| `frontend/src/components/interactive/BoardCanvas.vue` | 连接点/尺寸热区统一交互层、拖线命中改认显示矩形（+208 行量级） | `.board-viewport` 接 hidden 策略 + `overscroll-behavior: contain` | 二者改的是不同区域（指针命中 vs 滚动策略），但都在同一文件；逐块审阅指针事件与滚动容器初始化顺序 |
| `frontend/src/components/interactive/BoardCard.vue` | 附件提示生命周期（+181 行量级） | `.card-body` 单层纵向视口、`.code-body` 软换行、编辑态最小高度、预览上下文 | 逐块审阅卡片正文/提示区的 DOM 结构与 CSS 归属；提示若依赖 `.card-body` 的限高需要复验 |
| `frontend/src/components/interactive/AddMenu.vue` | 添加菜单交互（+252 行量级） | 接入统一滚动策略 + `data-qio-fit` | 菜单列表的滚动容器与指示条挂载点需复验 |

不相交但相关：另一任务新增 `frontend/src/composables/useTransientNotice.ts` 与三个测试文件，
本任务未触碰；本任务新增的 10 个文件（`styles/scroll.css`、`composables/useQioScrollbars.ts`、
`composables/useQioTables.ts`、`utils/qioTable.ts`、`utils/scrollIndicator.ts`、
`components/ui/QioScrollArea.vue`、`components/interactive/horizontalFit.ts` 与四个测试文件）
另一任务未触碰。

**共享样式层**：本任务改了 `styles/tokens.css`、`styles/base.css`（下拉浮层宽度）与
`styles/interactive-shell.css`（仅文件末尾新增跨组件滚动声明，几何未动）；
另一任务若也改这三个文件，冲突面在 `base.css` 的 `.qio-select-menu` 与
`interactive-shell.css` 的尾部追加块 —— 都可能需要人工合并。

## 7. 后续合并建议

1. **顺序**：先合另一任务（它在做用户可见缺陷的热修），再合本任务；
   或先合本任务再让另一任务 rebase —— 无论哪种，**必须重跑**当前 CI（基线已变）。
2. **逐块审阅位置**（三处相交文件 + 三处共享样式）：
   - `BoardCanvas.vue`：指针命中层（另一任务）与 `.board-viewport` 的滚动容器/策略（本任务）；
   - `BoardCard.vue`：附件提示的挂载点与 `.card-body` 的滚动层边界、`.code-body` 软换行；
   - `AddMenu.vue`：菜单列表的滚动容器与指示条宿主；
   - `styles/base.css` 的 `.qio-select-menu`、`styles/interactive-shell.css` 的尾部追加块。
3. **合并后必须定向验证**（建议最小集）：
   - 连接点/连线创建/拖线命中与卡片拖动、尺寸调整互不串扰（本任务改过 `ResizeHandles` 的指针隔离）；
   - 附件提示在新滚动层下不遮挡、可关闭，且不被 `.card-body` 裁切；
   - 板面平移/缩放/坐标换算与 `canScroll/insideScrollable` 判定不变；
   - 对话内横向可滚仍为 0、表格仍在正文边界内（跑 Lead 探针 `probe.mjs` 与复核 91+ 断言）；
   - 两条线各自新增的测试文件全部通过。
4. **不要**用本任务的「已完成」当作合并后的验收：本任务只证明在冻结基线 `17f28f7b` 上成立。
   main 在本任务验证之后仍可能前进，真正合并时需重新核对差异并重跑门禁。

## 8. 已知限制与未完成验证

1. ~~无头浏览器下的滚动条占位~~ → 已由复核在**有头真实窗口**（Edge 移到屏外）实测：
   `::-webkit-scrollbar` 计算值 0px/none、`documentElement` 与所有可滚动容器的 gutter 均为 0 ——
   原生滚动条确实消失，不是 overlay 假象。
2. **125% / 150% 显示缩放**：已用浏览器级缩放（`--force-device-scale-factor`，dpr 1.25/1.5）
   与 Edge 的 DPR 1.25/1.5 复测通过；**仍未在真实 Windows「显示设置 → 缩放」下复验**。
3. **Tauri / WebView2 实机**：未在打包后的桌面窗口里验收（自绘标题栏、真实 PointerEvent、
   输入法组合、触控板横向手势仍属待验证）。已用 Edge（WebView2 同源内核）作为引擎级近似。
4. **对话内预览**：`context="chat"` 的调用点在当前实现里不存在（只有单测覆盖），
   若以后要在对话里内嵌同一渲染器必须显式传 `context="chat"`（TS 必填）。
5. **板面最小尺寸下的预览**：复核观察到卡片缩到最小时 xlsx 预览容器高度变为 0、网格不再渲染
   （验收没要求，但记录在案）。
6. **markdown 指令扩展**会吃掉标题里「词:标识符」的冒号后半段（复核在基线发现，**不属于本任务范围**，未修）。
7. **CI 结果**：见「最终状态」；若 CI 有负载/基础设施失败，会记录原 run/job 并只定向重跑该项。

## 9. 最终状态

（收尾时写入：最终 HEAD_SHA、工作树状态、唯一全量 CI 的 run id 与结论。）

**当前声明：独立分支开发完成，合并材料已准备，未开始合并。**
