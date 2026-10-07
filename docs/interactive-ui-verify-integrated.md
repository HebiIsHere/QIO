# 互动板修复与前端审美优化 · 集成后独立复核（子智能体 E）

> 本文件由**独立复核者**在集成提交上亲自读源码 + 起应用操作后写成，不采信开发方报告。
> 结论、证据与问题清单都在本文件里；所有数字都能用文中的命令重跑。

- 复核对象：`D:\qio-dev\qio-ui`，分支 `fix/interactive-ui-refinement`，集成提交 **`40483ae`**（A/B/C/D 四路已合入）。
- 集成实例：后端 `http://127.0.0.1:8931`、前端 `http://127.0.0.1:5431`（数据目录 `%TEMP%\qio-ui-integration`，已过引导层）。
- 复核时间：2026-10-07 18:07–18:40（Asia/Shanghai）。
- 本次只写两个文件：本报告 + `scripts/interactive-verify/fe-verify-e.mjs`（我自己的探针脚本）。
  **没有改任何产品代码、没有改别人的报告、没有改集成分支上别人的文件。** 发现的问题只报告。

## 0. 结论摘要

| 检查块 | 结果 | 说明 |
| --- | --- | --- |
| A 规则 1–8 | **8/8 通过** | 全部实机复现（模拟项单独标注），见 §2 |
| B 布局 | **通过** | 3 尺寸 × 2 主题：工具栏 60 / 84 / 84 px、行数 1 / 2 / 2；两面板相交面积全部 **0**（改前基线 127200 / 133600 / 94864 px²） |
| B 审美 | **通过（1 项次要）** | 层级、命中、连接点、动效、开发用语逐项给依据，见 §3.4–§3.5 |
| C 反例 | **3/3 挡住旧实现** | 「默认组名」「按创建秒归批」「按剩余待审批数判资格」各一条，见 §4.1 |
| C 回归 | **全绿** | 后端互动板 158 passed；前端 vue-tsc 0 错误；vitest 510 passed，见 §4.2 |
| C 已知现象 | **5/5 通过** | 健康探测用例复跑 5 次全过，负载 19–100%，见 §4.3 |

**问题清单：阻断 0 · 重要 1 · 次要 4**（§5）。**没能验证 8 项**（§6）。

---

## 1. 怎么复核（可复现）

### 1.1 先读源码与契约

- 契约：`docs/interactive-mode-contract.md` §8、**§9（用户规则，优先于 §8 与既有测试）**、已作废的 §8.5。
- 前端：`frontend/src/interactive/{approval,drafts,chat,submission,board,overlayLayout,viewport}.ts`、
  `frontend/src/stores/{interactive,session}.ts`、
  `frontend/src/components/interactive/{BoardToolbar,BoardCanvas,BoardCard,BoardGroupFrame,ChatDock,SubmitCluster,IntentBatchTray,CardDraftHint,DemoIntentEntry}.vue`、
  `frontend/src/views/InteractiveView.vue`、`frontend/src/styles/{tokens,base,interactive-shell}.css`。
- 后端：`backend/src/agent/interactive/{models,board,submission,intents}.py`。
- 既有用例只用来确认覆盖范围，不用来当结论。

### 1.2 我的探针

```powershell
# 仓库根目录
node scripts/interactive-verify/fe-verify-e.mjs <场景>          # 场景可逗号分隔，或 all
# 场景：layout connect aesthetics group batch chat chat-fail card-draft pure visibility click-merge restore cleanup
```

- 脚本自己生成 `visual_probe` 的步骤 JSON（`%TEMP%\qio-e-verify\steps-<场景>.json`）、自己起 Chrome
  （CDP **9351**、profile `%TEMP%\qio-e-verify\chrome-profile`，与别人的 9333 隔离），
  原始结果落 `%TEMP%\qio-e-verify\raw-<场景>.json`；截图直接落 `docs/interactive-ui-screenshots/e-*.png`。
- 每条结论都写了「我做了什么 + 实际输出 + 结论」，并标注证据等级：**【亲自复现】** / **【只读代码推断】** / **【没能验证】**。
- **模拟声明**：本机没有模型凭据，真实 QIO 回复无法验证。凡是「发送失败 / 草稿写入失败 / 提交失败 / 存储写满 / 迟到回执」这类，
  都是用**页面级 `fetch` / `Storage.prototype.setItem` 拦截**造出来的，报告里一律写明「模拟」。
  真实跑过的链路：聊天发送 `POST /api/turns`、板面提交 `POST /api/interactive/boards/board_default/submissions`、草稿 `PUT /drafts`。
