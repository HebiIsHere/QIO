# 真实端到端验收记录（Agent G · 前端 / 打包 / 真机 E2E）

分支：`wt/agent-g-frontend-e2e`（基于 main `fbb350a`）
日期：2026-10-02
验证环境：Windows 11 + 本机 Chrome（headless CDP）、真实后端（uvicorn + SSE）、
真实生产构建（`npm run build` + `vite preview`）、离线假厂商服务
（`scripts/e2e_fake_provider.py`）。**全程不需要真实 API Key、不联网。**

---

## 0. 本机环境的硬限制（决定了哪些必须人工做）

本次会话的沙箱只允许**在工作树内**写文件。实测（`backend/.venv/Scripts/python.exe`）：

| 路径 / 操作 | 结果 |
| --- | --- |
| `D:\QIO-e2e-g`（新建目录 + 写文件） | `PermissionError [WinError 5]` |
| `%TEMP%\qio-wt-g-probe` | `PermissionError [WinError 5]` |
| `HKCU\Software\qio\probe`（winreg 写） | `PermissionError [WinError 5]` |
| `<工作树>\.e2e-scratch\sandbox-probe` | 可写 |

后果（都实测过，不是推测）：

* `scripts/e2e_up.py` 默认数据目录 `%TEMP%\qio-e2e` 在本机不可写 →
  后端启动报 `sqlite3.OperationalError: attempt to write a readonly database`。
  改用 `QIO_DATA_DIR=<工作树>/.e2e-data` 后正常。
* 数据库身份自检要写 `HKCU\Software\qio\QIO` → `PermissionError`；
  用 `QIO_DB_BASELINE=<工作树>/.e2e-data/db-baseline.json` 走文件实现后正常。
* `npm run build` 失败：`esbuild ... remove %TEMP%\esbuild-xxx: Access is denied`；
  把 `TEMP/TMP` 指到工作树内的 `.tmp` 后构建成功。
* **NSIS 安装包跑不起来**：`QIO_0.1.10_x64-setup.exe /S /D=D:\QIO-e2e-g`
  8 秒后 `exit=2`，目标目录与注册表项都没有产生 —— 安装器必须写工作树以外的
  目录与 HKCU，本会话一律被拒。这不是 QIO 的缺陷，是会话沙箱的限制。

因此本记录把「可离线判定」的部分做成脚本与实测证据，把「必须真机人工」的部分
明确列在第 5 节，不混在一起。

---

## 1. 离线发布闸门（G1 的可自动化部分）

脚本：`scripts/release_gate.py`（纯标准库、不联网、不安装），自检：
`python scripts/release_gate.py --selftest` → `[PASS] 发布闸门自检通过：健康产物全绿，哈希不符 / 后端过期 都能变红`。

真实运行（真实发布产物）：

```
python scripts/release_gate.py --repo "<main 检出>"
```

| 检查项 | 结果 | 说明 |
| --- | --- | --- |
| installer | PASS | QIO_0.1.10_x64-setup.exe（108.2 MB） |
| installer.sha256 | PASS | 26434967d0bdc673… |
| sha256sums | PASS | 与 dist/SHA256SUMS.txt 一致 |
| latest.json.bom / version / url | PASS | 无 BOM、0.1.10 与 tauri.conf.json 一致、url 指向同一文件 |
| latest.json.signature / installer.sig | PASS | 结构合法（alg=ED，key id=5af7e96c8e24bf8b 与内嵌公钥一致） |
| nsis.payload | PASS | NSIS 标记 + 产品名 + 版本号（UTF-16LE）都在 |
| sidecar | PASS | qio-backend-x86_64-pc-windows-msvc.exe（56.9 MB，2026-10-02 00:48） |
| **sidecar.fresh** | **FAIL** | **安装包（2026-09-29 17:11）比 sidecar（2026-10-02 00:48）旧：dist 里这个包包含的是更早构建的后端，必须重新打包再发布** |
| models | PASS | bge-small-zh-v1.5 的文件与 model_manifest.json 的 bytes+sha256 逐一对上 |
| bundle.config | PASS | externalBin / resources / createUpdaterArtifacts 自洽 |

