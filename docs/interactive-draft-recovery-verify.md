# 草稿恢复补齐与失败处理 · 独立验收报告（第四轮 · 子智能体 D）

> 验收人：独立验收子智能体 D（工作区 qio-recover-d，分支 wt/rec-d-verify）
> 基线：f436ad8。**本报告只写我自己的用例、探针、截图与结论；不改任何产品代码。**
> 证据分级：【状态/单元】【组件/DOM】【真实浏览器】【真实请求】【视觉】【模拟】【没能验证】。

## 0. 摘要

> **最终提交验收（独立复核子智能体 D2，2026-10-08）**：集成提交 `2e816f3` 上，我自己的 12 条独立用例
> 与恢复顺序 4 条**全部通过**；主智能体对我用例做的三处改动经核对**都是驱动修正**（其中一处削弱了
> 单测强度，已用真实浏览器证据补回，见 §9.3）；命令全绿（vue-tsc 0 / 前端全量 135 文件 1360 用例 /
> 后端 2113 passed 9 skipped / check_docs 通过 / 实机 24 项全通过）；我另外写了 4 个文件 11 条反例，全部通过。
> 仍然存在的产品缺陷见 §9.6：**480×600 边缘宽度下聊天面板的输入区被面板裁掉（长失败原文时更明显）**，
> 800×600 桌面最小窗口正常。**结论与证据从 §9 开始；§0–§8 是基线阶段的原始记录。**

### 0.1 基线阶段摘要（2026-10-08 早前，未改写）

- 基线 f436ad8 上，我按**用户行为**写的 12 条独立用例全部失败（不是函数不存在、不是选择器改名）：
  卡片草稿防抖前刷新恢复不到、清除后复活、本机写入失败无说明、对话页无失败原因与找回入口、
  刷新后失败事实消失、未绑定话题发送成功后文字回到输入框、提交失败默认区显示通用说明、
  480px 切换条没有真实定位/预留高度。
- 真实浏览器探针在同一基线跑 11 个检查点，**10 个失败**（1 个是启动检查）：
  切换条 computed position 是 static、与聊天面板相交 6424px²、关掉面板后切换条整个消失。
- 修复后的正式验收（第 7 节）等主智能体给出集成提交号后执行；本节结论**只代表基线**。

## 1. 基线与方法

| 项 | 值 |
| --- | --- |
| 基线提交 | f436ad8（冻结卡片草稿的两种记录身份） |
| 我的分支 | wt/rec-d-verify |
| 前端 | vite.e2e.config.ts，端口 5454，`VITE_QIO_BACKEND_URL=http://127.0.0.1:8954` |
| 后端 | uvicorn agent.main:create_app --factory --port 8954（QIO_DEV_INSECURE=1、PYTHONPATH=backend/src、QIO_DATA_DIR=%TEMP%/qio-rec-d） |
| 浏览器 | Edge headless + 持久化 profile `%TEMP%/qio-chrome-d` + 独立调试端口 9554 |
| 我的用例 | frontend/src/stores/__tests__/d5CardDraftRecovery.verify.test.ts、d5FailedSendRecovery.verify.test.ts；frontend/src/components/__tests__/d5ComposerRecovery.verify.test.ts；frontend/src/components/interactive/__tests__/d5SubmitFailure.verify.test.ts、d5LocalWriteFailure.verify.test.ts、d5SwitchBarGeometry.verify.test.ts |
| 我的探针 | scripts/interactive-verify/d5-recovery-probe.mjs |
| 证据 | docs/interactive-ui-screenshots/d5-baseline-vitest.txt、d5-baseline-probe.json、r5-d-baseline-*.png |

命令（真实执行，原样）：

- `cd frontend; npx vitest run <上面 6 个文件> --reporter=verbose` → `Test Files 6 failed (6) / Tests 12 failed (12)`（输出存档 d5-baseline-vitest.txt）
- `cd frontend; npx vue-tsc --noEmit` → exit 0（无输出）
- `node scripts/interactive-verify/d5-recovery-probe.mjs --app http://127.0.0.1:5454 --label baseline --port 9554 --profile %TEMP%/qio-chrome-d` → `步骤：11，失败：10`

## 2. 基线行为性失败逐条证据

### 2.1 场景 1：首次编辑 → 600ms 防抖前刷新 → 恢复不到那段文字（§11.1）

用户行为：新建一张文字注释 → 选中 → 编辑 → 输入文字 → **立刻刷新**（远小于 600ms）。

- 【状态/单元】用例 `场景 1：防抖未到就刷新：本机记录必须被发现并恢复出来`：
  `→ 刷新后没有发现只存在于本机的新草稿：输入框的文字恢复不出来: expected false to be true`
- 【状态/单元】`恢复后没有把这份草稿保存回服务器（saveDrafts 调用次数 0）: expected false to be true`
- 【真实浏览器】探针步骤 1 失败，evidence（d5-baseline-probe.json）：
  `typing.value = "刷新前刚写的新文字-D5-1791444910236"`；刷新前本机记录确实写下了：
  `qio.draft.card.local-c_muz81ns411dbi={"text":"刷新前刚写的新文字-D5-1791444910236",...}`；
  刷新后 `editorValueAfterReload = ""` —— 本机有记录，恢复却完全发现不了它。
- 【视觉】docs/interactive-ui-screenshots/r5-d-baseline-01-card-draft-before-debounce.png

### 2.2 场景 2：服务器已有旧草稿 → 用户清除 → 刷新后草稿复活（§11.2）

用户行为：编辑并等 1.6s（服务器已有草稿）→ 点「完成编辑」（确认编辑＝清除草稿）→ 撤销这次编辑让正式内容与旧草稿分开 → 刷新 → 重新编辑。

