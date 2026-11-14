# task-7 · 更新文案 b：按失败阶段显示动作

> 执行者：frontend-updater2（子智能体）。worktree：`D:\qio-dev\pm-qio032-integration`，分支 `fix/pm-qio032-integration`，起点 HEAD `acdb671c`。
> 时间：2026-11-14 12:17 – 13:45（Asia/Shanghai）。
> 纪律：只做本地定向验证；未触发 CI、未跑全量 pytest / 全量 npm test、未 git push / 未开 PR / 未合并 / 未打标签、**未 git commit**（Lead 统一按问题拆分提交）。
> 未改 `.github/workflows`、`docs/status.md`、迁移文件、后端；未碰 `stores/interactive.ts`、`conversationPreview.ts`。

## 0. 一屏结论

| 场景 | 主界面（改后） | 主按钮 | 定向验证 |
| --- | --- | --- | --- |
| 仅检查更新失败 | 「检查更新失败」+ 原因「更新源请求失败…」+「可以点「重试」重新检查；更新设置与当前版本不受影响。」 | 「重试」→ 重新检查 | 组件 3 例 + 浏览器 A/B 共 9 项 |
| 下载 / 校验失败 | 「下载更新失败」+ 原因「更新包校验未通过…」+「可以点「重试下载安装」重新下载并安装。」 | 「重试下载安装」→ 重新下载安装 | 组件 2 例 + 浏览器 C 共 4 项 |
| 安装失败、应用已恢复 | 「安装更新失败」+「应用已恢复运行，可以继续使用。更新没有安装，可以点「重试下载安装」重新下载安装。」 | 「重试下载安装」 | 组件 1 例 + 浏览器 D 共 3 项 |
| 安装失败、后端未恢复 | 「安装更新失败」+「更新没有装上，且后端未能自动恢复。请完全退出 QIO 再重新启动；重启后可在设置里重新检查更新。」 | 「重新检查更新」（不假装能重试安装） | 组件 2 例 + 浏览器 E 共 5 项 |

**关键修复**：失败阶段与重试动作变成**结构化事实**（`failureStage` / `retryAction`），不再由卡片从 message 猜；
检查失败不再出现「更新没有安装」「完全退出 QIO」「Can't write …qio-backend.exe」这类安装失败说明。

定向：**12 个文件 / 144 条用例全绿**；`npx --no-install vue-tsc --noEmit` **EXIT=0**；真实浏览器（Edge + CDP）**PASS 21 / FAIL 0**。

## 1. 改动文件清单

### 1.1 产品源码（3 个，全部在我的写入范围内）

| 文件 | 改动 |
| --- | --- |
| `frontend/src/services/updater.ts` | 新增 `UpdateFailureStage`（check / download / install）与 `UpdateRetryAction`（check / install / none）；`UpdateFailure` 增加 `stage` / `retry`；`UpdateFailureError` 可携带 `stage` / `retry`；新增 `retryActionFor(stage, recovery)`；`describeUpdateError(error, stage = "check")` 按阶段产出结构化事实（`UpdateFailureError` 未标阶段时用 recovery 兜底推断 install）；下载前「更新源没有新版本 / 版本已变化」标 `stage: "check", retry: "check"`；安装失败标 `stage: "install"` |
| `frontend/src/stores/updater.ts` | state 新增 `failureStage` / `retryAction`；`_beginOperation` / `_cancelAutoCheckOperation` 一并保存与回滚；`check()` 失败按 `"check"` 阶段、`download()` 失败按 `"download"` 阶段（由 services 标注的真实阶段覆盖）写入；新增 `retry()`：按 `retryAction` 走「重新检查 / 重新下载安装 / 什么都不做」，单飞保护，没有可安装版本时退回重新检查 |
| `frontend/src/components/UpdateCard.vue` | 失败主行按 `failureStage` 说「检查更新失败 / 下载更新失败 / 安装更新失败」；新增 `[data-update-reason]` 原因行（安装失败不重复，避免与下一步同义复述）；下一步按 `recovery` + `retryAction` 四分支给真实动作；新增 `[data-action="retry-install"]`「重试下载安装」按钮；未恢复时主按钮改「重新检查更新」且只做重新检查；`<details data-update-diagnostics>` 保持默认折叠 |