- 探针限制：每次调用新建浏览器实例，**localStorage 跨调用不保留**（同一次运行内 `reload` 正常）。

### 1.3 集成实例可用性（复核开始时实测）

```powershell
Invoke-WebRequest http://127.0.0.1:5431/ -UseBasicParsing            # 200，len=802
Invoke-WebRequest http://127.0.0.1:8931/api/health -UseBasicParsing  # 200 {"status":"ok","db":true}
Invoke-RestMethod http://127.0.0.1:8931/api/interactive/boards/board_default/state   # 200
```

---

## 2. A · 规则 1–8（提示词第四节）

### A1 两张卡片真实重叠成组；默认组名「默认组名」；改名 / 留空 / 取消 / 刷新 / 撤销重做；历史「组 N」不被批量覆盖 —— **通过【亲自复现】**

命令：`node scripts/interactive-verify/fe-verify-e.mjs group`（原始结果 `raw-group.json`）

我做了什么：进入 `#/interactive` → 用**真实指针**把未分组的 `s_long` 拖到 `s_note_b` 中心后松手 → 读 DOM 与后端状态。

| 步骤 | 实测 |
| --- | --- |
| 拖动前 | `groups[1] s_group=默认组名(default=true, 成员 2)` |
| 松手后 | `groups[2]`：新增 `g_muxynu9r1sqp2 = 默认组名(default=true, 成员 2)`，`s_group` 不变 |
| 组名留空（`change`） | 输入框回到「默认组名」，该组仍 `默认组名(default=true)`，组仍成立 |
| 输入自定义名 | `E-复核-自定义组名(default=false)`，输入即用 |
| 撤销 | 回到 `默认组名(default=true)`；重做 → 回到 `E-复核-自定义组名` |
| 刷新后 | 两个组名都保持 |
| 历史形态 | 组名改成「组 7」→ 保存 → **刷新后仍是「组 7」**（`defaultName=true`），没有被批量改写 |
| 后端对照 | `GET /api/interactive/boards/board_default/state` 的 name/defaultName 与界面一致 |

结论：默认组名是**「默认组名」**；输入即用、留空/取消不动、刷新与撤销/重做一致；**组身份按 id**（两个组同名并存）；历史「组 N」被识别但不被覆盖。
【只读代码推断】`board.ts:283-290`（`normalizeGroup` 按存储值保留 `defaultName`）、`board.ts:590-601`（`renameGroup` 空名直接返回）、
`backend/board.py:242-244 / 515-532` 同规则。
【没能验证】实例里没有真实历史「组 N」数据（只读扫过 `%TEMP%\qio-ui-integration\app.db` 的 state / snapshot / submission 载荷）；
由后端用例 `test_interactive_board.py:846` 与我的纯函数检查覆盖。

### A2 同一秒创建的两批各两项，清掉本地记录后不能变成四项一批；localStorage 禁用 / 损坏 / 写满也不混批 —— **通过（含模拟）**

命令：`node scripts/interactive-verify/fe-verify-e.mjs pure` + `... batch`
（纯函数是**在真实页面里** `import('/src/interactive/approval.ts')` 跑的，不是另写副本）

```
sameSecond_allSameSecond  = true                       # 4 个意图 createdAt 截断到秒完全相同
sameSecond_batchCount     = 4                          # 旧实现（按秒归批）会得到 1
sameSecond_listBatches    = 0                          # 旧实现会给出「同一批 4 项」
sameSecond_keys           = ["intent:a1","intent:a2","intent:b1","intent:b2"]
sameSecond_hasRecoverable = [false,false,false,false]  # 如实说「批次不可恢复」
storageFull_batchCount    = 4     # 模拟 Storage.prototype.setItem 抛 QuotaExceededError
storageFull_listBatches   = 0
corrupt_batchCount        = 4     # localStorage 里塞 '{这不是 JSON'
partialCorrupt_batchCount = 2     # {id:42} 这类坏记录被忽略
```

