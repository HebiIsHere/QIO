# 互动板浮层修复与独立验收报告（子智能体 D）

> 工作区：`D:\qio-dev\qio-ui-d`（分支 `wt/ui-d-overlay`，基线 `12266d9`，本轮提交 **`def1210`**）
>
> **注意**：下面是**开发阶段**在 D 自己的分支上的一次完整实跑；集成后我会用主智能体给的提交号 / 工作区
> 再跑同一套探针复验（届时只报告、不改产品代码）。
> 范围：`interactive/overlayLayout.ts`、`IntentBatchTray.vue`、`IntentStatusPopover.vue`、`ImpactConfirmDialog.vue`、
> `styles/interactive-shell.css`、`__tests__/overlayLayout.test.ts`、`__tests__/batchTray.test.ts`（既有组件用例，按用户规则补反例）、
> `scripts/interactive-verify/fe-verify-d.mjs`、本文件与 `docs/interactive-ui-screenshots/`。
>
> **本报告区分三种证据**：【亲测】= 我自己起应用/跑命令得到的真实输出；【读码】= 只读源码推断；
> 【未验】= 这一轮没能验证。不把推断写成实测，也不粉饰失败。

---

## 0. 结论摘要

| 项 | 目标 | 实测 | 结论 |
| --- | --- | --- | --- |
| 聊天面板 × 批量列表相交面积 | 0 px² | **0 px²**（1440×900 / 1024×768 / 800×600 × 明暗两主题，共 6 格） | 达标【亲测】 |
| 两面板最小间距 | ≥ 8px | 恒定 **12px**（= OVERLAY_GAP） | 达标【亲测】 |
| 浮层压住底部工具栏 | 0 px² | **0 px²**（6 格全部） | 达标【亲测】 |
| 聊天输入 / 发送 / 板面提交入口可点 | elementFromPoint 命中自身 | 6 格全部命中自身 | 达标【亲测】 |
| 800×600 工具栏总高 | ≤ 约 96px | **285px** | **未达标（A 的 BoardToolbar，见问题 1）**【亲测】 |
| 800×600 聊天消息可读区高度 | > 0 | **0px**（被 285px 工具栏挤没；根因同上） | **未达标（根因在工具栏）**【亲测】 |
| 连接点不被局部工具栏 / 菜单盖住 | 3 次命中自身 | 1440×900：4/4 命中；800×600：**1 个点被 `add-menu` 挡住（3/3 次）** | **未达标（A 的 AddMenu/卡片工具栏，见问题 2）**【亲测】 |
| §9.3 处理掉一项后批量入口保留 | 保留并显示剩余/已处理 | 页面状态：入口在、列表在、"剩余待处理 3 项 · 已处理 1 项" | 达标【亲测】 |
| §8.3 聊天发送不调用板面提交接口 | 0 次 submissions | 发送期间 `POST /api/turns` 2 次、`/submissions` **0 次** | 达标【亲测】 |
| 单测 | 全绿 | `227 passed`（含我新增的 overlayLayout 14 例 + 批量新规则 6 例） | 达标【亲测】 |
| `vue-tsc --noEmit` | 无输出 | 无输出（exit 0） | 达标【亲测】 |

---

## 1. 怎么跑（可复核）

前后端都在**我自己的端口**（8914 / 5414）上，未占用 8791/5299：

```powershell
# 后端（用主检出的 venv，源码走我的工作区）
$env:QIO_DEV_INSECURE='1'; $env:QIO_DATA_DIR="$env:TEMP\qio-ui-d"
$env:PYTHONPATH='D:\qio-dev\qio-ui-d\backend\src'
& 'D:\qio-dev\qio-ui\backend\.venv\Scripts\python.exe' -m uvicorn agent.main:create_app --factory --host 127.0.0.1 --port 8914
# 真实输出：INFO: Application startup complete. / INFO: Uvicorn running on http://127.0.0.1:8914

# 前端
$env:VITE_QIO_BACKEND_URL='http://127.0.0.1:8914'
node frontend/node_modules/vite/bin/vite.js --config frontend/vite.e2e.config.ts --port 5414 --host 127.0.0.1
# 真实输出：VITE v6.4.3  ready in 740 ms / ➜  Local: http://127.0.0.1:5414/
```

探针（Edge 驱动，独立 profile + 端口 9667，未用 Chrome headless，避免上一轮的大量实例卡死）：

```powershell
node scripts/interactive-verify/fe-verify-d.mjs --app http://127.0.0.1:5414
```