### 1.2 测试（4 个：1 新增 + 3 修改）

| 文件 | 改动 |
| --- | --- |
| `components/__tests__/UpdateCard-failure-stage.test.ts`（**新增**，8 例） | 三种失败状态的文案 + 按钮行为 + 诊断默认折叠：检查失败（主行/原因/下一步/无安装说明、点重试真的重新检查、诊断折叠）、下载失败（主行/原因/重试下载安装、点它真的重新下载安装）、安装失败已恢复（可以继续使用 + 重试下载安装 + 不进全局异常）、安装失败未恢复（完全退出再启动 + 进全局异常 + 按钮只做重新检查） |
| `components/__tests__/UpdateCard.test.ts` | 检查失败 / 下载失败 / 安装失败（已恢复、未恢复）用例改为断言「阶段行 + 原因行 + 下一步」；「检查失败与安装失败下一步不同」补反断言（检查失败不出现「安装」「Can't write」） |
| `stores/__tests__/updater.test.ts` | 检查失败补 `failureStage` / `retryAction` 断言；新增 5 例：下载失败 → download/install、安装失败已恢复 → install/install、安装失败未恢复 → install/none、`retry()` 按动作走对应操作、`retryAction=none` 时 `retry()` 什么都不做 |
| `services/__tests__/rm-e-updater.spec.ts` | 三条断言从「错误 message 里含原始原因」改为结构化契约（`failure.stage` / `failure.recovery` / `failure.detail`）；顺带修正一处「两次独立操作」的计数（原断言 `closeCalls === 2` 依赖两次调用，改为单次操作 + 结构化断言） |

## 2. 先写反例（红）再修

新增测试先跑，确认失败原因就是本任务要修的问题（`updater2-red-proof.log`）：

```
 × 主行说「检查更新失败」，下一步是重试重新检查
   → expected '更新源请求失败：可能是网络或代理问题' to be '检查更新失败'
 × 主行说「下载更新失败」，下一步给重试下载安装，不说完全退出
   → expected '更新包校验未通过，已放弃安装（不会安装未经签名的包）' to be '下载更新失败'
 × 点「重试下载安装」真的重新下载安装（不重新检查）
   → Cannot call trigger on an empty DOMWrapper.        ← 根本没有这个按钮
 × 主行说「安装更新失败」，下一步说可以继续使用并重试下载安装
   → expected '更新没有安装成功；应用已恢复运行，可以重试' to be '安装更新失败'
 × 主行说「安装更新失败」，下一步说完全退出再启动，并进全局异常入口
   → expected '更新没有安装成功，且后端未能自动恢复' to be '安装更新失败'
 × 未恢复时不给「重试下载安装」：按钮只做它真正会做的事（重新检查）
   → expected '重试' to be '重新检查更新'
 Test Files  1 failed (1)
      Tests  6 failed | 2 passed (8)
```

复现方式（`updater2-red-proof.log` 就是这么生成的）：把三个产品文件临时 `git checkout HEAD --` 回基线，
只跑新增测试文件，再把工作区改动原样恢复（脚本用文件哈希前后比对确认恢复无损，输出 `RESTORED=True`）。

浏览器侧的红（修复前的真实界面，来自 `_lead-logs/pm-qio032/integration-032/browser-copy.log` c3-1）：
只点「检查更新」后主界面是「更新失败 / 更新没有安装。可以点「重试」重新检查；若安装被中止（例如 "Can't write …qio-backend.exe"），请完全退出 QIO 后重试安装。」
—— 检查失败却给了安装失败说明，正是本任务要修的现象。

## 3. 跑过的命令与结果（逐条）