实机（`batch` 场景）：`演示` 生成 4 项 → 入口出现；随后 `localStorage.removeItem('qio.interactive.intentBatches')` + 刷新 →
`entryPresent=false`（服务端仍有 4 项 pending）——**没有可证明来源时宁可不出现列表，绝不把不同批次相加**。
**模拟标注**：写满 / 损坏是我在页面里造出来的，不是真实磁盘配额耗尽。

### A3 同批四项：处理一项后入口仍在并显示剩余数，剩 3/2/1 都在，处理完才消失；刷新后资格保持；另一批三项不会凑成四项 —— **通过【亲自复现】**

命令：`node scripts/interactive-verify/fe-verify-e.mjs batch`（走真实批量接口 `batch-reject`）

| 时刻 | 入口文案 | 列表 |
| --- | --- | --- |
| 生成后 | `这一批待审批 4 项（共 4 项）` / `剩余待处理 4 项 · 已处理 0 项` | 4 项，全 `data-im-state=pending` |
| 处理掉 1 项 | `这一批待审批 3 项（共 4 项，已处理 1 项）` / `剩余待处理 3 项 · 已处理 1 项` | **仍是 4 项**，被处理项 `state=processed` |
| 刷新后 | 同上（资格与剩余数保持） | 4 项 |
| 全部处理完 | **入口消失**（`entryPresent=false`） | — |

纯函数侧（`pure`）：`qualify_pending4/3/1` 各 1 批（total=4，pending=4/3/1），`qualify_pending0=[]`；
`qualify_entryText3="这一批待审批 3 项（共 4 项，已处理 1 项）"`；`threePlusOne_batchCount=2, threePlusOne_listBatches=0`；
`splitBlocked={ids:[],blocked:[{id:"s2",reason:"这一项已经处理过了，不能再次提交审批。"}]}`，`isBatchItemSelectable(batch,'s2')=false`。

### A4 聊天草稿刷新后恢复、不自动发送、不提交板面；与卡片草稿独立；话题切换不错用 —— **通过（话题切换为只读代码推断）**

命令：`node scripts/interactive-verify/fe-verify-e.mjs chat`（同一次运行内 reload）

1. 输入 `E-复核-刷新前草稿` → 1.5s 后 `draftStatus="草稿已保存在本机"`，
   `localStorage["qio.draft.chat.__unbound__"]={"text":"E-复核-刷新前草稿","seq":1,...}`（话题未确定时用占位键）。
2. `reload` 后：`inputValue="E-复核-刷新前草稿"`、`draftStatus="草稿已保存在本机"`，
   **`turnsCalls=0`、`submissionsCalls=0`** → 恢复了，但没有自动发送、没有提交板面。
3. 键独立：`draftStorageKey('chat','topic-1')="qio.draft.chat.topic-1"`，`draftStorageKey('card','c1')="qio.draft.card.c1"`，`draftKeysDiffer=true`。
4. 【只读代码推断】话题切换：`session.ts:2139-2166 bind(topicId)` 先落盘旧键再读新键；未确定话题时的占位键内容**只**迁移到用户随后进入的那个话题。
   实例只有一个「默认话题」，**没能实机切换话题验证**。

### A5 聊天受理失败、持久写入失败、旧回执迟到：保留新旧文字、不误报保存成功；成功发送不把旧草稿恢复回来 —— **通过（失败路径为模拟）**

命令：`node scripts/interactive-verify/fe-verify-e.mjs chat,chat-fail`

- **受理失败（模拟）**：`failure="发送失败：模拟：受理失败（页面级拦截）（输入已保留，可以重试）"`；
  失败请求还在飞时用户输入的新文字仍是 `inputValue="E-新文字"`（旧稿没有覆盖它）。
- **持久写入失败（模拟 QuotaExceededError）**：
  `draftStatus="草稿未保存：本机存储已满，草稿没有保存成功（清理一些空间后可以重试）（文字还在输入框里，可以重试） 重试保存"`，
  输入框保留文字，**没有出现「已保存」**；点重试（仍失败）状态不变；恢复后再点重试 → `草稿已保存在本机`，存储记录 `seq=6` 的新文字。
- **成功发送（真实后端）**：`/api/turns` ×1、`/submissions` ×0；输入框清空、`draftKeys=[]`（草稿被删且**没有回填**）。
- 【只读代码推断】迟到回执：`drafts.ts:168 isStaleReceipt` + `session.ts:2054-2078 commit()`；
  `stores/interactive.ts:382-429` 用 `draftKeySeq/draftSavedKeySeq` 防止旧回执把新输入标成已保存。