探针自己完成：清批次记录 → 点击演示入口生成「同一批 4 项」→ 三档窗口 × 两主题打开聊天与批量列表 → 截图 + 量测 →
边界检查（读取选择器、点击、真实鼠标事件）。**完整 JSON 落在 `docs/interactive-ui-screenshots/d-report.json`**，
下面的每个数字都能从那里复核。

---

## 2. 浮层几何（契约 §9.6）【亲测】

### 2.1 实测表格（`d-report.json` → `cells`）

| 视口 | 主题 | 模式 | 相交面积 | 面板间距 | 工具栏高 | 消息区高 | 输入/发送/提交命中 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1440×900 | 暗 | side-by-side | 0px² | 12px | 172px | 312px | ✅ / ✅ / ✅ |
| 1440×900 | 亮 | side-by-side | 0px² | 12px | 172px | 312px | ✅ / ✅ / ✅ |
| 1024×768 | 暗 | side-by-side | 0px² | 12px | 236px | 171px | ✅ / ✅ / ✅ |
| 1024×768 | 亮 | side-by-side | 0px² | 12px | 236px | 171px | ✅ / ✅ / ✅ |
| 800×600 | 暗 | side-by-side | 0px² | 12px | 285px | 0px | ✅ / ✅ / ✅ |
| 800×600 | 亮 | side-by-side | 0px² | 12px | 285px | 0px | ✅ / ✅ / ✅ |

上一轮的 45080px²（我这次在同一台机器、同一档窗口复现旧行为时得到的是 **约 10 万 px² 量级**，
见单测里的反例用例）在本轮几何下归零。为什么是 0：并排时批量列表整体被推到聊天面板左边
（`batchRight = 16 + 聊天宽 + 12`），两块的最坏矩形左右不相交；上下模式则把「两块高度上限 + 间距」
压在可用高度之内，最坏情况刚好相切。

### 2.2 我做了什么

1. `planOverlayGeometry` 按**实际可用区域**算：视口、舞台上下沿、**量到的底部工具栏顶边**、两面板最小尺寸；
   先并排、再上下、最后二选一（`side-by-side | stacked | switched`）。
2. `IntentBatchTray.vue` 兼作**运行时施加者**：量舞台/工具栏/聊天入口按钮，把结果写成
   `--im-geo-*` 变量挂在**舞台元素**上；`styles/interactive-shell.css` 里聊天面板读这些变量限制上限
   （聊天组件不由我维护，所以只通过样式表消费，不改它的源码）。
3. **避免循环**：只观察舞台与底部工具栏（这两者的尺寸都不由浮层决定），写入前比较几何串（相同就直接返回），
   面板自身尺寸永远是被写的一方，不进观察链路；几何计算里所有数字取整，避免 0.5px 抖动反复重排。
4. 二选一（switched）：批量列表顶部出现明确的切换行「看对话 / 看审批列表」并说明原因，
   只展开一个，消息、草稿与勾选都保留。实测本次三档窗口都还放得下，所以都走了并排。

### 2.3 给 C 的接口（我没有改 `ChatDock.vue`）

C 的面板现在**不需要额外改动**就能吃到避让，因为上限是通过 CSS 变量施加的。如果 C 想自己读数字：

| 变量（写在板面舞台元素 `.im-stage` 上） | 含义 |
| --- | --- |
| `--im-geo-chat-max-w` | 聊天面板宽度上限（px） |
| `--im-geo-chat-max-h` | 聊天面板高度上限（px，已扣掉聊天入口按钮占的那一行） |
| `--im-geo-batch-max-w` / `--im-geo-batch-max-h` | 批量列表面板的宽 / 高上限 |
| `--im-geo-batch-right` | 批量列表右边距（并排时 = 左边距 + 聊天宽 + 间距） |
| `--im-geo-gap` | 两面板之间的间距（12px） |
| 属性 `im-geo-mode` | 当前模式：side-by-side / stacked / switched |

约束（需要 C 知道，否则实测会变）：聊天面板的**底边必须停在工具栏顶边上方 78px**
（容器底边 = 工具栏顶边 − 24px，再让出入口按钮约 34px 与 8px 间距）。
现在的 `ChatDock.vue` 正好满足（它自己的 `--chat-dock-clearance` 就是这么算的）；
如果以后改成别的锚点，几何里的 `OVERLAY_CHAT_FOOTER` 要一起改，否则上下模式会算错高度。

### 2.4 单测（纯几何，可离线跑）

```
npx vitest run src/interactive/__tests__/overlayLayout.test.ts
✓ 14 tests
```

