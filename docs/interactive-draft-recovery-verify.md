# 草稿恢复补齐与失败处理 · 独立验收报告（第四轮 · 子智能体 D）

> 验收人：独立验收子智能体 D（工作区 qio-recover-d，分支 wt/rec-d-verify）
> 基线：f436ad8。**本报告只写我自己的用例、探针、截图与结论；不改任何产品代码。**
> 证据分级：【状态/单元】【组件/DOM】【真实浏览器】【真实请求】【视觉】【模拟】【没能验证】。

## 0. 摘要（截至基线验收阶段）

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