### A6 注释编辑草稿保存失败有提示、有重试、重开编辑器可继续；未确认草稿不变成正式卡片 —— **通过（失败为模拟）**

命令：`node scripts/interactive-verify/fe-verify-e.mjs card-draft`（模拟：页面级 `fetch` 让 `PUT /drafts/...` reject）

| 步骤 | 实测 |
| --- | --- |
| 打开编辑器输入 `E-复核-卡片草稿` | `hintText="草稿未保存：模拟：草稿写入失败（页面级拦截）（输入内容已保留） 重试保存"`，`retryPresent=true` |
| 点「重试保存」（仍失败） | 提示与文字都保留，`editorValue="E-复核-卡片草稿"` |
| 点「取消」 | `editorOpen=false`，卡片数仍 4 |
| 再点「编辑」 | `editorValue="E-复核-卡片草稿"`（重开可继续） |
| 恢复网络后重试 | `hintText="草稿已保存"`，`retryPresent=false` |
| 接口对照 | `liveCards=4, ids=[s_note_a,s_note_b,s_code,s_long]`，`sNoteB` 内容未变 → **未确认草稿没有变成正式卡片** |

### A7 未勾选注释及其链接不进入允许查看范围（含删除路径）；成功提交取消勾选、失败保留勾选 —— **通过【亲自复现 + 只读代码推断】**

后端纯函数（只读脚本，不改数据）：

```powershell
cd backend; uv run --frozen python "$env:TEMP\qio-e-verify\e_visibility_check.py"
```

```
snapshot_cards      = ["n_checked","m_code"]      # 未勾选注释不在
snapshot_links      = ["l2"]                      # 连到未勾选注释的那条链接不在
snapshot_groups     = [{"g1":["n_checked"]}]      # 组只列可见成员
snapshot_selection  = ["n_checked"]               # 选择里的未勾选注释被过滤
delete_unchecked_before_cards = ["n_was_checked"] # 删除且未勾选：不进 before（「撤回」也带不回来）
uncheck_before_cards = []                         # 取消勾选后整条丢弃
```

前端同判据（`pure`）：`localVisible.cards=["m1"]`、`links=[]`、`selection=[]`、`isVisibleCard(uncheckedNote)=false`。

实机（`visibility`，用产品的指针处理路径选中注释卡，再点勾选框）：

| 步骤 | 实测 |
| --- | --- |
| 勾选后 | `visible-range="本次允许 QIO 查看：注释 1 条、材料 2 项、组 1 个…"`，`checks=[{s_note_a,checked:true}]` |
| **提交失败（模拟 500）** | `submit-status="提交失败（未提交） …/submissions -> 500: 模拟：提交失败（页面级拦截）；本次改动与注释勾选已保留，基准没有更新，可以直接再试一次。"`，`checks=[{s_note_a,checked:true}]` → **失败保留勾选** |
| 真实提交成功 | `checks=[{s_note_a,checked:false}]`、载荷 `checkedCleared=["s_note_a"]`、`visibleCards=["s_note_a","s_code","s_long"]` → **成功取消勾选** |
| 文案 | 「提交成功后已自动取消 1 条注释的勾选（不是删除或撤回）」 |

### A8 点击聊天发送与点击板面提交，网络请求互相独立 —— **通过【亲自复现】**

命令：`node scripts/interactive-verify/fe-verify-e.mjs chat`（页面内 `window.fetch` 只记录、不改行为）

- 点聊天发送：`turnsCalls=1`、**`submissionsCalls=0`**（记录里是 `…/api/turns`）。
- 点板面提交：出现 `…/submissions`，`turnsCalls` 不增加。
- 说明：我最初看到 `/submissions` 被记了 2 条，核对后是**我的记录器被包了两层**（同一请求被记两次）；
  证据：`SELECT id,seq,status,created_at FROM board_submissions ORDER BY rowid DESC` 显示该时刻只新增 **1 条**（`succeeded`），DB 里没有重复提交。

---

## 3. B · 布局与审美（要数字）

### 3.1 工具栏（3 尺寸 × 暗/亮；两个主题数字完全相同）

命令：`node scripts/interactive-verify/fe-verify-e.mjs layout`（`raw-layout.json`）