### 3.1 定向 vitest（12 文件，最终一轮）

~~~powershell
cd D:\qio-dev\pm-qio032-integration\frontend
npx --no-install vitest run `
  src/components/__tests__/UpdateCard.test.ts `
  src/components/__tests__/uf-updater-stages.test.ts `
  src/components/__tests__/UpdateCard-failure-stage.test.ts `
  src/components/__tests__/uf-window-chrome-close.test.ts `
  src/stores/__tests__/updater.test.ts `
  src/stores/__tests__/updaterTimers.test.ts `
  src/services/__tests__/updaterFlow.test.ts `
  src/services/__tests__/rm-e-updater.spec.ts `
  src/views/settings/__tests__/settings-content.test.ts `
  src/views/settings/__tests__/settings-shell.test.ts `
  src/views/settings/__tests__/settings-reset-layout.test.ts `
  src/views/settings/__tests__/ReleaseStep3Settings.test.ts
~~~

结果（完整输出见 `updater2-vitest.log`）：

```
 ✓ src/services/__tests__/rm-e-updater.spec.ts (11 tests)
 ✓ src/stores/__tests__/updater.test.ts (17 tests)
 ✓ src/services/__tests__/updaterFlow.test.ts (10 tests)
 ✓ src/components/__tests__/UpdateCard-failure-stage.test.ts (8 tests)
 ✓ src/stores/__tests__/updaterTimers.test.ts (9 tests)
 ✓ src/components/__tests__/UpdateCard.test.ts (15 tests)
 ✓ src/components/__tests__/uf-updater-stages.test.ts (8 tests)
 ✓ src/components/__tests__/uf-window-chrome-close.test.ts (7 tests)
 ✓ src/views/settings/__tests__/settings-content.test.ts (23 tests)
 ✓ src/views/settings/__tests__/settings-shell.test.ts (7 tests)
 ✓ src/views/settings/__tests__/settings-reset-layout.test.ts (7 tests)
 ✓ src/views/settings/__tests__/ReleaseStep3Settings.test.ts (22 tests)

 Test Files  12 passed (12)
      Tests  144 passed (144)
EXIT=0
```

### 3.2 类型检查

~~~powershell
cd D:\qio-dev\pm-qio032-integration\frontend
npx --no-install vue-tsc --noEmit
~~~

结果：**EXIT=0，无输出**（`updater2-tsc.log` 为空文件）。
（说明：本轮其它成员并发改动期间曾出现过 `stores/interactive.ts` / `pm032-board-save-isolation.test.ts` 的 tsc 报错，那是他们的文件；我最后一次跑时全仓 tsc 已干净。）

### 3.3 真实浏览器（Edge + CDP）

~~~powershell
cd D:\qio-dev\pm-qio032-integration
# 起隔离数据域的后端 + vite（scripts/e2e_up.py，QIO_DATA_DIR 指向 %TEMP% 下的一次性目录）
& backend\.venv\Scripts\python.exe scripts\e2e_up.py
node _lead-logs\pm-qio032\integration-032\updater2-browser.mjs
& backend\.venv\Scripts\python.exe scripts\e2e_down.py
~~~

结果（完整输出见 `updater2-browser.log` / `updater2-browser-summary.json`，截图见 `shots/`）：

```
== 结果：PASS 21 / FAIL 0 / TOTAL 21 ==
```

逐项（节选关键行）：