- 【状态/单元】`清除最后一份草稿也必须发出同步请求`：
  `→ 清除了草稿，服务器上却还留着它：这次清除没有同步（刷新后必然复活）: expected 服务器上的旧草稿 to be undefined`
- 【状态/单元】`刷新之后旧草稿又回来了（用户已经清除过它）: expected 服务器上的旧草稿 not to be 服务器上的旧草稿`
- 【真实浏览器】探针步骤 2 失败：`刷新后旧草稿又回来了（编辑器里冒出 "服务器上的旧草稿-D5-1791445464485"，服务器草稿仍在：true）`；
  请求证据：确认编辑前 `GET /api/interactive/drafts/board_default` → `{"drafts":{"card:c_...":"服务器上的旧草稿-D5-..."}}`；
  确认编辑并等 1.6s 后服务器草稿仍在；刷新后重新打开编辑器，编辑框里是那份**已经清除过**的旧草稿。
- 【视觉】docs/interactive-ui-screenshots/r5-d-baseline-02-draft-revive.png

### 2.3 场景 3：本机写入失败（localStorage.setItem 抛错）→ 界面没有任何说明（§11.3）

用户行为：只让 `qio.draft.*` 的写入抛 QuotaExceededError（其他键正常，应用其余功能可用）→ 打开卡片编辑器输入文字。

- 【组件/DOM】用例 `本机存储写入失败：编辑处要如实说明，并保留输入内容`：
  `→ 本机写入失败后，界面上一句解释都没有: expected 文字注释输入过程只保存草稿；点「完成编辑」才形成有效文字状态。草稿保存中…… to match /本机|本地|这台|关闭后|重开后|恢复副本/`
- 【真实浏览器】探针步骤 3 失败：`界面上一句解释都没有（本机写入失败后只显示「草稿保存中…」）`；
  evidence：`cardDraftHintImmediately = "草稿保存中…"`，等服务器保存成功后 `cardDraftHintAfterServerSave = "草稿已保存"`（本机副本却没写进去）。
- 【视觉】docs/interactive-ui-screenshots/r5-d-baseline-03-local-write-failure.png

### 2.4 场景 4：普通对话页发送失败 → 没有本次原因，也没有找回原文的入口（§11.4）

用户行为：对话页输入并发送；`/api/turns` 由探针明确拦截为 500 + `{"detail":"模拟的发送失败原因-D5（探针拦截）"}`（【模拟】失败，但处理流程是应用自己的）。

- 【组件/DOM】`失败后页面上直接看得见本次真实原因`：
  `→ 对话页没有显示这次发送失败的真实原因: expected 默认话题Enter 发送 · Shift+Enter 换行↑ to contain 网络中断`
- 【组件/DOM】`输入框已有新文字时，仍要有取回失败原文的入口且两份都保留`：`expected 0 to be greater than 0`
- 【真实浏览器】探针步骤 4 两项失败：`composerText = "默认话题Enter 发送 · Shift+Enter 换行↑"`（整页没有失败原因），
  `recoveryButtons = []`（没有任何放回/找回/取回/恢复/互换入口）；输入框虽然被会话层自动放回了原文，但用户看不到为什么失败、也没有明确的恢复操作。
- 【视觉】docs/interactive-ui-screenshots/r5-d-baseline-04-composer-failure.png

### 2.5 场景 5：发送失败后刷新 → 失败事实丢失（§11.4）

用户行为：上一步失败后按 F5 正常刷新。

- 【状态/单元】`刷新（新 store）后仍能知道这次失败并取回原文`：
  `→ 刷新之后失败原文不见了（它只存在内存里）: expected undefined to be 服务器挂了的时候写下的原文`
- 【真实浏览器】探针步骤 5 失败：刷新后 `composerText` 里没有任何失败信息；输入框里虽然有原文（聊天草稿本身持久化了），
  但**这次失败与原因已经查不到**，用户不知道这段文字是没发出去的。
- 【视觉】docs/interactive-ui-screenshots/r5-d-baseline-05-composer-after-reload.png

### 2.6 场景 6：currentTopicId=null 发送 → 绑定真实话题 → 成功 → 已发送文字回到输入框（§11.5）

用户行为：会话上下文还没绑定话题时输入并发送 → 受理返回前绑定真实话题 T1 → 受理成功。

- 【状态/单元】`currentTopicId=null 发送 → 绑定真实话题 → 受理成功：输入框与草稿都要干净`：
  `→ 已经成功发送的文字又回到了输入框（清理找的是旧位置）: expected 未绑定话题时写的字 to be ""`
- 【没能验证 - 真实浏览器】：探针没有单独构造「话题绑定发生在请求返回之前」的时序（现有实例只有一个话题，时序窗口只有几十毫秒）；
  该条目前只有【状态/单元】证据（详见第 5 节）。

### 2.7 场景 7：提交失败默认区显示的是通用说明，看不到本次真实原因（§11.6）

用户行为：点「提交给 QIO」；`/submissions` 由探针明确拦截为 500 + `{"detail":"模拟的提交失败原因-D5（探针拦截）"}`（【模拟】失败）。

- 【组件/DOM】`上一次提交成功、本次请求失败：默认区域必须含本次原因`：
  `→ 默认失败区域看不到本次提交失败的真实原因: expected 本次可见：材料 0 项 · 注释 0 条提交失败（未提交）没有可提交的改动… to contain 网络中断`
- 【组件/DOM】`上一次提交也失败过：默认区域不许拿上一次的旧原因当本次原因` 同样失败（`submitFailureText(store.lastSubmission)` 读的是上一次结果）。
- 【真实浏览器】探针步骤 7 失败：默认失败区文本为
  `提交失败不会丢改动：已保存的板面、本次注释勾选都保留，上次成功提交的基准没有被更新`，
  `detailsOpen = false`（详情未展开），本次原因完全看不到。