| 视口 | 主题 | 工具栏 高×宽 | 行数 | 纵向溢出 | geoMode |
| --- | --- | --- | --- | --- | --- |
| 1440×900 | dark / light | **60** × 1069 | **1** | 无 | side-by-side |
| 1024×768 | dark / light | **84** × 888 | **2** | 无 | side-by-side |
| 800×600 | dark / light | **84** × 680 | **2** | 无 | side-by-side |

- 800×600 目标「≤ 约 96px、最多两行」→ **84px / 2 行，达标**。
- 行数按 `.tb-edit > *, .tb-submit > *` 可见项的中线聚类（容差 12px）得到，不是数子元素个数。

### 3.2 聊天面板与批量列表同时展开：相交面积必须 0

| 视口 | 聊天面板 | 批量列表 | **相交面积 px²** | 改前基线 |
| --- | --- | --- | --- | --- |
| 1440×900 | 420×520 @ (1004,233) | 400×334 @ (592,107) | **0** | 127200 |
| 1024×768 | 420×476 @ (588,125) | 400×334 @ (176,107) | **0** | 133600 |
| 800×600 | 420×365 @ (364,72) | 336×370 @ (16,99) | **0** | 94864 |

三档都是**并排**（`geoMode=side-by-side`，没有出现 `overlay-switch` 的二选一），不是靠切换回避相交。
消息可读区高度（`[data-im="chat-stream"]` 的 clientHeight）：**312 / 269 / 157 px**；单条消息 37px。

### 3.3 命中测试与连接点

`elementFromPoint` 命中（元素自身或其子元素才算命中）：

| 视口 | chat-input | chat-send | submit | undo / search / add-menu | batch-entry |
| --- | --- | --- | --- | --- | --- |
| 1440×900 | ✅ | ✅ | ✅ | ✅ / ✅ / ✅ | ✅ |
| 1024×768 | ✅ | ✅ | ✅ | ✅ / ✅ / ✅ | ✅ |
| 800×600 | ✅ | ✅ | ✅ | ✅ / ✅ / ✅ | ✅ |

连接点遮挡（`node ... connect`，每个尺寸重复 **3** 次，共 9 次）：`covered=0` **9/9**；
4 个连接点（上/右/下/左）都满足「与卡片局部工具栏相交面积 = 0」且 `elementFromPoint = connect-point`；
同时量到 `toolbarVsTitle=0`、`toolbarVsContent=0`、与底部工具栏相交的连接点数 `=0`。

### 3.4 审美逐项（判断依据，不只统计存在）

命令：`node scripts/interactive-verify/fe-verify-e.mjs aesthetics`

| 项 | 实测依据 | 判断 |
| --- | --- | --- |
| 文字层级 | 标题 `19px Noto Serif SC 600`；次级数量 `11px Cascadia Mono`；主状态/正文 `12.5px system-ui`；主操作「提交给 QIO」`14.5px system-ui`（元信息 11px） | 三声部成立；正文与主要操作没有缩到元信息字号 |
| 主题对比 | 暗色：标题 15.5、数量 **3.1**、保存 6.1、提交 4.92、可见范围 4.73；亮色：16.47 / **3.28** / 6.5 / 4.92 / 4.75 | 主要文字 ≥4.5；**次级数量 3.1–3.28 低于 AA 4.5**（次要问题 §5.2） |
| 动效克制 | 常态整页只有 **1** 个元素带非零 `transition-duration`（0.28s）；互动面板 CSS 自身无动画 | 克制 |
| 减少动态效果 | `Emulation.setEmulatedMedia(prefers-reduced-motion: reduce)` → `reducedMotion=true`，222 个元素时长统一 **90ms**，`--shift-*` 位移令牌归零（`base.css:29-47`） | 遵守（设计上保留 90ms 淡入淡出，不是瞬切） |
| 图标一致性 | `iconOnlyButtons=0`、`iconOnlyWithoutLabel=0`；`svg` 只有 5 个（2400×1600 星球素材），互动版按钮全部文字标签 | 不存在「图标风格不统一」；该项在互动版上没有可比较的图标集（如实说明） |
| 开发用语 | 可见文本扫描 `adapter/接口/求差/状态机/DOM/payload/token/前端/后端/字段/schema/undefined/JSON/TODO` → **可见文本 0 命中**；标记里命中 5 个（接口/DOM/token/后端/字段），全在 CSS 变量名、`data-*` 与注释里 | 界面没有开发用语 |
| 错误与未接入状态 | 顶部常驻「QIO 还没有读取板面」；成功文案「…第一阶段没有接入 QIO 模型调用，QIO 还没有真正读取或理解这些内容」；失败文案「提交失败（未提交）…已保留，基准没有更新，可以直接再试一次」；载荷 `delivery.delivered=false` | **没有**把 `delivered=false` 说成「QIO 已收到板面」 |
| 对齐 / 留白 | 工具栏 `padding: 4px 12px`；间距走 `--sp-*`；顶部数量为次级小字，主状态单独一行 | 符合 §9.6「数量退到次级」 |