覆盖：宽窗口并排（1440/800）、窄窗口切换（480×600 → switched）、上下模式（600×900）、
最小尺寸不被压破、单开不为另一面板预留宽度、视口为 0 / NaN / 工具栏越界等退化输入、
**320–2000 宽 × 480–1200 高 × 5 档工具栏位置共 850 个组合的相交面积恒为 0**，
以及一个反例：同尺寸下旧算法（窗口高度减固定值、两块都贴右）相交面积远大于 0。

---

## 3. 提示词第四节规则 1–8 逐条

| # | 规则 | 我的结论 | 证据 |
| --- | --- | --- | --- |
| 1 | 默认命名全路径是「默认组名」 | 【读码】本工作区 `interactive/board.ts` 仍是旧实现（`组 N`）；A/B 的分支负责，集成后复验 | 只读了源码，没在本工作区改它 |
| 2 | 不同批不因「同一秒创建」被误合 | 【读码】本工作区 `approval.ts:636` 仍在用「创建秒」做第三级归批（`SECOND_BATCH_PREFIX`）；B 负责删除 | 引用方案文档 §2 的既有实测（两批各两项被并成四项） |
| 3 | 同批达到四项后列表保留到处理完 | 【亲测】入口与列表保留，显示「剩余待处理 3 项 · 已处理 1 项」；已处理行灰掉、无勾选框、不可提交；全部处理完后入口消失 | 探针页面状态 + 6 条新单测（含旧规则反例） |
| 4 | 聊天草稿跨刷新 / 跨话题恢复 | 【未验】C 的文件；本工作区仍是内存草稿 | 建议集成后用 UI 亲测 |
| 5 | 草稿保存失败与旧回执不覆盖新文字 | 【未验】同上（C） | — |
| 6 | 卡片草稿保存失败可重试 | 【未验】同上（C） | — |
| 7 | 未勾选注释与删除路径不进提交载荷 | 【读码 + 界面观察】服务端 `selectable_cards` 是唯一实现；界面上「本次允许 QIO 查看的范围是空的（未勾选与明确隐藏的注释不在其中）」实测可见 | 未做真实提交载荷级验证（本轮板面无有效改动，提交是 empty） |
| 8 | 聊天发送与板面提交互相独立 | 【亲测】发送期间网络请求：`POST /api/turns` 2 次，`/submissions` **0 次**；失败时如实显示「还没有配置可用的模型凭据…」 | `d-report.json → chatIndependence` |

### 3.1 规则 3 的细节（我自己复现）

- 页面实测（`d-report.json → batchRetention`）：`entry=true, list=true, remaining="剩余待处理 3 项 · 已处理 1 项",
  rows=4, pendingRows=3, processedRows=1`；已处理那行 `data-im-state="processed"`、没有 `input[type=checkbox]`。
  这一批总共 4 项、已经处理掉 1 项，而入口与列表都还在 —— 这正是 §9.3 要的行为
  （旧判据"剩余待审批 ≥4"在这一刻会直接把入口藏掉）。
- 探针还做了一次真实鼠标点击（`Input.dispatchMouseEvent`）勾选第 1 个**可批准**项：
  `topAtPoint=batch-item/input`、`checkedAfterMouse=[true,false,false]`、批准按钮由禁用变可用
  （`approveBefore=false`）。点击"批量批准"后服务端按冲突规则拒绝了这次批准，界面如实显示：
  「批量批准：批准 0 项，拒绝 0 项；未选中的继续等待。未成功 1 项（与已批准且尚未撤回的
  「［演示］把材料归为一组并给出对比摘要」互不相容：不能同时批准。）」
  —— 冲突判定在服务端、前端只显示结果（契约 §1.6），这一条同时被验证到了。
- 因为这份演示数据里可批准项互不相容，**本轮没有在页面上观察到"待处理 3 → 2"的转移**；
  转移过程由单测确定性地覆盖（见下），页面侧只证明了"保留 + 数量显示"这一半。
- 单测（`batchTray.test.ts` 新增 6 例）：4 项里 1 项已完成 → 入口在、文案对；全选后批量批准只提交 3 个待处理 id
  （`i3` 不在调用参数里）；4 项全完成后入口与列表一起消失；阅读位置在内容更新后保持 220px 不动；
  以及**反例**：同一份数据下旧判据 `pendingIds.length >= 4` 为 false（列表会提前消失），新判据为 true。