- 【视觉】docs/interactive-ui-screenshots/r5-d-baseline-07-submit-failure.png

### 2.8 场景 8：480px 关闭一个面板后切换条没有真实布局/预留高度（§11.7）

用户行为：480×600 窄窗口 → 生成同一批 4 项演示意图 → 打开聊天 → 打开批量列表（空间不足，只留最近打开的批量列表）→ 量 DOM → 点切换条「看对话」→ 量 DOM → 关掉当前面板 → 再量。

- 【组件/DOM（几何变量）】用例 `切换条要给剩下的面板预留真实高度`：
  `→ 切换条压在剩下的面板上：面板底边 450px，切换条顶边 408px（没有为切换条预留高度）: expected 450 to be less than or equal to 408.5`
- 【真实浏览器】探针步骤 8 三项全失败（d5-baseline-probe.json）：
  1. `computed position 是 static`：`barStyleAttr = "left: 16px; bottom: 276px; height: 34px;"`，但 `barComputed.position = "static"` →
     left/bottom 完全不生效，实际矩形 `{left:140,top:60,right:464,bottom:94,width:324,height:34}`（跑到顶部，不在应有的一行）。
  2. 切到聊天后：`intersectionChatSwitchBar = 6424`（6424px²），聊天面板 `{left:12,top:72,right:432,bottom:331}`，
     面板底边超出切换条顶边 `chatBottomVsBarTop = 271`px。
  3. 关掉当前面板后：`switchBar = null`、`hits.bar.found = false` —— 切换条整个消失，没有明确的切换入口。
- 【视觉】docs/interactive-ui-screenshots/r5-d-baseline-08a-switch-bar-cramped.png、r5-d-baseline-08b-switch-bar-chat.png、r5-d-baseline-08c-after-close-panel.png

## 3. 我的独立用例覆盖到的契约条款

| 用例文件 | 契约条款 | 基线结果 |
| --- | --- | --- |
| d5CardDraftRecovery.verify.test.ts（4 条） | §11.1 / §11.2 | 4/4 失败 |
| d5FailedSendRecovery.verify.test.ts（2 条） | §11.4 / §11.5 | 2/2 失败 |
| d5ComposerRecovery.verify.test.ts（2 条） | §11.4 | 2/2 失败 |
| d5SubmitFailure.verify.test.ts（2 条） | §11.6 | 2/2 失败 |
| d5LocalWriteFailure.verify.test.ts（1 条） | §11.3 | 1/1 失败 |
| d5SwitchBarGeometry.verify.test.ts（1 条） | §11.7 | 1/1 失败 |
| d5-recovery-probe.mjs（10 个行为检查） | §11.1–§11.7 | 10/10 失败 |

## 4. 缺陷清单（基线 f436ad8）

### 4.1 阻断（用户会丢字 / 文字错位）

1. **本机独有的新草稿恢复不了（§11.1）**——首次编辑在 600ms 防抖前刷新，整段文字丢失。
   复现：新建文字卡 → 编辑 → 输入 → 立刻 F5 → 重新编辑：输入框为空（本机记录 `qio.draft.card.local-<id>` 明明存在）。
   证据：2.1（单元 + 真实浏览器）。
2. **清除草稿不同步，刷新后旧草稿复活（§11.2）**——用户确认编辑（清除草稿）后刷新，编辑器里又冒出已经清除的旧草稿。
   复现：编辑并等 1.6s → 完成编辑 → 撤销 → F5 → 重新编辑。证据：2.2。
3. **未绑定话题发送成功后，已发送文字回到输入框（§11.5）**——`settleSend` 按点击时的旧键清理，绑定迁移后找不到新位置的记录。
   证据：2.6（单元）。

### 4.2 重要（失败不可见 / 无法找回）

4. **本机写入失败完全不可见（§11.3）**——隐私模式/配额满时用户以为草稿保存了；界面显示「草稿保存中…」→「草稿已保存」。证据：2.3。
5. **对话页发送失败没有本次原因，也没有找回入口（§11.4）**——原文会被自动放回，但用户不知道失败原因，输入框已有新文字时也没有互换/找回入口。证据：2.4。
6. **失败事实只存在内存（§11.4）**——刷新后失败原文与原因查不到（草稿本身还在，但「它没发出去」这件事没了）。证据：2.5。
7. **提交失败默认区显示通用说明/上一次结果（§11.6）**——`failureText` 读 `lastSubmission`，不读 `submitError`；本次原因只在展开详情里（旧文案甚至带着「提交接口」这类实现用语）。证据：2.7。

### 4.3 次要（布局与可读性）

8. **480px 切换条没有真实布局（§11.7）**——computed `position: static`，inline 的 left/bottom 无效；与聊天面板相交 6424px²；关掉面板后切换条消失（没有明确切换入口）。证据：2.8。
9. **切换条缺少为面板预留的高度（§11.7）**——几何计划在「只开一个面板」时按 side-by-side 计算（不预留切换条），
   480×600 下聊天面板底边可到 450px、切换条顶边 408px（单元用例量的就是这 42px 重叠）。证据：2.8 与用例 d5SwitchBarGeometry。

## 5. 没能验证清单（截至基线）

1. **真实浏览器里的关闭重开**：基线阶段只做了正常刷新（同一页面 reload）；探针已实现 `--reopen`（关掉浏览器进程、用同一个 profile 重开），
   但**基线阶段没有执行**，修复后验收必须执行并留下证据（不能用新空 profile 冒充）。