### 3.5 窄屏共存

800×600 下 `cramped` 为 **false**：两面板仍并排且不相交。聊天面板该尺寸高 365px、消息可读区 157px（约 4 行）——可用但偏紧（§5.3）。

---

## 4. C · 反例、回归与稳定性

### 4.1 三条反例（旧实现必须过不了）

命令：`node scripts/interactive-verify/fe-verify-e.mjs pure`（真实页面对真实模块求值）

1. **默认组名**：`nextDefaultGroupName(['组 1','组 2'])` → `"默认组名"`；`createGroup(带「组 7」的板面, …)` → 新组名 `"默认组名"`，老组仍 `"组 7"`。
   旧实现（按已有名字编号）会给出 `"组 1"/"组 3"` → **必然失败**。实机对照：改名成「组 7」→ 刷新仍是「组 7」（§2 A1）。
2. **按创建秒归批**：4 个意图 `createdAt` 截断到秒完全相同、无可证明来源 → `groupIntentsByBatch(...).length=4`、`batchesWithList(...).length=0`。
   旧实现（已作废的「同一秒同批」）会得到 1 批 4 项 → **必然失败**。实机对照：清掉本地记录后刷新，四项列表不出现（§2 A2）。
3. **按剩余待审批数判资格**：同一批**产生** 4 项、已处理 3 项 → `batchesWithList(...).length=1`（total=4, pending=1）。
   旧实现（剩余 ≥4 才给列表）会得到 0 → **必然失败**。实机对照：处理掉 1 项后入口仍在且显示「待审批 3 项（共 4 项，已处理 1 项）」（§2 A3）。

### 4.2 回归

| 命令 | 实际输出 |
| --- | --- |
| `cd backend; uv run --frozen pytest -q tests/test_interactive_board.py tests/test_interactive_submission.py tests/test_interactive_intents.py tests/test_interactive_integration.py tests/test_interactive_verify.py` | junit：`tests="158" failures="0" errors="0" skipped="0" time="22.321"`，退出码 0 |
| `cd frontend; npx vue-tsc --noEmit` | 退出码 **0**（无输出） |
| `cd frontend; npx vitest run src/interactive src/stores src/components/interactive` | **40 个文件 / 510 个用例全部通过**，`EXIT=0` |

### 4.3 已知待确认现象：健康探测用例复跑（没有降门槛、没有删测试）

用例：`backend/tests/test_interactive_during_heavy_work.py::test_health_probe_stays_responsive_while_slow_prediction_runs`（门槛 `gap_ms < 100`）

```powershell
$test = 'tests/test_interactive_during_heavy_work.py::test_health_probe_stays_responsive_while_slow_prediction_runs'
for ($i=1; $i -le 5; $i++) {
  $cpuBefore = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
  $sw = [Diagnostics.Stopwatch]::StartNew()
  & cmd /c "cd /d D:\qio-dev\qio-ui\backend && uv run --frozen pytest -q --no-header -p no:cacheprovider $test 2>&1" | Out-Null
  $code = $LASTEXITCODE; $sw.Stop()
  $cpuAfter = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
  "run $i : exit=$code 用时=$([int]$sw.ElapsedMilliseconds)ms CPU前/后=$cpuBefore/$cpuAfter%"
}
```

```
run 1 : exit=0 用时=6485ms CPU前/后=56/24%
run 2 : exit=0 用时=4802ms CPU前/后=35/38%
run 3 : exit=0 用时=3376ms CPU前/后=19/37%
run 4 : exit=0 用时=3072ms CPU前/后=40/52%
run 5 : exit=0 用时=3847ms CPU前/后=46/100%
```