边界（脚本里也写明了）：ed25519 验签需要非标准库实现（本机 backend venv 无
`cryptography`），所以签名只做**结构 + key id 归属**核对，不能当作"验签通过"。

---

## 2. 凭据改地址：旧 Key 不会在保存前发到新地址（G2）

真实链路：生产构建的界面 + 真实后端 + 两个离线假厂商端点
（E1 = `http://127.0.0.1:8799/v1`，E2 = `http://127.0.0.1:8798/v1`，
脚本 `scripts/e2e_fake_provider.py`，只记密钥的 sha256 指纹）。

1. 界面新建凭据（厂商「其他 / 自定义服务」、Key `sk-e2e-old-KEY-0001`、地址 E1）
   → E1 收到 `GET /v1/models` + `POST /v1/chat/completions`，指纹
   `e365e3f6a649f909`（= 旧 Key），保存成功。
2. 编辑该凭据，把地址改成 E2、**不填新 Key**：
   * 界面提示「地址和这把 Key 原来的地址不一致，这里不会自动去取模型列表」；
   * 点保存被挡下：「请填写 API Key」「你改了服务地址或连接协议…请勾选确认后再保存」；
   * E2 收到请求数 = 0。
3. 勾选确认 + 填新 Key `sk-e2e-new-KEY-0002` 保存 → E2 **只**收到 1 次
   `chat/completions`，指纹 `f60ac0d53a23b105`（= 新 Key）；
   E2 的日志里 `contains_old_fp=False`。
4. 绕开界面的服务端核对（直接打 API）：
   * `PATCH /api/credentials/<id>` 改地址到 E1、不带 secret →
     `400 {"detail": "changing endpoint is a credential reconfiguration: re-enter the secret and pass confirm_reconfigure=true"}`，
     且 E1 请求数不变（2 → 2）；
   * 对照组：地址不变时 `PATCH` 返回 200（证明拒绝来自"改了发送目标"，不是路径坏了）。

结论：**保存前不会把旧 Key 发到新地址**，且这条约束由服务端强制，不依赖界面自觉。

---

## 3. 审批成功出队（G3）

真实链路：假模型发一次 `run_shell` 调用 → 后端产生 pending approval。

* **不抢焦点**：用户正在输入框里打字时，审批窗口**不自动弹出**，改为亮出
  「有 1 项操作等待确认」入口；此时 `document.activeElement` 仍是
  `TEXTAREA#composer-input`（实测采样）。
* **打开**：点入口后对话框获得焦点，`role=dialog`、`aria-modal=true`、
  `aria-labelledby=approval-title`；标题「电脑操作审批」。
* **稍后处理 / Esc**：只收起窗口、保留待审批项（入口重新出现），不做任何决定。
* **approve 请求体**（页面内挂钩 fetch 实测）：
  `{"decision":"approved","turn_id":"turn_a3bd5343957e","request_digest":"7778bb44785b7c12ef913095fd435b1c"}`。
* **成功出队 + 只执行一次**（直接查库）：
  `pending_approvals`：`appr_e3ba1f57138e, kind=computer, turn_id=turn_a3bd5343957e,
  status=approved`（created 17:57:43 → resolved 17:57:45）；
  `tool_calls` 里该命令**只有 1 行**；`tool_records` 也只有 1 行。

**缺口（未修，属后端路由）**：审批身份里 `session_id` 全程为 `NULL` ——
`approvals.set_context()` 只传 `turn_id`（`services/app.py:1131`、
`services/turn_orchestrator.py:78`），所以 `approval.py:332` 的会话校验永远不可能触发；
前端 store 也带了 `sessionId` 字段但事件里拿到的是 null。有效绑定是
turn_id + request_digest（两者都实测被服务端校验）。

---