2. **场景 6 的真实浏览器时序**：实例只有一个话题，`currentTopicId=null → 绑定` 的窗口只有几十毫秒，探针没有构造出来；只有【状态/单元】证据。
3. **强制结束 / 崩溃恢复**：基线不改动这一点，本轮同样不验证（与契约 §10.5 的边界一致）。
4. **真实 QIO 未接入**：失败与交付状态都是模拟拦截；没有模型凭据，不伪造 QIO 回复。
5. **事件缓冲区负载敏感用例**：本轮尚未跑全量 `npx vitest run`（修复后按最终提交跑全量并记录）。

## 6. 产品缺陷与脚本缺陷分开写

### 6.1 产品缺陷（上面第 4 节的全部 9 条）

全部能用「用户怎么做 → 期望看到什么 → 实际看到什么」复现，且与具体函数名/选择器无关。

### 6.2 我的脚本/用例缺陷（不影响上面结论，但如实记录）

1. 场景 2 的第一版探针把「草稿文本」与「确认后的正式正文」写成同一段文字，导致"草稿复活"与"内容一致"无法区分（当时误判为通过）。
   已改成：确认编辑后**再撤销这次编辑**，让正式内容与旧草稿分开，并同时核对服务器草稿接口（见 2.2 与探针现版本）。
2. 场景 3 的第一版探针在输入后 sleep 500ms 才读提示，那时服务器已经保存成功、提示是「草稿已保存」，削弱了「服务器尚未保存」这个窗口的判定。
   现版本改为输入后立即读一次（`cardDraftHintImmediately`），等服务器保存后再读一次（`cardDraftHintAfterServerSave`），两次都记录。
3. 探针第一版点击「添加 → 文字」之间没有等待菜单展开，导致没有建出卡片、三个场景假失败；现在会 `waitFor(add-menu-list)` 并把点击结果写进 notes。
4. 探针第一版用相对路径 `fetch("/api/interactive/drafts/...")`，在 dev server 上取回的是 index.html；现在固定打后端地址（`--backend`，默认 http://127.0.0.1:8954）。

## 7. 修复后正式验收计划（等主智能体给集成提交号）

1. 在集成提交上重跑：`npx vue-tsc --noEmit`、`npx vitest run`（全量，含负载敏感用例的首次结果与复验）、
   `cd backend; uv run --frozen pytest`、`python scripts/check_docs.py`；本报告逐条替换为通过输出。
2. 真实浏览器：新 store/新页面进程 + 持久化 profile，**正常刷新**与**正常关闭重开**分别验证（探针 `--reopen`）。
3. 真实请求证据：文字发送只打 `/api/turns`、板面提交只打 `/submissions`；清除最后一份草稿确实发出持久化请求；旧请求与后续版本先后关系正确。
4. 视觉矩阵：1440×900 / 1024×768 / 800×600 × 明暗 + 480px 边缘；聊天展开、长失败原文、长提交原因、批量列表展开、两者同时请求展开、
   输入框后方有密集卡片文字与关系线；保留相同场景的改前/改后截图（改前已存 `r5-d-baseline-*`，改后用 `r5-d-after-*`）。
5. 结论逐条标注证据层级；没能验证的继续如实列出。

## 8. 边界

- 我没有修改任何产品代码；提交只包含我自己的用例、探针、报告与证据目录。
- 没有合并 main、没有改数据库与迁移、没有接真实 QIO。
- 失败注入（`/api/turns` 与 `/submissions` 的 500）都明确标注为探针拦截（【模拟】），但被验证的是**应用自己的处理流程**。

---

## 9. 最终提交上的独立复核（子智能体 D2 · 2026-10-08）

> **验收对象**：`D:\qio-dev\qio-recover` 分支 `fix/interactive-draft-recovery-completion` 的提交
> `2e816f3`（含前一位验收者 D 的用例与报告）。我的工作区 `D:\qio-dev\qio-recover-d`（分支
> `wt/rec-d-verify`）已用 `git merge 2e816f3` **fast-forward** 到同一提交（没有产生合并提交，也没有改产品文件）。
> 本节只写我自己的用例、探针、截图与结论。
>
> **证据层级**：【状态/单元】【组件/DOM】【纯几何】【真实浏览器】【真实请求】【视觉】；机制性的失败用
> **【模拟】** 明确标注（请求拦截、本机存储注入、板面数据种子）。凡是只有单元/组件证据的条目，都会写明
> 「真实浏览器未覆盖」。

### 9.1 命令与输出（原样）

| 命令 | 输出（原样） | 判定 |
| --- | --- | --- |
| `npx vue-tsc --noEmit` | 无输出，`TSC_EXIT=0` | 通过 |
| `npx vitest run`（全量，第一遍 22:54） | `Test Files  135 passed (135)` / `Tests  1360 passed (1360)` / `Duration  85.00s` | 通过 |
| `npx vitest run`（全量，最终复跑 23:21） | `Test Files  135 passed (135)` / `Tests  1360 passed (1360)` / `Duration  66.95s` | 通过 |
| 全量中间一次（与探针/隔离用例并发、`environment 1465s`） | `Test Files  1 failed | 134 passed`：`eventBufferOverflow.verify.test.ts`「同步期间灌入 2 万条事件」（30s 上限）超时 | **负载敏感**，与本轮改动无关：该文件隔离重跑 `4 passed (4)`（10.9s） |
| `cd backend; uv run --frozen pytest` | `2113 passed, 9 skipped, 1 warning in 371.49s (0:06:11)` | 通过 |
| 同一命令复跑（与全量前端 + 真实浏览器并发时） | `FAILED tests/test_cmd_tools.py::test_proc_list_ok - TimeoutError`，`UV_EXIT=1` | **负载敏感**，非本轮改动：该用例在隔离重跑下通过（见下一行） |
| `uv run --frozen pytest tests/test_cmd_tools.py::test_proc_list_ok -q` | `.` → `PYTEST_EXIT=0` | 通过 |
| `python scripts/check_docs.py` | `文档一致性检查通过（31 个里程碑条目）`，`DOCS_EXIT=0` | 通过 |
| `node scripts/interactive-verify/fe-scenarios.mjs --only=18,19,21,22` | `合计 24 项，通过 24 项，失败 0 项` | 通过 |
| `node scripts/interactive-verify/d5-recovery-probe.mjs … --visual` | `步骤：17，失败：3`（三条都是同一件事：480px 聊天输入区，见 §9.6） | 有失败，见 §9.6 |