结论：**5/5 通过**；其中 3 次与我的探针任务并发跑（CPU 最高 100%）仍然通过。没有观察到 130ms > 100ms 的失败。

---

## 5. 问题清单

### 阻断（0）

无。

### 重要 1 · 单击一张与别的未分组卡片重叠的卡片会**直接成组**（选择手势变成板面改动）

- 现象：`BoardCanvas.vue:434-451 onPointerUp()` 在「卡片拖动」状态下**无条件** `dropCard(current, drag.cardId, drag.x, drag.y)`；
  卡片拖动与平移/框选不同，**没有位移门槛**。于是「按下即松开」（纯单击、用单击选中卡片）也会做一次落点判定：
  只要点中的卡片与另一张未分组卡片**重叠面积 ≥25% 且目标中心被覆盖**（`board.ts:893-960`），立刻成组并自动保存。
- 我怎么撞上的（**亲自复现**，副作用触发）：复核 A6 时用真实鼠标单击 `s_note_b` 想选中它 → 板面立刻多出一组。证据链：
  1. `GET /state`：`g_muxyu67g1i9vt created=2026-10-07T10:29:38.668Z members=[s_note_b,s_long]`（这两张卡在 10:28 被我解除组后仍重叠）；
  2. 同一时刻该场景对板面的唯一指针动作就是这次 `from=(520,184) to=(520,184)` 的点击（步骤 JSON `%TEMP%\qio-e-verify\steps-card-draft.json` 可核对）；
  3. 之后 A6 的测量里卡片数不变、编辑器打开的是 `s_note_b`，说明这次点击被当作「落点在同一位置」的拖动处理。
- 【只读代码推断】机制：`BoardCanvas.vue:370-393`（pointerdown 即 `onDragStart`）→ `:434-441`（pointerup 即 `dropCard`）→ `board.ts:975-1010 resolveDrop/dropCard`（重叠即成组）。
- 人工复现步骤（约 1 分钟）：两张卡片成组后，选中该组 → 点「解除组」（成员留在原地、仍然重叠）→ **不要拖动**，直接单击其中一张 → 两张卡立刻重新成组并自动保存，工具栏上方出现「…已放入组…」回执。
- 影响：单击（选择 / 打开局部工具栏）这个最高频手势会静默改动并保存板面；可用撤销回退、回执也说清了，所以不是数据损坏。
- 建议方向（**只报告，不改**）：给卡片拖动加与平移一致的位移门槛（例如 >3px 才算拖动），或比较起落点是否位移；「重叠成组」只在真实拖动后松手时触发。

### 次要 2 · 次级数量文字对比度不足（3.1 暗 / 3.28 亮 < AA 4.5）

- 依据：`aesthetics` 场景量到 `[data-im="board-counts"]`（11px Cascadia Mono）对比度 **3.1（暗）/3.28（亮）**，同页主要文字 4.73–16.47。
- 复现：`node scripts/interactive-verify/fe-verify-e.mjs aesthetics`，看「次级-数量」的 `ratio`。

### 次要 3 · 800×600 同时展开时消息可读区只剩 157px

- 依据：`layout` 场景 800×600：`chat-stream clientHeight=157`（单条 37px，约 4 行），面板高 365px。
- 影响：不相交、不遮挡（§3.2/§3.3 已证），但滚动阅读偏紧；并排已是该尺寸下的最好结果，记为观察项。

### 次要 4 · 「尚未提交」在两处重复表达

- 依据：顶部主状态 `已保存（尚未提交）`，提交区又写 `未提交 尚未提交：当前有 N 项改动还没有交给 QIO。编辑与保存都不会调用 QIO。`（同屏可见，见 `e-layout-800x600-dark.png` 底栏）。
- §9.6 要求「『已保存』与『已提交』各自只设一处主状态」；现在两处都在讲提交状态，措辞不矛盾但重复。

### 观察（不是缺陷，避免误判）

- 最初的请求记录里 `/submissions` 出现 2 条，是**我的记录器被包了两层**；DB 里只新增 1 条提交（§2 A8）。
- 顶部「任务」数字在复核期间变化，来自演示入口与我自己的操作，不是异常。

---

## 6. 没能验证（如实列出）