## 4. 无进展暂停（G6）与「后端已核对」（G5）

### 4.1 无进展暂停：修掉一个**说假话**的收费点

真实链路：连续 3 次 `echo` 拿到完全相同结果 → 后端 `no_progress` 暂停。

* 修复前实测：操作条文案是 **「已达迭代上限 3/128」**（`aria-label` 同）——
  预算根本没耗尽（3/128），原因是"重复调用没有新信息"。用户据此决定继续/停止。
* 根因：`handleApprovalRequired` 只取 `used/max` 丢掉 `payload.reason/message`，
  `ContinueBar` 把文案写死。
* 修复后实测：`这一轮没有新的进展` + 后端原话
  `\`echo\` 连续 3 次给出完全相同的结果，这一轮没有新的进展`；
  `aria-label` 同步为「这一轮没有新的进展，需要你决定是否继续」。
* **不抢焦点**：操作条出现时焦点仍在 `TEXTAREA#composer-input`。
* **继续后可以恢复**：点「继续」→ 计数清零、本轮继续跑完
  （第 4 次 echo + 最终文本 `RESUMED-AFTER-CONTINUE …` 正常显示）。
* **停止后正确结束**：点「停止」→ 该轮结束并留下
  「本轮暂停：echo 连续 3 次给出完全相同的结果…按你的选择停下来了。」，
  模型**没有**再被调用（脚本里的 `SHOULD-NOT-APPEAR` 未出现）。

### 4.2 「后端已核对」只在有后端证据时出现

假模型调用 `declare_completion(task_id=task_does_not_exist_9f3a, …)`：
后端拒绝 → 页面 `verified-note` 数量 = 0、全页无「后端已核对」字样，
而是显示「声明没有通过核对（以后端记录为准）：- 找不到开发任务…」
与「这几项没有通过验证，不能当作「已完成 / 可使用」。」
（后端 `turn_facts.declaration` 只在 accepted 时返回记录，见
`core/turn_facts.py:183`。）

正面用例（真实存在证据 → 出现「后端已核对」）**本次未跑通**：
它需要完整的「创建工具 → 写文件 → 跑测试 → 提交 → 注册」开发任务链，
本轮时间不足以在线路上跑完（见第 6 节 Not Verified）。

---

## 5. 必须人工在真机完成的部分（G1 安装器全链）

以下路径**本机沙箱无法执行**（安装器 exit=2，见第 0 节），必须在有桌面会话、
写权限正常的机器上做；每一步都要留证据（截图 / 日志 / 数据库查询）：

1. 安装：`QIO_<version>_x64-setup.exe`（默认 per-user 路径）。
2. 首次启动：壳起来、sidecar `qio-backend.exe` 被拉起、随机端口上的
   `/api/health` 通（壳把 {port, token} 只交给自己的 WebView）。
3. 内置 embedding 模型：首次启动后 `%APPDATA%\qio\models\bge-small-zh-v1.5`
   出现且带 `.ready` 指纹；离线（断网）发起一次记忆检索，确认**没有**退回 BM25。
4. 设置 provider → 保存 credential（`POST /api/credentials`），确认 keyring 里
   有密文、数据库里没有明文。
5. 发起对话 → 创建工具（`create_tool`）→ **测试前授权** →
   dependency install → 测试 → 提交 → 注册 → 调用 → 工具记录（`/api/tools/…` 或工具卡展开）。
6. 重启应用：工具仍在（`tool_store` 里能 load）、开发任务恢复（DevTaskEntry 可见）、
   授权状态恢复（不需要重新授权同一工具）。
7. 更新：把新版本安装包与 `latest.json` 放到更新端点 → 应用内「检查更新」→
   下载 → 重启完成更新（`createUpdaterArtifacts` 产出的 .sig 必须真的被验）。
8. 卸载：先退出应用（`main.rs` 里已处理"子进程残留会锁住 qio-backend.exe"），
   再 `uninstall.exe`；确认程序目录、开始菜单项、卸载项注册表都被清掉，
   **用户数据目录 `%APPDATA%\qio` 保留**（数据不该被卸载带走）。