实机场景的关键输出（原样，节选）：

    --- 场景 18 ---
    PASS  18 刷新后重新打开编辑器：未保存的文字能恢复  —— {"got":"首次编辑未保存610295","want":"首次编辑未保存610295"}
    --- 场景 19 ---
    PASS  19 确认（清除这条草稿）后服务端不再保留它  —— {"mark":"server-cleared","ok":true,"ms":4}
    PASS  19 删除卡片后它的草稿也从服务端清掉  —— {"mark":"del-drafts","ok":true,"ms":5}
    --- 场景 21 ---
    PASS  21 默认区域说出本次请求的真实原因（网络层），不是通用保留说明  —— 失败原因：Failed to fetch…
    PASS  21 提交按钮仍然可点、没有被失败文字挤坏  —— {"w":98,"h":41,"disabled":false}
    --- 场景 22 ---
    PASS  22 480×600 切换条有真实矩形且在视口内  —— {"t":360,"b":394,"l":81,"r":464,"w":383,"h":34}
    PASS  22 480×600 切换条已就位（不是 static）  —— {"position":"fixed","display":"flex",…}

### 9.2 我的 12 条独立用例：基线失败 → 最终提交通过（逐条对照）

`cd frontend; npx vitest run d5` 在 `2e816f3` 上：`Test Files 9 passed (9)` / `Tests 48 passed (48)`。
其中属于我的 12 条（基线记录见 §2、§4）逐条对照如下：

| 用例 | 基线 `f436ad8` | 最终提交 `2e816f3` |
| --- | --- | --- |
| d5CardDraftRecovery.verify.test.ts · 场景 1「防抖未到就刷新：本机记录必须被发现并恢复出来」 | 失败：`expected false to be true`（本机记录没有被发现） | 通过（12ms）；同一个文件里的探针证据见 §9.4 场景 1 |
| d5CardDraftRecovery.verify.test.ts · 场景 1「恢复出来的本机草稿要重新排一次保存」 | 失败：`saveDrafts 调用次数 0` | 通过（5ms） |
| d5CardDraftRecovery.verify.test.ts · 场景 2「清除最后一份草稿也必须发出同步请求」 | 失败：`服务器上的旧草稿` 仍存在 | 通过（5ms） |
| d5CardDraftRecovery.verify.test.ts · 场景 2「清除后刷新，旧草稿不许复活」 | 失败：旧草稿又回来了 | 通过（3ms） |
| d5FailedSendRecovery.verify.test.ts · 场景 5「刷新（新 store）后仍能知道这次失败并取回原文」 | 失败：`expected undefined to be 服务器挂了的时候写下的原文` | 通过（20ms） |
| d5FailedSendRecovery.verify.test.ts · 场景 6「currentTopicId=null 发送 → 绑定真实话题 → 受理成功：输入框与草稿都要干净」 | 失败：已发送的文字又回到输入框 | 通过（7ms） |
| d5ComposerRecovery.verify.test.ts · 「失败后页面上直接看得见本次真实原因」 | 失败：看不到本次真实原因 | 通过（72ms） |
| d5ComposerRecovery.verify.test.ts · 「输入框已有新文字时，仍要有取回失败原文的入口且两份都保留」 | 失败：`expected 0 to be greater than 0` | 通过（29ms） |
| d5SubmitFailure.verify.test.ts · 「上一次提交成功、本次请求失败：默认区域必须含本次原因」 | 失败：看不到本次提交失败的真实原因 | 通过（47ms） |
| d5SubmitFailure.verify.test.ts · 「上一次提交也失败过：默认区域不许拿上一次的旧原因当本次原因」 | 失败：读的是上一次结果 | 通过（8ms） |
| d5LocalWriteFailure.verify.test.ts · 「本机存储写入失败：编辑处要如实说明，并保留输入内容」 | 失败：界面上一句解释都没有 | 通过（79ms） |
| d5SwitchBarGeometry.verify.test.ts · 「切换条要给剩下的面板预留真实高度」 | 失败：面板底边 450px，切换条顶边 408px | 通过（66ms） |

另外，主智能体代为提交的恢复顺序用例 `d5RecoveryOrdering.verify.test.ts`（4 条）在最终提交上也是 4/4 通过：
旧保存迟到不许复活已清除的草稿、慢返回不许覆盖恢复期间的新输入、过期判断只按这张卡片自己的记录、
取回失败原文不触发发送。这 4 条不在基线文档的 12 条清单里（编写时间晚于基线记录），**我没有单独在基线上复跑它们**。

### 9.3 主智能体修的三处驱动缺陷：核对结论

三处改动都只动驱动，**断言一字未改**（`git diff cde0d62^ cde0d62 -- <三个文件>` 逐行看过）。逐条：