1. **真实 QIO 回复**：无模型凭据；`delivery.delivered` 恒为 `false`，只能验证受理、路由与文案。
2. **跨浏览器会话（重开应用）后的草稿恢复**：探针每次调用新建实例，本环境 localStorage 跨调用不保留；只验证了同一次运行内 `reload` 的恢复。
3. **话题切换不错用**：实例只有一个「默认话题」，无第二个话题可切，仅【只读代码推断】。
4. **真实历史「组 N」数据**：实例里没有；用「改名成『组 7』→ 刷新仍是『组 7』」+ 后端用例替代。
5. **真实存储写满 / 浏览器禁用 localStorage**：用页面级模拟代替（已标注）。
6. **提交失败的真实原因**（网络中断、服务端 5xx）：用页面级 500 模拟代替（已标注）。
7. **「单击重叠卡片成组」的干净自动化复现**：自动化尝试因卡片被预览浮层遮挡没取到可点位置（`no-free-point`），
   但**真实鼠标那一次已经发生**（§5 重要 1 证据链），人工复现步骤已给出。
8. **长时间运行 / 多用户并发**：只做了单实例、单会话检查。

---

## 7. 我写了什么、对集成实例做了什么

### 7.1 只写了这两个文件（+ 截图）

- `docs/interactive-ui-verify-integrated.md`（本报告）
- `scripts/interactive-verify/fe-verify-e.mjs`（我的探针脚本，14 个场景）
- 截图：`docs/interactive-ui-screenshots/e-*.png`（`e-layout-*`、`e-connect-*`、`e-aesthetic-*`、`e-batch-*`、`e-chat-*`、`e-card-draft-*`、`e-visibility-*`、`e-group-*`、`e-click-merge-*`、`e-cleanup-final`、`e-restore-final`），随提交进仓库。
- 临时文件都在 `%TEMP%\qio-e-verify\`（步骤 JSON、原始 JSON、只读 python 检查脚本），不在仓库里。

### 7.2 对集成实例（`%TEMP%\qio-ui-integration`）的影响与复位

| 复核动作 | 影响 | 复位 |
| --- | --- | --- |
| 新建 2 张文字卡片用于重叠成组 | 板面多 2 张卡 | 用卡片工具栏「删除」删掉（soft-delete），live 卡片仍是原来的 4 张 |
| 拖动 `s_long` 制造重叠 | `s_long` 位置改变 | 用同一条拖动路径把它放回 `(380,379)`（`restore` 场景输出 `dx=0, dy=330.06`，之后位置已回到原位） |
| 组名改成「组 7」/「E-复核-自定义组名」 | 组名变化 | 改回「默认组名」 |
| 勾选 `s_note_a` 后提交 | 成功提交会清空勾选 | 结束前重新勾选回 `checked=true`（与开始时一致） |
| 演示意图：处理掉 4 项又重建 | 意图 id 变化 | 结束时重新生成 **4 项 pending 演示意图** |
| 板面提交 2 次（一次真实、一次模拟失败） | 新增提交记录、基准推进 | 无法回退（提交是追加记录）；属产品正常使用痕迹，如实记录 |

最终状态核对（复核结束时的实测输出）：

```powershell
Invoke-RestMethod http://127.0.0.1:8931/api/interactive/boards/board_default/state
# live cards=4：s_note_a(checked=True) / s_note_b / s_code / s_long(y=379，已回到原位)
# groups=1：s_group = s_note_a,s_code，name=默认组名，defaultName=True；links=1
Invoke-RestMethod http://127.0.0.1:8931/api/interactive/boards/board_default/intents
# pending=0，needs_update=4（i_f11bf6258b4b / i_84fdf689a94b / i_f09780b41d67 / i_256721caa6bb，全部 demo=True）
```

**关于这 4 项演示意图**：复核开始时它们是 `pending`。我在 A7 里做了一次**真实提交**，
服务端按设计把「受这次提交影响的意图」标成 `needs_update`（§1.6：提交后由 QIO 判断是否需要更新预览），
所以它们现在是 `needs_update` 而不是 `pending`——这是产品行为，不是复核留下的脏数据；
但我**没能**通过界面把它们变回 `pending`（`needs_update` 需要走「更新预览」路径），如实记录。
如果下一位复核者需要 `pending` 状态的四项批次，可以在演示入口再点一次「生成 4 项演示意图」并走一次预览更新。