发布前请先跑 `python scripts/release_gate.py`：当前它会在 `sidecar.fresh` 上亮红灯
（dist 里的 0.1.10 安装包比今天重建的 sidecar 旧），必须先重新打包。

---

## 6. 其余各项的实测结论

| 项 | 结论 | 证据 |
| --- | --- | --- |
| G7 WebGL init | `getContext('webgl2')` 耗时 **≈101 ms**；点开星球到 `.planet-view` 出现 ≈101 ms | CDP 注入 `Page.addScriptToEvaluateOnNewDocument` 计时的真实运行 |
| G7 topic assembly | **无法单独测**：话题画在 canvas 里、没有 window 级调试钩子（`debugState()` 只在组件内部用） | 代码检索；未新增生产埋点（任务书要求不为此改设计） |
| G7 真机 GPU / 高刷 / 触控板 / 旋转连续性 / 数据替换 / 冷启动 | **未验证**，本机是 headless + SwiftShader | 需真机人工 |
| G8 审批入口/弹窗 | role=dialog、aria-modal、aria-labelledby、打开焦点入内、Esc 只收起、Tab 循环、focus-visible 都有 | 源码 + 真实运行 + `ApprovalDefer.test.ts` |
| G8 凭据弹窗 | **缺 Tab 循环** → 已修（与 ApprovalModal 对齐）；新增 4 条用例 + 真实运行复核（27 个可聚焦元素，Tab 从末位回到首位且仍在弹窗内） | `CredentialModal.test.ts` |
| G8 星球控件 | 已有 aria-label / role=option+aria-selected / 键盘 Enter/Space / focus-visible；**缺 listbox 祖先** → 已修（真实运行：`ul.topic-list[role=listbox][aria-label=话题列表]`） | `PlanetView.vue` + 真实运行 |
| G8 focus-visible / 对比度 | `--focus-ring` 两套主题都有；组件用 `:focus-visible` 描边。**对比度未做数值计算**（需要另写脚本取 tokens 计算） | `tokens.css:89` |
| G9 favicon 404 | **obsolete**：index.html 用的是内联 `data:image/svg+xml` favicon，真实启动 0 个 HTTP 失败、没有 `/favicon.ico` 请求 | 生产构建启动探针（`httpFails: []`、`errors: []`） |
| G9 「短暂 tok 0」 | **obsolete**：token 下缀只在开发者模式显示，且 `recordUsage` 对没有 token 的事件不会建记录（`if (!Number.isFinite(tokens) && !prev) return;`），代码里不存在"占位 0"路径；真实一轮采样（200ms × 30）未见 `tok 0` | `MessageItem.vue:20-25`、`events.ts:539-550` |
| 前端全量测试 | `npx vitest run` → **80 个文件 / 754 条全绿**（含新增用例） | 真实运行输出 |
| 前端类型检查 | `vue-tsc --noEmit` 通过（`npm run build` 第一步） | 构建输出 |
| 对话链路 | 生产构建 → 真实后端 → 假厂商 → 助手回复正常渲染；`resyncProtocol` 未单独验 | 真实运行 |

---

## 7. Not Verified（不写成完成）

* G1 安装/首次启动/更新/卸载全链：本会话沙箱拒绝工作树外写入与 HKCU 写入，
  安装器 exit=2；只完成了可离线判定的发布闸门（第 1 节）与人工清单（第 5 节）。
* G4 工具创建卡（proposal → building → testing → waiting approval → registering → ready
  同一张卡推进）与 G5 的**正面**用例：需要完整开发任务链，本轮未跑通；
  G4 的 failure/retry/unfinished/continue development/revoke 分支同样未验。
* G3 里 `session_id` 这一路身份（见第 3 节缺口）。
* G7 真机性能与连续性；G8 对比度数值；G9 开发者模式下的目视复核。