1. **`cardLocalDraftKey` → `cardLocalDraftStorageKey`（d5CardDraftRecovery / d5RecoveryOrdering）——只是驱动修正。**
   基线 `git show f436ad8:frontend/src/interactive/drafts.ts` 里 `listLocalCardDraftIds()` 扫的前缀就是
   `draftStorageKey("card", "local-")` = `qio.draft.card.local-`；真实浏览器探针在基线上写下的记录也是
   `qio.draft.card.local-<id>`。我原来的写法把**逻辑键** `card-local:<id>` 当存储键传给 `writeDraft()`，
   写到了产品永远不读的位置——那一条在基线上的失败结论因此**不能只靠单元用例**，只能靠真实浏览器证据；
   §2.1 当时同时给了探针证据（`qio.draft.card.local-c_…` 存在但恢复不到），结论不变。
   **顺带发现（次要）**：`cardLocalDraftKey()`（契约 §11.1 写的本机副本身份 `card-local:<id>`）在最终提交里
   **没有任何产品代码使用**（只有定义与注释），本机记录实际用存储键 `qio.draft.card.local-<id>` 枚举、
   用 `cardIdFromDraftKey()` 两种前缀都认。行为上不影响恢复，属于文档口径与实现姿势不一致 + 死代码。
2. **失败原文从互动板 store 改到会话层 store（d5RecoveryOrdering 场景 12）——只是驱动修正。**
   基线 `frontend/src/stores/session.ts` 就已经有 `failedSend / failedSendError / retryFailedSend /
   swapFailedSendText`；`stores/interactive.ts` 从来没有这些成员（`git show f436ad8:…` 对照）。
   契约 §11.4 要求两个入口共用**会话层**状态，所以原来对互动板 store 调这些动作只会拿到 `undefined`。
   没有掩盖缺陷：互动板 store 里不存在第二套失败事实。
3. **jsdom 里 `getBoundingClientRect().width > 0` 改成「只排除 disabled」（d5ComposerRecovery 的恢复入口查找）
   ——是真实的 jsdom 限制，但它确实削弱了这条断言的强度。**
   jsdom 不做布局，`getBoundingClientRect()` 恒为全 0，原写法让辅助函数**永远找不到恢复入口**（不是产品缺入口）。
   改完后只要 DOM 里有一个不 disabled 的匹配按钮就算过，**「看不见 / 被盖住 / 在收起的详情里」这几种情况
   jsdom 判不出来**。我用真实浏览器把强度补回来：探针场景 4 现在按 `visible + elementFromPoint 命中自己` 判定
   入口，场景 5 真的点这个入口把原文取回来（§9.4）。所以这一处**不是掩盖产品缺陷**，但它把「可见可点」的举证
   责任完全推给了真实浏览器证据——报告里必须这样标注。

### 9.4 真实浏览器（我的探针，最终提交）

`node scripts/interactive-verify/d5-recovery-probe.mjs --app http://127.0.0.1:5471 --backend http://127.0.0.1:8971
--label after --port 9588 --profile %TEMP%\qio-chrome-d2 --visual`（Edge headless + 持久化 profile）。

| 检查点 | 结果 | 证据（原样节选） |
| --- | --- | --- |
| 场景 1 防抖前刷新恢复 | OK | 本机记录 `qio.draft.card.local-…`、刷新后编辑器 =`刷新前刚写的新文字-D5-…` |
| 场景 2 清除后不复活 | OK | 清除同步 `{"ok":true,"has":false,"ms":239}`；刷新后 `serverValueAfterReload=""`、`oldTextRevived=false`（键仍在，见 §9.6 次要 1） |
| 场景 3 本机写入失败可见 | OK | `草稿还没保存成功；本机也没能留下恢复副本：本机存储已满…（现在关闭页面就恢复不到这次未完成的输入） 重试保护` |
| 场景 4 对话页失败原因可见 | OK | 默认区显示本次请求的真实原因（见下） |
| 场景 4 有可见可点的恢复入口 | OK | 入口按 `visible + hitSelf` 判定 |
| 场景 5 刷新后仍能取回原文 | OK | `alreadyInInput=false`，点恢复入口后输入框 =`发送失败的原稿-D5-…` |
| 场景 7 提交失败默认区显示本次原因 | OK | `失败原因：/api/interactive/boards/board_default/submissions -> 500: 模拟的提交失败原因-D5（探针拦截）…`，`detailsOpen=false` |
| 场景 8 切换条有真实定位 | OK | `position: fixed`、`{"t":292,"b":326,"l":81,"r":464}` |
| 场景 8 只开批量列表时切换条不压列表 | OK | `intersectionBatchSwitchBar=0`，列表 `{top:99,bottom:267}` |
| 场景 8 切到聊天后不重叠 | OK | `intersectionChatSwitchBar=0`，`chatBottomVsBarTop=-12` |
| 场景 8 关掉一个面板后有回去的路 | OK | 关闭最后一个面板后切换条隐藏（设计如此），两个面板入口可见可点 |
| **场景 8/9 480px 聊天输入区可点** | **FAIL ×3** | 见 §9.6 重要 1 |
| 场景 9 800×600 长失败原文下输入区可点 | OK | 面板 `h=335`，输入框 `hitSelf=true`、发送按钮 `hitSelf=true` |
| 关闭重开（新浏览器进程、同一 profile） | OK | 应用重新起得来 |

**这一次「注入的 500 详情」真的到了界面**：修掉探针自己的两个缺陷（应答没有 CORS 头、把 `OPTIONS` 预检也拦成
500）之后，默认失败区显示的是
`/api/turns -> 500: {"detail":"模拟的发送失败原因-D5（探针拦截）：请求没有到达服务端，诊断信息：连接被重置，这段文字还在这台机器上。"}（输入已保留，可以重试）`
——【模拟】失败注入，但「取本次请求的原因并默认显示」是应用自己的行为。截图：
`docs/interactive-ui-screenshots/r5-d-diag-09b-chat-composer-with-failure.png`。

### 9.5 我新增的反例（4 个文件 11 条，全部通过）