> 说明（需要 Lead/B 确认的一点）：本轮 §9.3 把资格从「剩余待审批数」改成「该批总量」，而 `approval.ts` 归 B。
> 为了让我的组件在 B 的改动落地前后都符合用户规则，组件里的资格条件是**用 B 导出的常量 `BATCH_LIST_MIN`
> 与 `groupIntentsByBatch` 自己组合**的（`intentIds.length >= BATCH_LIST_MIN && pendingIds.length > 0`）。
> 集成后如果 `batchesWithList` 已按同一规则落地，两者结果完全一致（用例里两条路径都断言过，不一致会打 warning）。

---

## 4. 审美检查项

| 检查项 | 结论 | 依据 |
| --- | --- | --- |
| 文字层级（标题衬线 / 正文无衬线 / 数字等宽） | 达标 | 面板标题用 `--serif`、正文 `--sans`、数量与进度 `--mono`（`.mono`）；截图可见 |
| 元信息字号不被当正文 | 已修 | 影响确认框的说明与后果文字从 `--fs-xs`(11px) 提到 `--fs-base`/`--fs-sm`，元信息保持 `--fs-xs` |
| 颜色 / 阴影 / 遮罩走令牌 | 已修 | 确认框原来的 `rgba(0,0,0,.42)` 遮罩与 `rgba(0,0,0,.45)` 备用阴影改走 `.qio-confirm-scrim`（`--bg-overlay`）与 `--elev-overlay`；面板阴影走 `--elev-floating` |
| 对齐与留白 | 达标 | 两面板右列对齐、间距恒为 12px（实测），条目标题/状态/操作在同一行基线 |
| 主题对比 | 达标（目视） | 明暗两套 6 张截图逐张看过：面板底色与文字对比正常，未出现"白底白字" |
| 动效 | 遵守令牌 | 新样式只用了 `--dur-*` / `--ease-*`（无裸时长/裸曲线/裸位移）；reduce-motion 由 base.css 统一降级 |
| 岗位用语 | 达标 | 我写的界面文案里没有 adapter / 接口 / 求差 / 状态机 / DOM 等开发用语 |
| 局部工具栏不遮标题/连接点 | **未达标** | 见问题 2 |

截图（6 张浮层共存 + 批量处理后 + 聊天发送 + 连接点，共 9 张）在 `docs/interactive-ui-screenshots/`：
`d-1440x900-dark.png`、`d-1440x900-light.png`、`d-1024x768-dark.png`、`d-1024x768-light.png`、
`d-800x600-dark.png`、`d-800x600-light.png`、`d-batch-after-partial.png`、`d-chat-send-independent.png`、
`d-800x600-connect-point.png` / `d-1440x900-connect-point.png`。

---

## 5. 问题清单（阻断 / 重要 / 次要）

### 阻断 1 · 800×600 底部工具栏高 285px（要求 ≤ 约 96px）
- **现象**：800×600 下工具栏 rect 高 285px（1440×900 也有 172px），占了近半个窗口。
- **后果（连锁）**：聊天可用高度只剩 160px，**消息可读区被挤成 0px**（实测）；README 要求的
  "工具栏最多两行"也没满足。
- **归属**：`components/interactive/BoardToolbar.vue`（A），不在我的写作用域。
- **复现**：`node scripts/interactive-verify/fe-verify-d.mjs --app http://127.0.0.1:5414`，
  看摘要里 `800×600 … 工具栏高=285px`；或打开 800×600 窗口量 `[data-im="board-toolbar"]`。
- **我的部分**：几何已经按量到的工具栏顶边倒推（不是"窗口高度减固定值"），工具栏一收窄，消息区自动恢复正常；
  在工具栏收窄前，800×600 不可能同时做到"两面板不相交 + 消息可读"。

### 重要 2 · 800×600 有一个连接点被 `add-menu` 盖住
- **现象**：选中卡片后，4 个连接点里有 1 个的中心点 `elementFromPoint` 命中 `add-menu`，**连续 3 次一致**；
  另有一次观测（早期一轮）命中的是 `card-toolbar`。
- **后果**：那个角上拖不出连线（点不到），属于 §9.6"卡片旁工具栏不遮标题、连接点或输入内容"。
- **归属**：`AddMenu.vue` / `BoardCard.vue` 的局部工具栏（A）。
- **复现**：探针最后一段（会自动新建一张卡片并选中）；或手动：800×600 → 添加一张文字卡 → 选中 →
  在卡片左上角连接点位置 `document.elementFromPoint(x,y)`。

### 重要 3 · 窄窗口里批量入口胶囊文本溢出（我在本轮修掉了）
- **现象（修前）**：800×600 同时打开聊天时，批量列表被推到左边（宽 336px），入口胶囊文案过长会换行并把文字挤出胶囊。
- **处理**：胶囊改为单行 + 省略号（`text-overflow: ellipsis`）、完整文案放进 `title`；**实测修后不再溢出**。