```
[PASS] A-1 主行只说「检查更新失败」 :: {"failure":"检查更新失败","next":"可以点「重试」重新检查；更新设置与当前版本不受影响。",...}
[PASS] A-2 失败块不出现安装失败 / 完全退出 / 文件写入示例 :: {"install":false,"quit":false,"write":false}
[PASS] A-4 诊断默认折叠且含原始错误 :: {"open":false,...}
[PASS] B-1 主行「检查更新失败」+ 原因「更新源请求失败」 :: {"failure":"检查更新失败","reason":"更新源请求失败：可能是网络或代理问题"}
[PASS] B-3 诊断默认折叠，原始错误只在里面 :: {"open":false,"diagBody":{"lines":["原始错误：failed to fetch: network unreachable",...]}}
[PASS] B-4 点「重试」真的重新检查（check 调用 +1，没有下载安装） :: {"beforeCheck":1,"afterCheck":2,"calls":{"check":2}}
[PASS] C-1 主行「下载更新失败」+ 原因「更新包校验未通过」 :: {"failure":"下载更新失败","reason":"更新包校验未通过，已放弃安装（不会安装未经签名的包）"}
[PASS] C-3 主按钮是「重试下载安装」（不是重新检查） :: {"retryInstall":"重试下载安装","checkButton":null}
[PASS] C-4 点「重试下载安装」真的重新下载安装（downloadAndInstall +1） :: {"beforeDl":1,"afterDl":2}
[PASS] D-2 下一步说「应用已恢复运行，可以继续使用」并给重试下载安装
[PASS] E-2 下一步说「完全退出 QIO 再重新启动」，不说可以继续使用
[PASS] E-3 没有「重试下载安装」按钮；主按钮只做它真正会做的事（重新检查更新） :: {"retryInstall":null,"checkButton":"重新检查更新"}
[PASS] E-4 进全局异常入口（store 里有 updater_install_failed，带原因与诊断）
[PASS] E-5 对话页顶部栏出现「问题」入口 :: {"entry":{"text":"问题\n1","label":"1 个独立问题（1 个需要处理），打开问题列表（每个问题都有原因与处理方式）"}}
```

截图（`_lead-logs/pm-qio032/integration-032/shots/`，共 6 张）：
`01-A-check-failed-real-browser.png`（真实浏览器只点检查更新）、`02-B-check-failed-simulated.png`、
`03-C-download-failed.png`、`04-D-install-failed-restored.png`、`05-E-install-failed-unrecovered.png`、
`06-E-global-issue-entry.png`。

最终一轮 runId=`20261114054908`（`updater2-browser-summary.json`）：**PASS 21 / FAIL 0 / TOTAL 21**。
驱动脚本 `updater2-browser.mjs` 只读（不改产品代码），跑完已 `scripts/e2e_down.py` 停掉后端与 vite，
并删除一次性数据域目录。

### 3.4 补丁卫生

~~~powershell
git diff --check -- frontend/src/services/updater.ts frontend/src/stores/updater.ts frontend/src/components/UpdateCard.vue frontend/src/components/__tests__/UpdateCard.test.ts frontend/src/stores/__tests__/updater.test.ts frontend/src/services/__tests__/rm-e-updater.spec.ts
~~~

结果：**EXIT=0**（无空白错误）；新增测试文件无行尾空白、以 LF 结尾。

### 3.5 未跑（按纪律）

- 未跑全量 `npm test` / 全量 `vitest run`；
- 未跑任何后端 pytest（本任务未改后端）；
- 未触发 CI、未 push、未 commit。

## 4. 验收对照