| 文件 | 条数 | 覆盖 | 结果 |
| --- | --- | --- | --- |
| `frontend/src/stores/__tests__/d5bCardsIsolation.verify.test.ts` | 2 | §11.1/§11.2：A 卡只有本机记录 + B 卡只有服务器草稿互不覆盖；清除 A 不影响 B（含刷新后） | 通过 |
| `frontend/src/stores/__tests__/d5bFailedSendPersist.verify.test.ts` | 3 | §11.4/§11.5：刷新后失败原文与新草稿同时保留且取回不覆盖；未绑定→绑定→成功只清那一版；迁移时失败归到真实话题 | 通过 |
| `frontend/src/components/interactive/__tests__/d5bSwitchBarGeometry.verify.test.ts` | 3 | §11.7：480×600 纯几何（切换条在视口内、两面板最坏矩形不压条、关掉面板不改变布局状态）；只开批量列表时 `--im-geo-batch-max-h` 也要让开切换条 | 通过 |
| `frontend/src/components/interactive/__tests__/d5bSubmitReason.verify.test.ts` | 3 | §11.6：板面保存也失败时不许说「已保存的板面都保留」；长原因完整显示且无「接口」类实现用语；重试成功后旧原因不残留 | 通过 |

**非空洞性说明（如实）**：这些反例是照着**最终实现**的接口面写的（`writeCardLocalDraft`、`readCardLocalDraft`、
`failedSendsForTopic`、`--im-geo-batch-max-h`）。其中 `cardLocalDraftStorageKey`、`writeCardLocalDraft`、
`readCardLocalDraft`、`failedSendsForTopic` 在基线 `f436ad8` 上**根本不存在**，把它们拿到基线上跑只会得到
「函数不存在」型失败（基线文档明确说不算缺陷），所以**我没有在基线上复跑这批新用例**；它们的价值是在最终
提交上补覆盖，并作为后续回归的护栏。另外我在 `d5bFailedSendPersist` 里补了
`api.sendTurn` 调用次数断言（取回/互换不许再发一次请求）。

### 9.6 缺陷清单（最终提交）

#### 重要

1. **480px 下聊天面板的输入区被面板自己裁掉（§11.7「输入区与主要操作可见可点」/ §11.8「原文较长时限高
   滚动，不挤掉输入框与发送按钮」）**。
   - 复现（真实浏览器，480×600）：打开互动板 → 打开聊天面板 → 量输入框。
     * 干净状态下（本机失败事实与草稿都清掉）就已经复现：面板 `{top:65,bottom:280,h:215}`，
       输入框 `{top:352,bottom:410}` —— **整条输入区落在面板盒子下方 72px**，被
       `.panel{ overflow: hidden }` 裁掉；`elementFromPoint` 在输入框中心命中 `DIV`、在发送按钮中心命中
       `chat-toggle`。`hitSelf=false`。
     * 有长失败原文时同样（输入框 `{top:247,bottom:379}`，中心命中 `overlay-switch-notice`）。
   - 机制证据（探针的祖先链诊断）：`SECTION.panel { height:215px, maxHeight:215px, overflow:hidden, display:flex }`；
     输入框 `TEXTAREA.input { flex:1 1 0%, minHeight:40px, maxHeight:132px }`。失败通知（本轮新增组件）与
     其它 flex 子项高度之和超过面板高度时，后面的 `input-row` 被排到面板盒子之外并被裁掉。
   - 基线对照（同一 profile、同样 480×600、聊天面板展开）：基线探针
     `d5-baseline-probe.json` 里 `chatInput.hitSelf=true`、`chatSend.hitSelf=true`（面板 `h=259`）。
     最终提交把面板压到 215px（本轮新增的切换条预留/抬升），**这是本轮引入的回归**。
   - 范围：**800×600（桌面最小窗口）正常**——同样有长失败原文时面板 `h=335`，输入框与发送按钮 `hitSelf=true`；
     问题只出现在 480px 这条契约明确要求检查的边缘宽度上。所以判**重要**，不判阻断。
   - 截图：`r5-d-diag-09b-chat-composer-with-failure.png`（480×600：面板里只有失败通知，**看不到输入框与发送按钮**）、
     `r5-d-after-09a-chat-composer-clean.png`、`r5-d-after-09c-chat-composer-with-failure-800x600.png`（对照：正常）。
   - 建议方向（仅供参考，未改产品代码）：面板高度不够时让失败恢复区参与收缩（`min-height:0` + 内部滚动），
     或给 `input-row` 一个「不被挤出」的约束，或在切换显示状态下重新核算面板最小高度。

#### 次要

2. **用户确认编辑（清除草稿）并撤销之后，重新打开一次编辑器，会在服务端留下一条「正文为空」的草稿记录**
   （`card:<id>: ""`，记录存在）。证据：探针场景 2 `draftKeyExistsAfterReload=true`、`serverValueAfterReload=""`、
   `oldTextRevived=false` —— 旧文字**没有**复活（§11.2 的主诉已修好），但清除掉的记录在"存在性"意义上又出现了一条。
   复现：新建空文字卡 → 编辑输入 → 完成编辑 → 撤销 → 刷新 → 打开编辑器（此时正式正文为空，应用按设计写下空草稿）。
   影响：不丢字；但下一次打开这张卡片时会以「存在但为空」的草稿覆盖正文（正文非空时用户会看到空编辑框，
   这正是 §10.4 想避免的一类状态）。建议产品明确「打开编辑器是否应该立刻写一份草稿」。
3. **`cardLocalDraftKey()` 是死代码**，契约 §11.1 写的逻辑键 `card-local:<id>` 在实现里不存在（见 §9.3.1）。行为不受影响。

（缺陷 1 的三条探针检查点是同一件事，不重复计数。）

### 9.7 我自己的脚本缺陷（与产品缺陷分开写，全部已修并留下证据）

1. 恢复入口只按 `<button>` 找、只认「找回/取回」文案 → 失败后原文已自动放回输入框时，「找回原文」按钮按设计不出现，
   被误报成「没有恢复入口」。**已修**：收集全部交互元素（`button,a,[role=button],[tabindex]`）并记录
   `visible`/`hitSelf`。