### 次要 4 · 聊天面板在 800×600 会只剩"标题 + 输入区"
- 与阻断 1 同源；工具栏收窄后应恢复。若希望更稳，可让聊天面板在极矮空间里隐藏"能力边界"说明
  （`[data-im="chat-scope"]`），但那是 C 的内容、也需要用户同意，我没有动。

### 观察 5 · 服务端冲突判定如实显示（不是缺陷，记录为通过项）
- 实测：批量批准被服务端以"互不相容"拒绝时，界面显示具体原因与"未选中的继续等待"，没有假装成功。

### 次要 6 · 探针噪音
- 控制台有一条 `Could not establish connection. Receiving end does not exist.`（开发服务器/扩展噪音，
  探针已归类为 `noise`，不计入问题）；产品错误 0 条，HTTP 4xx/5xx 0 条。

---

## 6. 没能验证 / 需要别人配合

1. **规则 1、2**（默认组名、删除按创建秒归批）：本工作区没有这两个改动，只在别的分支；集成后必须实测。
2. **规则 4、5、6**（草稿持久化与失败重试）：C 的文件，本工作区是旧实现。
3. **规则 7**：没有做真实提交载荷级验证（本轮板面只有 preview 卡片，提交会是 empty）。
4. **聊天 × 批量 × 任务浮层三者同时打开**：任务浮层（我的 `IntentStatusPopover`）已经改成
   "在候选位置里挑相交面积最小的"，但本轮只单测了几何函数，没有做三浮层同开的实机量测。
5. **真实鼠标拖拽连接线 / 平移缩放后浮层定位**：本轮只做了 elementFromPoint 命中与 resize 后复量，
   没有做拖动过程中的连续采样。集成后补。
6. `ChatDock.vue` / `InteractiveView.vue` 我没有改（不是我的文件）；若 C/Lead 需要我上面的接口生效，
   不需要他们改代码，CSS 变量已经接好，但**如果聊天面板的锚点改了**，几何常数要一起改（见 §2.3）。
7. 端口：我只用了 8914 / 5414（探针 CDP 9667、独立 Edge profile），没有占用 8791 / 5299。

---

## 7. 本轮我改了哪些文件

| 文件 | 改动 |
| --- | --- |
| `frontend/src/interactive/overlayLayout.ts` | 实现 `planOverlayGeometry`（冻结签名不变）+ `overlayRects` / `intersectionArea` / `chatHeightRelaxation` 与常量 |
| `frontend/src/interactive/__tests__/overlayLayout.test.ts` | 新增 14 例纯几何用例（含 850 组合扫描与旧算法反例） |
| `frontend/src/components/interactive/IntentBatchTray.vue` | 兼作几何控制器（写 `--im-geo-*`、避免循环）；§9.3 呈现（剩余/已处理、已处理项不可选、阅读位置保留）；键盘（空格不外泄、Escape 先关、滚轮滚自身）；`role=dialog` + 焦点进出；二选一切换行；入口胶囊单行省略 |
| `frontend/src/components/interactive/IntentStatusPopover.vue` | 候选位置避让（避开聊天/批量/工具栏）、键盘与滚轮边界、收起后焦点回到入口 |
| `frontend/src/components/interactive/ImpactConfirmDialog.vue` | 遮罩/阴影走令牌与原语、说明文字提到正文字号、按钮改用 `.qio-btn` 原语（高度放开避免裁字）、Escape/Tab/焦点回位保持 |
| `frontend/src/styles/interactive-shell.css` | 聊天面板的上限来自 `--im-geo-*`（放在文件末尾压过组件媒体查询）；层级与避让注释更新 |
| `frontend/src/interactive/__tests__/batchTray.test.ts` | 补 §9.3 的 6 条用例（含旧规则反例）；**这是既有组件用例，用户规则要求为错误规则背书的测试必须改** |
| `scripts/interactive-verify/fe-verify-d.mjs` | 新增：Edge/CDP 探针（三档 × 两主题、相交面积、命中测试、连接点、规则实测、JSON 落盘） |
| `docs/interactive-ui-screenshots/*.png`、`d-report.json` | 实机截图与完整量测证据（提交进仓库） |
| `docs/interactive-ui-verify.md` | 本报告 |

**没有碰**：`views/InteractiveView.vue`、`interactive/types.ts`、`services/interactive.ts`、
`scripts/interactive-verify/fe-scenarios.mjs`、`ChatDock.vue`、`approval.ts`、`board.ts`、其它工作区的文件。