| 任务要求 | 落地 | 验证 |
| --- | --- | --- |
| 用现有操作状态或明确的失败阶段区分提示 | `failureStage`（check / download / install）+ `retryAction`（check / install / none）是 store 的结构化事实，由真实操作路径写入 | store 5 例 + 组件 8 例 + 浏览器 B/C/D/E |
| 重试动作必须与界面所说的操作一致 | 检查失败 → `retry()` 走 `check()`；下载/安装失败 → 走「回到 available 再 download()」；未恢复 → 按钮改「重新检查更新」且只重新检查 | 「点重试真的重新检查」「点重试下载安装真的重新下载安装」「未恢复时按钮只做重新检查」+ 浏览器 B-4 / C-4 / E-3 |
| 仅检查失败：显示「检查更新失败」，提供「重试」并重新检查；**不出现**安装失败、退出应用或文件写入示例 | 主行「检查更新失败」+ 下一步「可以点「重试」重新检查；更新设置与当前版本不受影响。」 | 组件反断言（不含「安装」「完全退出」「Can't write」「qio-backend.exe」「更新没有安装」）+ 浏览器 A-2 / B-2 |
| 下载或安装失败、应用仍可用：显示实际失败阶段和可执行的重试动作；确实恢复时才提示可以继续使用 | 下载失败 → 「下载更新失败」+「重试下载安装」；安装失败已恢复 → 「安装更新失败」+「应用已恢复运行，可以继续使用」+「重试下载安装」 | 组件 3 例 + 浏览器 C/D |
| 安装失败且应用未能恢复：提示完全退出后重新打开 QIO，按现有流程提供可执行操作 | 「更新没有装上，且后端未能自动恢复。请完全退出 QIO 再重新启动；重启后可在设置里重新检查更新。」+ 全局异常入口 | 组件 2 例 + 浏览器 E-2 / E-4 / E-5 |
| 具体错误与路径继续放在默认折叠的诊断里 | `<details data-update-diagnostics>` 保持默认折叠，含「原始错误 / 代理 / 日志」 | 组件 3 处 `open` 断言 + 浏览器 A-4 / B-3 / D-3 |
| 组件测试覆盖三种失败状态的提示文案、按钮行为、诊断默认折叠 | `UpdateCard-failure-stage.test.ts` 8 例 | 见 3.1 |
| 浏览器故障模拟验证记录界面行为 | `updater2-browser.mjs`（Edge + CDP），A 为真实浏览器路径，B–E 用注入的 `UpdaterApi` 替身驱动真实组件 | 见 3.3 |

## 5. 仍未验证的边界（如实）

1. **Windows 真机安装 / 重开时序未验证**：本任务只改界面与前端编排；`qio_prepare_for_update` / `qio_restore_backend` / 安装器在真实壳里的行为没有跑（浏览器里没有 Tauri 更新器）。B–E 的更新器是**注入的替身**，验证的是「界面按阶段说话 + 按钮触发对应操作」，不证明真实安装成功/失败时序。
2. **真实 Tauri 壳里的检查失败路径未跑**：A 是真实浏览器路径（无更新器 → 真实失败），但真实壳里 `plugin:updater|check` 的网络失败没有实测。
3. **未跑全量前端测试**：只跑了 12 个定向文件；本轮其它成员并发改了 `stores/interactive.ts` / `conversationPreview.ts`，它们的用例由各自负责人跑。
4. **`rm-e-updater.spec.ts` 的断言口径变了**：三条断言从「错误 message 里含原始原因」改为结构化 `detail` / `recovery` / `stage`。这是 task-4（文案清理）把 message 与 detail 拆开后的必然结果 —— 已用临时探针确认（探针跑完即删，未入库）：
   - 把 `551a2e99`（文案清理前）的 `services/updater.ts` 单独取出跑同样三条断言：**3 passed**（message 里确实拼了原始原因）；
   - 把 `HEAD（acdb671c）` 的 `services/updater.ts` 单独取出跑同样三条断言：**3 failed**（message 已不再拼原始原因）。
   也就是说这三条红不是本任务引入的，本任务把它们改成与当前契约一致的结构化断言。
5. **`retry()` 的「重新下载安装」会重新走一次 `check()`**：这是 `downloadAndInstall` 现有编排（下载前重新取更新源、校验版本一致）决定的，不是新增行为；浏览器 C-4 记录到 `check:1, downloadAndInstall:2`（第一次下载前的 check 在 `before` 里已计入）。

## 6. 与其它成员的交叉

- 执行期间 `frontend-preview`（task-6）在改 `stores/conversationPreview.ts`、`frontend-46` 在改 `stores/interactive.ts`、`backend-28` 在改 `backend/src/agent/tools/attachment_tools.py`。我**未修改**他们的任何文件，也未在他们的文件上跑验证。
- 我改的 3 个产品文件与 4 个测试文件都在 task-7 的写入范围内。