2. 拦截应答没有 CORS 头，并且把 `OPTIONS` 预检也拦成 500 → 浏览器按 CORS 失败处理，应用只能看到
   `Failed to fetch`，注入的 500 详情永远到不了界面，原因断言因此永远不可能通过。**已修**：应答带
   `Access-Control-Allow-*`（回显页面 origin）并放行预检。
3. 场景 3 的 `Page.addScriptToEvaluateOnNewDocument`（`qio.draft.*` 写入失败）**没有撤销** → 场景 4/5 是在
   「本机草稿根本写不进去」的环境里跑的，「刷新后输入框为空」被误判成产品问题。**已修**：场景 3 结束时
   `removeScriptToEvaluateOnNewDocument`，并写进 notes。
4. 场景 2 用「最后一张卡片」定位、用**没有命中测试**的按文案鼠标点击、固定等 1.6 秒 → 清除请求可能压根没发出去
   或被判早了。**已修**：按「编辑器所属卡片」定位、点它自己的「完成编辑」（先命中测试，点不到才退回 DOM 点击）、
   轮询服务端最多 6 秒。修完后场景 2 通过，且 `clearedOnServer.ms=239` 证明清除请求确实发出并被服务器确认。
5. 场景 2 只按「键还在不在」判断「旧草稿复活」→ 把「重新打开编辑器写下的空草稿」也算成复活。**已修**：按**值**判断。
6. 场景 5 要求「刷新后输入框里必须就是原文」→ 草稿恢复成功时原文本来就在输入框里、恢复按钮按设计不出现。**已修**：
   「已经在输入框里」与「点入口取回」两条路径都算通过，各自记录。
7. 探针修好之前的**第一轮**原始输出留在 `docs/interactive-ui-screenshots/d5-after-probe-firstrun-superseded.json`
   （6 条失败，其中场景 2/4/5/7 都是上面这几种驱动缺陷造成的假失败，场景 8 的两条是真实缺陷证据的一部分）。
8. 视觉矩阵的板面数据种子（真实接口 PUT）会被并发写入盖掉（第一次跑到截图时板面只剩 3 张卡片）→ **已修**：
   进矩阵前先数一次卡片，不足 6 张就用界面自己的「添加 → 文字」补卡片；最终一轮种子生效（9 张卡片 / 3 条关系线）。

### 9.8 视觉矩阵（`docs/interactive-ui-screenshots/r5-d2-*`，14 张）

| 文件 | 视口 | 主题 | 状态 |
| --- | --- | --- | --- |
| `r5-d2-visual-1440x900-{light,dark}-chat.png` | 1440×900 | 明/暗 | 互动板 + 聊天展开，背后 9 张卡片 / 3 条关系线 |
| `r5-d2-visual-1024x768-{light,dark}-chat.png` | 1024×768 | 明/暗 | 同上 |
| `r5-d2-visual-800x600-{light,dark}-chat.png` | 800×600 | 明/暗 | 桌面最小窗口（此处输入区正常，见 §9.4） |
| `r5-d2-visual-1024x768-{light,dark}-batch-dense.png` | 1024×768 | 明/暗 | 批量列表 + 密集板面 |
| `r5-d2-visual-480x600-{light,dark}-switch-bar.png` | 480×600 | 明/暗 | 切换条 + 批量列表 |
| `r5-d2-visual-1024x768-{light,dark}-long-failure-text.png` | 1024×768 | 明/暗 | 对话页长失败原文（限高滚动，输入框与发送按钮都在） |
| `r5-d2-visual-1440x900-{light,dark}-long-submit-reason.png` | 1440×900 | 明/暗 | 长提交原因完整换行、详情未展开 |

【模拟】说明：明暗通过 `html[data-theme]` + `localStorage(qio-theme)` 切换；密集卡片与关系线用**真实接口
PUT 板面状态**做数据种子；长失败原文与长提交原因用请求拦截制造。界面本身全部由应用自己渲染。
功能证据截图另有 `r5-d-after-01…08`、`r5-d-after-09a/b/c`（探针运行时写入）。

### 9.9 没能验证清单（本轮）

1. **真实 QIO**：没有模型凭据，发送/提交的「失败」都是拦截制造的【模拟】；不伪造 QIO 回复。
2. **服务器 500 详情在生产同源部署下的表现**：我验证的是 dev 跨源实例（5471 → 8971）。修掉预检拦截后应用能
   显示注入的详情；生产同源部署下的同一路径没有单独验证。
3. **缺陷 1 的修复后复验**：我没有改产品代码，所以这条缺陷只有失败证据，没有修复后的通过证据。
4. **480px 之外的非整数/更窄宽度**（例如 360px）：没有覆盖。
5. **关系线的交互**（建链、改含义、删除关系）：本轮只作为视觉背景（种子数据），没有驱动它的交互流程。
6. **事件缓冲区负载敏感用例的稳定性统计**：全量只跑了一次（135 文件 / 1360 用例全绿）；后端
   `test_proc_list_ok` 在并发负载下出现过一次 `TimeoutError`，隔离重跑通过，没有做重复次数统计。
7. **强制结束 / 崩溃恢复**：不在本轮范围（与契约 §10.5 的边界一致）。

### 9.10 边界（本轮补充）

- 我没有修改任何产品代码；本节的所有变更只包含：我新增的 4 个测试文件、我的探针脚本
  `scripts/interactive-verify/d5-recovery-probe.mjs`、我的报告与截图。
- 没有合并 main、没有改数据库与迁移、没有接真实 QIO。
- 探针的失败注入（`/api/turns`、`/submissions` 的 500）、本机存储注入、板面数据种子都明确标注为【模拟】，
  被验证的是**应用自己的处理流程**。


