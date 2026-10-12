# QIO 窗口审计报告（W0）

> 目标：穷尽列出 QIO 里**所有**可能产生「QIO 本体以外窗口」的用户操作，包括一闪而过的控制台黑框。
> 三端覆盖：Vue 前端 / Tauri 壳（Rust）/ 后端（Python + 打包产物 + NSIS 安装器）。
> 本报告**没有改动任何产品代码**，只给结论与「想让它不出现该改哪里」的建议。
>
> 审计对象（被测实物）：
> * 源码工作树：D:\qio-dev\qio-irm-int（分支 integrate/reliability-interactive-real-model-20261011）
> * 已安装发行版：D:\QIO\qio.exe（release，PE subsystem = WINDOWS_GUI=2），运行中 pid 95524（sidecar.lease.95524.json），数据目录 D:\QIO-data（用户环境变量 QIO_DATA_DIR）
> * 实测机：Windows 11，默认终端应用 = Windows 终端（这一点很重要，见 §5.1）
> * 审计时间：2026-10-12

---

## 0. 结论摘要（先看这三条）

**C-1（最高风险，真实存在、可复现）**：**QIO 每次启动、启动后约 10 秒的静默检查、每 24 小时的定时检查、以及用户点「检查更新」，都会让 Windows 弹出一个可见的控制台窗口。** 来源是 frontend/src-tauri/src/updater_net.rs:321 的 reg query（读系统代理），通过 frontend/src-tauri/src/ownership.rs:863 的 std::process::Command 拉起，**没有 CREATE_NO_WINDOW**。本机默认终端是 Windows 终端，所以用户看到的是一个**约 0.2–0.4 秒的 Windows 终端窗口闪现**（不是 conhost 黑框，但同样是「QIO 本体以外的窗口」）。
实测：启动第二个 QIO 实例的头 13 秒里，录到 **4 次可见窗口**（每次一个 reg.exe）。
旁证（用户正在跑的实例自己的日志，D:\QIO-data\logs\qio.log）：2026-10-12 00:22:40–00:25:50 三分钟内 **9 次系统代理查询**（每次 1–2 个 reg.exe）+ **1 次 taskkill**。

**C-2（同源）**：taskkill（ownership.rs:863-907）同样没有创建标志。触发点：安装更新前结束后端（qio_prepare_for_update）、后端恢复清理、主窗口退出的兜底路径。实测一个 taskkill 会让 Windows 终端窗口**可见约 1.17 秒**。

**C-3（设计如此，但值得知道）**：QIO 会开出「本体以外窗口」的**预期**操作共 6 类：原生文件选择器、系统默认程序打开附件、文件资源管理器（在文件夹中显示）、外链交给默认浏览器、安装器 UI、同安装目录的第二个 QIO 实例。这些都有据可查、属本意。

一句话回答用户的问题：**除了那些本来就该弹的对话框，QIO 目前唯一在用户什么都没做时也会闪的，是系统代理探测（reg.exe）与进程收尾（taskkill）拉起的控制台窗口。**

---

## 1. 方法与证据分级

本报告每条结论都标了来源：

| 标记 | 含义 | 判定标准 |
| --- | --- | --- |
| 【码】 | 我在代码里确认的 | 读到了真实执行路径（不是注释、不是测试桩），必要时追到依赖 crate 源码 |
| 【测】 | 我实测观察到的 | 真机跑出来、有原始输出（窗口事件流 / 进程树 / 日志） |
| 【推】 | 我推断的 | 由【码】+【测】外推，但没有直接观测到该场景 |

实测工具（本机 PowerShell 5.1）：SetWinEventHook(EVENT_OBJECT_CREATE..HIDE) 顶层窗口事件流（能抓到 10ms 级闪现，比轮询可靠）+ EnumWindows 可见窗口快照 + PE 头读 subsystem + 进程树。脚本见 §8。

风险三档（按任务书）：
* **A 预期窗口**：用户点一下，本来就该出现的东西。
* **B 需要用户确认**：对话框 / 安装器 / 需要点确认；或能力上需要用户批准（agent 执行任意程序）。
* **C 疑似闪现或多余窗口**：用户没要求、也不该出现的窗口（控制台闪现、失败弹窗、开发态噪音）。

---

## 2. 总表

「无窗口标志」列：✅ 到位；❌ 缺失；— 不适用（GUI 程序 / 系统对话框本来就没有控制台）。

| # | 触发操作（界面上的动作） | 代码位置 file:line | 会开出什么 | 本意? | 无窗口标志 | 风险档 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 点「附件」选本地文件（桌面壳） | frontend/src/components/Composer.vue:745-755 → frontend/src/services/attachments.ts:483-488 → frontend/src-tauri/src/attachment_picker.rs:14-28, 83 | Win32 原生「打开」文件对话框（GetOpenFileNameW，owner = QIO 主窗口，模态） | 是 | —（系统对话框） | A 低 |
| 2 | 同上，原生选择器不可用 / 抛错时回退 | Composer.vue:757-766；模板 Composer.vue:1627-1636（input type=file multiple） | WebView2 自带文件选择对话框 | 是 | — | A 低 |
| 3 | 历史消息点「重新定位」 | frontend/src/components/MessageItem.vue:285 → attachments.ts:483 | 同 #1 原生对话框 | 是 | — | A 低 |
| 4 | 历史消息点「重新上传」，取消原生选择后 | MessageItem.vue:313-320 → attachments.ts:185-214 | WebView2 文件选择对话框 | 是 | — | A 低 |
| 5 | 添加菜单 / 板面卡片点「上传附件」 | frontend/src/components/interactive/AddMenu.vue:79；BoardCard.vue:178 → attachments.ts:185-214 | WebView2 文件选择对话框 | 是 | — | A 低 |
| 6 | 点附件「打开」（普通文件，桌面壳） | MessageItem.vue:267、Composer.vue:916 → attachments.ts:740 → 壳命令 open_attachment_path（attachment_picker.rs:114-130，shell().open） | **系统默认程序窗口**（图片查看器 / PDF / 记事本 / 播放器 / 浏览器…） | 是 | —（ShellExecuteW） | A 中 |
| 7 | 点可执行 / 脚本类附件的「打开」→ 自动降级为「在文件夹中显示」 | attachments.ts:573-590, 736 → attachment_picker.rs:134-137, 172-180 | **文件资源管理器窗口**（explorer.exe /select） | 是（界面有说明文案） | —（explorer 是 GUI 进程） | A 中 |
| 8 | 显式点「在文件夹中显示」 | 同 #7 | 同上 | 是 | — | A 低 |
| 9 | 点 Markdown 正文里的外链 | frontend/src/components/MarkdownContent.vue:86, 337-356 → frontend/src/utils/externalLink.ts:42-52（invoke plugin:shell 的 open） | **系统默认浏览器窗口** | 是（只放行 http/https/mailto） | —（ShellExecuteW） | A 中 |
| 10 | 点正文里的 mailto: 链接 | 同 #9 | 默认邮件客户端窗口 | 是 | — | A 低 |
| 11 | 浏览器里（非壳）点外链 | externalLink.ts:54-62 | 浏览器新标签页 | 是 | — | A 低（非安装版） |
| 12 | 浏览器里点附件「打开」（安全类型副本） | attachments.ts:698-716, 744-751 | 浏览器新标签页（blob URL） | 是 | — | A 低（非安装版） |
| 13 | 点「下载」/ 打开失败回退下载 | attachments.ts:636-657（anchor download） | WebView2 内置下载提示（**不是顶层窗口**，在应用内） | 是 | — | A 低（未实测） |
| 14 | 板面 url 卡片点链接 | frontend/src/components/interactive/BoardCard.vue:537（a target=_blank，无 click 处理） | **什么都不开**：wry 未设 new_window_req_handler 时 args.SetHandled(true)（wry-0.55.1/src/webview2/mod.rs:781-783）把请求吞掉 | 否（点了没反应） | — | 低（UX 缺陷，非窗口问题） |
| 15 | **启动时（无需任何操作）** 代理探测：reg query ProxyEnable | frontend/src-tauri/src/updater_net.rs:304-350（:321 Command::new 的 reg）→ frontend/src-tauri/src/ownership.rs:105-132 | **可见控制台窗口**（本机为 Windows 终端窗口，约 0.2–0.4s） | 否 | ❌ 缺 CREATE_NO_WINDOW | **C 高** |
| 16 | 同一次探测的第二条 reg query ProxyServer（系统代理启用时才查） | updater_net.rs:340-348 | 又一个可见控制台窗口 | 否 | ❌ | **C 高** |
| 17 | 启动后 10 秒静默检查 / 每 24 小时 / 用户点「检查更新」 | frontend/src/stores/updater.ts:222-237 → frontend/src/services/updater.ts:179-188（invoke qio_refresh_updater_proxy）→ #15/#16 同一条路径 | 1–2 个可见控制台窗口 | 否 | ❌ | **C 高** |
| 18 | 点「下载并安装」→ 安装前结束后端 | services/updater.ts:196-203 → 壳命令 qio_prepare_for_update → frontend/src-tauri/src/backend_lifecycle.rs:121-165（taskkill /PID … /T /F）→ ownership.rs:863-897 | **可见控制台窗口（taskkill）**，实测可见约 1.17s | 否 | ❌ | **C 高** |
| 19 | 关主窗口（job 未生效的兜底）/ 退出的兜底清理 | frontend/src-tauri/src/main.rs:1527-1546（shutdown_runtime）、1152-1170（terminate_runtime_backend） | 可见控制台窗口（taskkill） | 否 | ❌ | C 中 |
| 20 | 后端起不来 → 启动失败 | main.rs:1176-1196（fatal_backend_start_failure）→ main.rs:1309-1327（show_startup_error_dialog） | **模态错误对话框**「QIO 启动失败」（MessageBoxW，MB_SETFOREGROUND） | 是（有意为之；QIO_STARTUP_ERROR_DIALOG=0 可关） | —（对话框本身即窗口） | B 中 |
| 21 | 写不下归属记录（安装目录不可写等） | main.rs:1342-1387（fatal_lease_failure）→ 同一个 MessageBoxW | 同上 | 是 | — | B 中 |
| 22 | 启动后端 sidecar（每次启动） | main.rs:1254-1272（app.shell().sidecar）→ tauri-plugin-shell-2.3.5/src/process/mod.rs:20, 173 | 隐藏控制台（conhost 存在但**不可见**） | 是 | ✅ CREATE_NO_WINDOW | 无 |
| 23 | 后端内部全部子进程（agent 工具、沙箱、taskkill、docker、py 探测、icacls 探针） | backend/src/agent/tools/cmd_tools.py:189-347；proc_cleanup.py:55-70；sandbox.py:167,424,729,801,856；tool_envs.py:394,422,601；isolation.py:436 | 继承 #22 的隐藏控制台 → 不新开窗口 | 是 | ⚠️ 自身无任何 creationflags，**靠继承** | B 中（结构脆弱点） |
| 24 | agent 用 run_program 启动程序（需审批） | cmd_tools.py:189-208 | **任意系统程序窗口** | 是（用户批准后） | ❌（靠继承隐藏控制台） | B 中 |
| 25 | agent 用 run_shell 执行自由命令（需审批） | cmd_tools.py:230-261 | 任意（GUI 程序会开窗；控制台程序走隐藏控制台） | 是（需审批） | ❌（同上） | B 中 |
| 26 | agent 用 proc_kill 结束进程 | cmd_tools.py:313-347 | 隐藏控制台里的 taskkill，无窗口 | 是 | 继承 | 低 |
| 27 | 双击安装包 QIO_x.y.z_x64-setup.exe | frontend/src-tauri/target/release/bundle/nsis/QIO_0.2.0_x64-setup.exe（NSIS，125 MB） | NSIS 安装向导窗口 | 是 | 安装器自身 UI；实测清单 requestedExecutionLevel=**asInvoker** → **不弹 UAC** | A 低 |
| 28 | Win10 缺 WebView2 时安装 | 生成的 installer.nsi：INSTALLWEBVIEW2MODE=downloadBootstrapper、WEBVIEW2INSTALLERARGS=/silent | 联网静默安装 WebView2（无窗口） | 是 | /silent | A 低 |
| 29 | 安装向导勾选「运行 QIO」 | Tauri NSIS 模板 | 一个新的 QIO 实例窗口 | 是 | — | A 低 |
| 30 | 应用在跑时执行卸载 | frontend/src-tauri/nsis/qio-ownership.nsh:34（MessageBox MB_OKCANCEL） | 卸载器的确认对话框 | 是 | NSIS MessageBox | B 低 |
| 31 | 卸载 / 更新时结束后端与外壳 | qio-ownership.nsh:28, 36；nsis/installer-hooks.nsh:103（nsExec::ExecToStack 调 qio-uninstall-helper.exe） | 隐藏控制台 helper；它再调的 taskkill 也在这条隐藏控制台里 | 是 | helper 由 nsExec 以隐藏方式启动 | 无（【推】，见 §7） |
| 32 | 从同一安装目录再启动一个 QIO | main.rs:1426-1456（note_and_prune_instances，按实例写 lease） | **另一个 QIO 实例窗口** | 是（设计如此） | — | A 低 |
| 33 | 开发者：tauri dev / debug 构建 | main.rs:16（仅 release 设 windows_subsystem） | debug 版是 console 子系统 → 控制台窗口（就是开发终端） | 是（开发态） | ❌（debug 有意不加） | C 低（仅开发者） |
| 34 | 开发者：verify-r4/r5/r6/r7/r8/fb/acc/audit-phase2.ps1 里的 Start-Process | scripts/verify-r4-phase2.ps1:43、r5:36、r6:36、r7:36、r8:36、fb:36、acc:41、audit:46 | 新控制台窗口（缺 -WindowStyle Hidden） | 否（脚本噪音） | ❌ | C 低（仅开发者） |
| 35 | 开发者：install_e2e*.py 静默安装 / 卸载、再拉起应用 | scripts/install_e2e.py:97-104（/S /D=）、:1646、:1659-1673（uninstall /S）、:455（Popen qio.exe） | 静默（无窗口）+ 一个 QIO 窗口 | 是 | /S | A 低（仅开发者） |
| 36 | 开发者：verify-e2e.ps1 截图 | scripts/verify-e2e.ps1:124-126 | msedge --headless=new → 无窗口 | 是 | headless | 无 |
| 37 | WebView2 渲染进程（系统组件，随应用起） | 由 Tauri / WebView2 管理 | 无可见顶层窗口（实测：只有 vis=False 的 Chrome_WidgetWin_*） | 是 | — | 无 |

---

## 3. A 档详解：预期窗口

### A-1 原生文件选择器（两条不同入口，必须分清）

任务书要求核实：attachment_picker.rs 的原生对话框与 attachments.ts 的 pickBrowserFile / input[type=file] 是不是两条入口、分别什么时候用。
**结论：【码】是两条独立入口，两套实现，触发条件有先后顺序（互斥）。**

* 入口甲（原生 GetOpenFileNameW）：attachments.ts:483-488 pickLocalPath() → 壳命令 pick_attachment_file → attachment_picker.rs:14-28 把调用 run_on_main_thread → :53-93 pick_native() → **GetOpenFileNameW(:83)**。过滤器「所有文件 / 文本代码」，标题「选择要附加的文件」，owner 取主窗口 HWND（:37-50）。
* 入口乙（WebView2 自己的文件对话框）：attachments.ts:185-214 pickBrowserFile() —— 现场造一个 input type=file、挪到屏幕外、触发 click()，靠 change 事件拿 File；取消不触发 change，用 window focus + 300ms 兜底判取消。

**什么时候用哪个（逐个调用点）**
* Composer「附件」按钮：**先甲**（Composer.vue:751）；只有甲不可用 / 抛错才**回乙**（:757-766 → fileInputRef.click()，模板 :1627-1636）。用户主动取消（甲返回 null 且探针说选择器可用）时**不会**再弹一次乙（:756-760），这一点是对的。
* 历史消息「重新定位」（MessageItem.vue:285）与「重新上传」（:313）：**先甲**；重新上传在甲返回 null（取消）后才**回乙**（:317）。
* 添加菜单（AddMenu.vue:79）与板面卡片（BoardCard.vue:178）：**直接用乙**（没有甲）。

**最小复现**：桌面壳 → 会话页点气泡上的「附件」→ 出现「选择要附加的文件」（甲）。让 pick_attachment_file 失败（或让探针返回 false）→ 出现 WebView2 的「打开」对话框（乙）。
**不想让它出现**：这是本意，不建议关。若想统一体验，建议把 AddMenu / BoardCard 也接到 pickLocalPath（能拿到真实路径，字节上传只作为回退），乙只留作降级。
**注意**：两条入口弹出的都是「QIO 本体以外的系统对话框」，模态归属不同（甲有 owner=主窗口，乙由 WebView2 管）。

### A-2 打开附件 = 系统默认程序（attachment_picker.rs:114-130）

open_attachment_path：先 checked_file()（:140-155，只接受绝对路径上真实存在的**文件**，拒绝目录 / 相对路径），再 is_executable_file()（:158-170，50 个可执行 / 脚本扩展名清单）——可执行类**拒绝自动打开**并提示改用「在文件夹中显示」；其余走 app.shell().open(...)（tauri-plugin-shell → ShellExecuteW）→ **系统默认程序窗口**。
**最小复现**：给会话加一个 .pdf 或 .png 附件 → 点「打开」→ 默认程序窗口出现。
**不想让它出现**：把 desktop 的 openPlanFor（attachments.ts:573-601）默认改成 reveal（只定位不打开），或固定用内置预览（本项目已有 CardAttachmentPreview.vue）。注意壳侧还有第二道 fail-closed，只改前端不够。

### A-3 在文件夹中显示 = explorer.exe /select（attachment_picker.rs:172-180）

std::process::Command::new("explorer.exe").arg(format!("/select,{}", path))，参数数组不拼命令行（路径带空格安全）。**实测（§6.3）**：出现了可见的「文件资源管理器」窗口（class=CabinetWClass，标题最终为 QIO - 文件资源管理器），**没有任何控制台事件**。
**最小复现**：加一个 .exe 附件 → 点「打开」（自动降级为 reveal），或任何附件点「在文件夹中显示」。
**不想让它出现**：这是本意；若不想要，改成应用内高亮所在卡片 / 复制路径，不要调 explorer。

### A-4 外链 → 默认浏览器（externalLink.ts:42-52 / MarkdownContent.vue:337-356）

Markdown 渲染出的 a.ext-link（:86）在容器上做事件委托（:337-356），命中后 preventDefault() 再 openExternal()；desktop 走 invoke("plugin:shell 的 open") → ShellExecuteW → **默认浏览器窗口**。白名单只放行 http/https/mailto（:13-26），并挡掉 java 换行 script 这类控制字符伪装（:15）。
**最小复现**：让模型回答里带一个 https 链接 → 点它 → 浏览器窗口。
**不想让它出现**：改成应用内打开（内置 WebView / iframe）——注意当前 CSP frame-src 为 none（tauri.conf.json:23），要开内嵌浏览器得同时改 CSP，代价大。

### A-5 安装器 / 第二个实例

* 安装器：NSIS 向导（预期窗口）；实测清单是 **asInvoker**（currentUser 安装）→ **不弹 UAC**；生成的 installer.nsi 里 INSTALLWEBVIEW2MODE=downloadBootstrapper + /silent（缺 WebView2 时静默联网装）。
* 第二个实例：main.rs:1426-1456 明确支持同安装目录多实例（每实例一份 sidecar.lease.<pid>.json），所以「又开了一个 QIO 窗口」是设计行为。

---

## 4. B 档详解：需要用户确认 / 需要用户批准的窗口

### B-1 启动失败对话框（main.rs:1309-1327）

show_startup_error_dialog() → MessageBoxW(null, …, MB_OK | MB_ICONERROR | MB_SETFOREGROUND)。触发路径两条：后端起不来（fatal_backend_start_failure，:1176-1196）与归属记录写不下（fatal_lease_failure，:1342-1387，fail-closed：先收后台再弹框，退出码 1）。
**最小复现**：把数据目录指向只读位置、安装目录也不可写 → 启动即弹「QIO 启动失败」。
**不想让它出现（自动化 / 无人值守）**：QIO_STARTUP_ERROR_DIALOG=0（:1312-1314 已实现）。排查时原因同时写在数据目录 / 安装目录下的 qio-startup-error.txt（:1293-1305）。
**建议**：保持现状。这条是「窗口都起不来」的兜底，模态是合理的。

### B-2 agent 执行任意程序 / 自由 shell（后端）

* run_program（cmd_tools.py:187-208）：asyncio.create_subprocess_exec(program, *args, shell=False)，走 computer.py 的风险分类 + 审批（plan 模式拒绝）；审批 UI 会给用户看「想运行什么」。
* run_shell（cmd_tools.py:230-261）：create_subprocess_shell，**永远需要审批**。
* proc_kill（:313-347）：Windows 用 taskkill /F /PID（不带 /T）。
**风险**：被批准的程序如果是 GUI 程序（notepad、浏览器、安装器…），就会出现 QIO 之外的窗口；这是能力本身，不是缺陷。**最小复现**：在对话里让 QIO 打开记事本 → 审批卡片 → 批准 → 记事本窗口。
**建议**：审批文案里显式写「会在你的桌面上打开窗口」；对 GUI 类目标（start、explorer、无控制台版的 exe）给更强提示。这一层不属于窗口缺陷。

### B-3 后端 child 进程的「结构脆弱点」（建议修，但不在当前窗口路径上）

全仓检索：**backend 的 Python 代码里没有任何 CREATE_NO_WINDOW / creationflags / startupinfo**（0 命中）。后端能不出窗口，完全依赖「父进程 qio-backend.exe 带着一个隐藏控制台」这一事实：
* 实测：qio-backend.exe 是 **console 子系统**（PE subsystem=3），由一个 conhost.exe 陪着，但**没有可见窗口**；它由 tauri-plugin-shell 拉起，该插件在 Windows 上给每个子进程加 CREATE_NO_WINDOW（tauri-plugin-shell-2.3.5/src/process/mod.rs:20, 173）。
* 于是后端所有子进程（agent 的 taskkill、沙箱 worker、docker 客户端、py 探测、isolation 的 cmd /c 探针）都继承这个不可见控制台 → 不弹窗。
**风险**：如果有人把后端以别的方式拉起（DETACHED_PROCESS，或 GUI 进程直接 CreateProcess 不带标志），这些 taskkill / cmd 会**集体变成可见闪烁**。
**建议（只给建议）**：① shell 侧保持 sidecar 走插件；② 后端给「必定会弹窗的类别」（taskkill、cmd /c、docker、icacls）在 Windows 上加 creationflags=CREATE_NO_WINDOW（Python 3.7+ 的 subprocess.CREATE_NO_WINDOW），把「不出现在屏幕上」从继承来的运气变成自己保证的语义；③ 加一条 CI 断言：backend 里所有 create_subprocess_* 调用点都必须显式带这个标志（或统一走一个 wrapper）。

---

## 5. C 档详解：疑似闪现 / 多余窗口（本次审计的主角）

### 5.1 C-1 系统代理探测 → reg.exe → 可见控制台窗口 【码】+【测】

**代码路径**
* updater_net.rs:304-350 system_proxy()：先 reg query HKCU 下 Internet Settings 的 ProxyEnable（:321 std::process::Command::new("reg")），enabled 为真再查 ProxyServer（:347）。
* 命令统一走 ownership.rs:105-132 run_command_with_timeout()：stdin(null) + stdout/stderr(piped)，然后 spawn()。**没有任何 creation_flags。**
* 调用时机：① 每次启动（main.rs:1462-1473 的后台线程 resolve_updater_proxy）；② 前端启动 10 秒后的静默检查与每 24 小时定时（stores/updater.ts:222-237 → services/updater.ts:181 qio_refresh_updater_proxy）；③ 用户点「检查更新」。
* 这台机器上 HTTPS_PROXY（用户 / 机器）为空、数据目录里没有 updater-proxy.txt → **一定走系统代理查询**。

**为什么会有可见窗口**：qio.exe 是 release 构建，main.rs:16 的 windows_subsystem = "windows" → 实测进程 **PE subsystem=2（WINDOWS_GUI，无控制台）**；它用 std::process::Command 拉起 console 程序且不带 CREATE_NO_WINDOW / DETACHED_PROCESS → Windows 会给子进程新分配控制台并**把窗口显示出来**。本机默认终端是 Windows 终端，所以显示为终端窗口。

**实测（真机，release 安装版，第二个实例）**：探针捕获 **4 次可见窗口**，全在 QIO 启动 / 静默检查的窗口期内：

~~~
  1522ms SHOW  pid=85800  proc=WindowsTerminal class=CASCADIA_HOSTING_WINDOW_CLASS title=Terminal
  1916ms HIDE  pid=85800  proc=WindowsTerminal class=CASCADIA_HOSTING_WINDOW_CLASS title=C:\Windows\system32\reg.exe   ← 可见 394ms
  2482ms SHOW  pid=98560  proc=WindowsTerminal class=CASCADIA_HOSTING_WINDOW_CLASS title=Terminal
  2676ms HIDE  pid=98560  proc=WindowsTerminal class=CASCADIA_HOSTING_WINDOW_CLASS title=C:\Windows\system32\reg.exe   ← 可见 194ms
 11448ms SHOW  pid=83208  proc=WindowsTerminal class=CASCADIA_HOSTING_WINDOW_CLASS title=Windows Terminal
 11642ms HIDE  pid=83208  proc=WindowsTerminal class=CASCADIA_HOSTING_WINDOW_CLASS title=C:\Windows\system32\reg.exe   ← 可见 194ms（启动后 10s 静默检查）
 12154ms SHOW  pid=106616 proc=WindowsTerminal class=CASCADIA_HOSTING_WINDOW_CLASS title=Terminal
 12422ms HIDE  pid=106616 proc=WindowsTerminal class=CASCADIA_HOSTING_WINDOW_CLASS title=C:\Windows\system32\reg.exe   ← 可见 268ms
~~~

**用户自己的实例也在做同样的事（旁证，非我触发）**：D:\QIO-data\logs\qio.log

~~~
[2026-10-12][00:22:42][qio::updater_net][INFO] [qio] 系统代理未启用
[2026-10-12][00:22:51][qio::updater_net][INFO] [qio] 系统代理未启用
[2026-10-12][00:23:19][qio::updater_net][INFO] [qio] 系统代理未启用
[2026-10-12][00:24:05][qio::updater_net][INFO] [qio] 更新走代理（来源：Windows 系统代理）：http://127.0.0.1:22924
[2026-10-12][00:24:09][qio::updater_net][INFO] [qio] 更新走代理（来源：Windows 系统代理）：http://127.0.0.1:22924
[2026-10-12][00:25:50][qio::updater_net][INFO] [qio] 更新走代理（来源：Windows 系统代理）：http://127.0.0.1:22924
~~~

三分钟内 6 次「探测完成」，按代码算 = **9 次 reg.exe**（「系统代理未启用」= 只查 ProxyEnable，1 次；「走系统代理」= ProxyEnable + ProxyServer，2 次）。外加 00:25:17 的 taskkill（见 C-2）。
更早的历史佐证：2026-10-03 18:39:36 的日志有「读系统代理失败或超时（ProxyEnable），按直连处理」，启动耗时「代理探测 3242ms」；updater_net.rs:32-35 的注释记录了本机 reg.exe 报 0xC0000142 并弹模态框、把启动钉住 1 分 42 秒的事故。

**最小复现**
1. 确认 HTTPS_PROXY 为空、数据目录下的 updater-proxy.txt 不存在（本机现状）。
2. 用 §8 的监听脚本开一个窗口事件流。
3. 双击 D:\QIO\qio.exe（或从命令行启动）。
4. 看启动后 0–3 秒与第 10–13 秒：各出现 1–2 个可见终端 / 控制台窗口，随即消失。

**如果不想让它出现（建议）**
* 最小改动、覆盖全部触发点：在 ownership.rs:105 run_command_with_timeout() 里对 Windows 加
  use std::os::windows::process::CommandExt; cmd.creation_flags(0x0800_0000 /* CREATE_NO_WINDOW */);
  —— reg（updater_net.rs:321）与 taskkill（ownership.rs:864）都走这个函数，一次改全部。
  **注意**：explorer.exe（attachment_picker.rs:175）不走这个函数，不要顺手给它加（它是 GUI 程序）。
* 更彻底：不要用 reg.exe。改成直接读注册表（windows-sys 的 Registry API），或把系统代理查询挪到「用户真的点检查更新」时再做一次并缓存，启动期完全不查。
* 产品级选项：既然代理只是给 updater 的提示，宁可默认直连，把系统代理查询降级为「手动点『检测代理』」。

### 5.2 C-2 taskkill → 可见控制台窗口 【码】+【测】

**代码路径**：ownership.rs:863-883 taskkill() → std::process::Command::new("taskkill")（无创建标志）；包装 kill_tree_by_pid_verbose(:895-897) / kill_pid_verbose(:905-907)。
触发点：
* backend_lifecycle.rs:121-165 stop_tree_by_pid()（安装更新前结束后端：services/updater.ts:196-203 → 壳命令 qio_prepare_for_update）；
* backend_lifecycle.rs:170-181 stop_pids()（恢复流程清理幸存 pid）；
* main.rs:1527-1546 shutdown_runtime()（**job 未生效时**的退出兜底）、main.rs:1152-1170 terminate_runtime_backend()；
* 卸载路径上的 helper 也会 taskkill（见 §7，那条是隐藏的）。

**实测（受控复现：GUI 子系统父进程 + 管道 stdio + 无创建标志，语义与 Rust 完全一致）**：

~~~
  341ms SPAWNED GuiSpawner.exe args=noflags taskkill /PID 99384 /T /F
 1850ms SHOW    pid=82244  proc=WindowsTerminal class=CASCADIA_HOSTING_WINDOW_CLASS title=Terminal
 1927ms CREATE  pid=103008 proc=taskkill       class=PseudoConsoleWindow
 1980ms SHOW    pid=103008 proc=taskkill       class=PseudoConsoleWindow
 3016ms HIDE    pid=82244  proc=WindowsTerminal class=CASCADIA_HOSTING_WINDOW_CLASS title=C:\Windows\SYSTEM32\taskkill.exe
 8859ms EXITED code=0
~~~

对照组（同样的父进程，只加 CREATE_NO_WINDOW）：**0 个窗口事件**。可见窗口时长 ≈ **1.17 秒**，比 reg 更显眼。

**最小复现**：QIO 里点「下载并安装」（走到 prepareForUpdate 就会 taskkill，日志会写「准备结束后端进程树：pid …（taskkill /PID … /T /F）」），此时看屏幕；或按 §6.2 的受控脚本复现机制。
**不想让它出现**：同 C-1 的一处修改即可。另外：能走 Job Object 就别走 taskkill（正常退出已优先用 job，main.rs:1152-1156，只有 job 未生效才 taskkill）。

### 5.3 C-3 开发态噪音（不影响最终用户）

* **debug 构建的 qio.exe 是 console 子系统**（main.rs:16 只对 release 设 windows_subsystem）→ tauri dev 时会有一个控制台窗口，就是开发终端本身，日志也打在那里。这是有意的。
* scripts/verify-r4/r5/r6/r7/r8/fb/acc/audit-phase2.ps1 里的 Start-Process -FilePath $py … -PassThru **缺 -WindowStyle Hidden** → 每跑一次验证脚本会额外弹一个控制台窗口（对照：verify-e2e.ps1:67、verify-sse-local.ps1:59,75、verify-audit-io-pressure.ps1:86 都加了 Hidden）。
  **建议**：给这几处补 -WindowStyle Hidden，或在脚本开头统一封装一个 Start-Hidden 函数。
* build_*.ps1 用 & 调用外部 exe / Invoke-External（同控制台，不新开窗）；build_nsis_with_patch.py:293 subprocess.Popen 同控制台；install_e2e*.py 安装 / 卸载都带 /S（静默），只有它主动 Popen(qio.exe) 时才会出现 QIO 窗口（那正是被测对象）。

---

## 6. 实测记录（原始证据）

### 6.1 环境与基线

* 审计开始时的进程树：qio.exe 95524 ← qio-backend.exe 82384 ← qio-backend.exe 94964；conhost.exe 102604（父 = 82384）**无可见窗口**。
* PE 子系统（读 PE 头）：D:\QIO\qio.exe → subsystem=2 (WINDOWS_GUI)；D:\QIO\qio-backend.exe → 3 (WINDOWS_CONSOLE)；D:\QIO 下的 qio-uninstall-helper.exe → 3 (WINDOWS_CONSOLE)。
* 可见顶层窗口基线：只有用户自己的应用（Chrome / Edge / QIO / 资源管理器…），无 QIO 相关多余窗口。
* **基线空跑 4 秒的窗口事件流：0 条事件** —— 下面对照组的信号是干净的。

### 6.2 受控实验：无创建标志 vs CREATE_NO_WINDOW

* 宿主：自编译的 GUI 子系统宿主（Add-Type -OutputType WindowsApplication），ProcessStartInfo{UseShellExecute=false, RedirectStandardOutput/Error=true, CreateNoWindow=false | true} —— 与 Rust run_command_with_timeout（管道 stdio + 无标志）和 tauri-plugin-shell（CREATE_NO_WINDOW）语义一一对应。
* 结果：noflags reg query … → 创建 PseudoConsoleWindow 且 **SHOW(vis=True)**；hide reg query … → **无任何窗口事件**。
* 结论：**可见性完全由创建标志决定**，与 reg 本身无关。【测】

### 6.3 explorer.exe /select（= 产品的 reveal_in_file_manager）

~~~
  420ms SPAWNED GuiSpawner.exe args=noflags explorer.exe /select,D:\QIO\qio.exe
 2687ms SHOW   pid=107156 proc=explorer class=CabinetWClass title=文件资源管理器
 3755ms CREATE pid=107156 proc=explorer class=CabinetWClass title=QIO - 文件资源管理器
~~~

全程无控制台类窗口。结论：reveal 路径只开文件资源管理器，符合预期。【测】

### 6.4 真机产品实测：启动第二个 QIO 实例（release 版）

方法：QIO_DATA_DIR 指向临时目录（不动用户的 app.db）+ QIO_STARTUP_ERROR_DIALOG=0，窗口事件流监听 55 秒，第 30 秒对该实例主窗口发 WM_CLOSE。结果（摘要）：
* 0.24–0.27s：QIO 主窗口（class=Tauri Window，title=QIO）CREATE + SHOW。
* 1.52s / 2.48s：**两次 Windows 终端窗口闪现**（reg.exe，见 §5.1）。
* 2.26s：qio-backend.exe 起来，只有 PyInstallerOnefileHiddenWindow（**vis=False**）+ 若干不可见窗口 → 后端确实无可见控制台。
* 0.42–0.46s：msedgewebview2.exe 的 Chrome_WidgetWin_0/1 全是 **vis=False** → WebView2 不开可见窗口。
* 11.4s / 12.1s：**又是两次终端窗口闪现**（启动后 10 秒的静默更新检查触发第二次代理探测）。
* 30.1s：WM_CLOSE → 主窗口 HIDE / DESTROY；进程在观测窗口内没退出（**观察**：合成 WM_CLOSE 后外壳进程仍存活、其后端树仍在。这不是本次审计范围，见 §7）。
* 清理：只对测试实例 pid 98328 执行 taskkill /PID 98328 /T /F；用户的 95524 + 82384 + 94964 完好，主窗口标题仍为 QIO；测试实例写入的 sidecar.lease.98328.json 在它自己的退出流程里已被删除（目录里只剩 87108 / 95524 / lease.json）。
* 该实例 stdout 旁证：内置模型已复制到临时数据目录（说明隔离生效，没有碰用户的 app.db）。

### 6.5 安装器实据

* frontend/src-tauri/target/release/bundle/nsis/QIO_0.2.0_x64-setup.exe（125 MB，本仓库构建产物）里同时含 requestedExecutionLevel 与 **asInvoker** → 不触发 UAC。
* 生成的 frontend/src-tauri/target/release/nsis/x64/installer.nsi：INSTALLMODE "currentUser"、INSTALLWEBVIEW2MODE "downloadBootstrapper"、WEBVIEW2INSTALLERARGS "/silent"，并已插入所有权守卫宏 !insertmacro QIO_CloseMainExeIfOwned 与 !define QIO_OWNERSHIP_PATCHED。
* D:\QIO 下的 uninstall.exe：同样含 asInvoker。

---

## 7. 存疑 / 未核实（如实交代）

1. **C-1 / C-2 的「每个用户都会看到」是推断**：我实测的是（a）受控复现（同语义）+（b）启动第二个实例时的真机观察 +（c）用户实例日志里的 9 次 reg 探测 / 1 次 taskkill。唯一没做到的是「在用户自己那一次启动的瞬间同步录像」——那次启动发生在 2026-10-12 00:25:49，早于本次审计。
2. **默认终端不是 Windows 终端的机器**：窗口类会变成 ConsoleWindowClass（黑框）。机制相同、可见性相同，但我没有第二台机器验证外观。
3. **板面 url 卡片的 target=_blank 被吞掉**（#14）：结论来自 wry 0.55.1 源码（未设 handler 时 args.SetHandled(true)）+ Tauri 2.11.5 只在 on_new_window 被显式设置时才装 handler（tauri-2.11.5/src/webview/mod.rs:585-591），而 QIO 代码里没有 on_new_window。**我没有在真实 UI 里点过这张卡片**，属【推】。
4. **卸载器里的 helper 是否完全无窗口**（#31）属【推】：nsExec::ExecToStack（NSIS 官方插件）本身以隐藏方式执行并捕获输出，helper 是 console 程序（PE 已验），它自己的 taskkill 会继承同一条隐藏控制台。没有真跑一次卸载来录像（会动用户的安装，越界）。
5. **合成 WM_CLOSE 后第二个实例没有退出**：§6.4 观察到的现象。可能与我发的是 WM_CLOSE（不是用户点关闭按钮的同一条路径）有关，也可能是真问题。**不是本次窗口审计要回答的问题，建议单独立项复核**（涉及 updater / 退出路径）。
6. **WebView2 下载提示**（#13）：downloadBlob 会触发 WebView2 的下载行为；Tauri 未配置 download handler 时，WebView2 可能在**应用内**显示下载浮层。这不算「本体以外的窗口」，我没有实测确认它的形态。
7. scripts/* 与 backend/scripts/* 里还有大量开发脚本会起进程（pytest、uv、uvicorn、fake provider…），它们都在开发者终端里跑，不产生独立窗口；我没有逐行审所有脚本，只按「是否会新开窗口」的调用形态（Start-Process / Popen / &）扫了一遍。

---

## 8. 附录：窗口采样 / 监听脚本（本次实测用的原样方法）

### 8.1 精确版：WinEvent 顶层窗口事件流（能抓 10ms 级闪现）

把下面内容存成 WinWatch.cs，用 Add-Type -Path 加载；本机实测放在 %TEMP% 下的 qio-win-audit 目录。

~~~csharp
// WinWatch.cs —— 顶层窗口 CREATE/SHOW/HIDE/DESTROY 事件流 + 可见窗口快照
using System; using System.Collections.Generic; using System.Diagnostics;
using System.Runtime.InteropServices; using System.Text; using System.Threading;
public static class WinWatch {
  delegate void WinEventDelegate(IntPtr h, uint ev, IntPtr hwnd, int idObject, int idChild, uint tid, uint t);
  delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] static extern IntPtr SetWinEventHook(uint min, uint max, IntPtr mod, WinEventDelegate cb, uint pid, uint tid, uint flags);
  [DllImport("user32.dll")] static extern bool UnhookWinEvent(IntPtr h);
  [DllImport("user32.dll")] static extern bool PeekMessage(out MSG m, IntPtr h, uint min, uint max, uint remove);
  [DllImport("user32.dll")] static extern bool TranslateMessage(ref MSG m);
  [DllImport("user32.dll")] static extern IntPtr DispatchMessage(ref MSG m);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] static extern IntPtr GetAncestor(IntPtr h, uint flags);
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);
  [StructLayout(LayoutKind.Sequential)] public struct MSG { public IntPtr hwnd; public uint message; public IntPtr wParam; public IntPtr lParam; public uint time; public int ptX; public int ptY; }
  const uint CREATE=0x8000, DESTROY=0x8001, SHOW=0x8002, HIDE=0x8003, OUTOFCONTEXT=0, GA_ROOT=2, PM_REMOVE=1;
  public static long T0 = Stopwatch.GetTimestamp();
  static string Stamp() { return ((Stopwatch.GetTimestamp()-T0)*1000.0/Stopwatch.Frequency).ToString("F0").PadLeft(7)+"ms"; }
  static string PName(uint pid) { try { return Process.GetProcessById((int)pid).ProcessName; } catch { return "?"; } }
  static void OnEvent(IntPtr hook, uint ev, IntPtr hwnd, int idObject, int idChild, uint tid, uint t) {
    if (hwnd == IntPtr.Zero || idObject != 0 || idChild != 0) return;
    if (GetAncestor(hwnd, GA_ROOT) != hwnd) return;
    uint pid; GetWindowThreadProcessId(hwnd, out pid);
    var c = new StringBuilder(256); GetClassNameW(hwnd, c, c.Capacity);
    var ti = new StringBuilder(512); GetWindowTextW(hwnd, ti, ti.Capacity);
    string kind = ev==CREATE?"CREATE":ev==SHOW?"SHOW":ev==HIDE?"HIDE":"DESTROY";
    Console.WriteLine(string.Format("{0} {1,-7} vis={2,-5} pid={3,-7} proc={4,-16} class={5,-30} title={6}",
      Stamp(), kind, IsWindowVisible(hwnd), pid, PName(pid), c.ToString(), ti.ToString()));
  }
  // 监听 N 秒（期间另开窗口跑你想观察的操作）
  public static void Watch(double seconds) {
    T0 = Stopwatch.GetTimestamp();
    var del = new WinEventDelegate(OnEvent);
    IntPtr h = SetWinEventHook(CREATE, HIDE, IntPtr.Zero, del, 0, 0, OUTOFCONTEXT);
    var sw = Stopwatch.StartNew(); MSG m;
    while (sw.Elapsed.TotalSeconds < seconds) { while (PeekMessage(out m, IntPtr.Zero, 0, 0, PM_REMOVE)) { TranslateMessage(ref m); DispatchMessage(ref m); } Thread.Sleep(3); }
    UnhookWinEvent(h); GC.KeepAlive(del);
  }
  // 监听 + 启动一个进程（不经过 shell），用来复现「某父进程拉起某子进程」的窗口行为
  public static void WatchRun(double seconds, string exe, string args) {
    T0 = Stopwatch.GetTimestamp();
    var del = new WinEventDelegate(OnEvent);
    IntPtr h = SetWinEventHook(CREATE, HIDE, IntPtr.Zero, del, 0, 0, OUTOFCONTEXT);
    var p = Process.Start(new ProcessStartInfo(exe) { UseShellExecute = false, Arguments = args ?? "" });
    Console.WriteLine(Stamp() + " SPAWNED pid=" + p.Id + " " + exe + " " + args);
    var sw = Stopwatch.StartNew(); MSG m;
    while (sw.Elapsed.TotalSeconds < seconds && !p.HasExited) { while (PeekMessage(out m, IntPtr.Zero, 0, 0, PM_REMOVE)) { TranslateMessage(ref m); DispatchMessage(ref m); } Thread.Sleep(3); }
    Console.WriteLine(Stamp() + " EXITED code=" + (p.HasExited ? p.ExitCode.ToString() : "still-running"));
    UnhookWinEvent(h); GC.KeepAlive(del);
  }
  // 当前可见顶层窗口快照（做前后对比用）
  public static List<string> Snapshot() {
    var rows = new List<string>();
    EnumWindows(delegate(IntPtr h, IntPtr l) {
      if (!IsWindowVisible(h)) return true;
      uint pid; GetWindowThreadProcessId(h, out pid);
      var c = new StringBuilder(256); GetClassNameW(h, c, c.Capacity);
      var t = new StringBuilder(512); GetWindowTextW(h, t, t.Capacity);
      rows.Add("pid=" + pid + " proc=" + PName(pid) + " class=" + c + " title=" + t);
      return true;
    }, IntPtr.Zero);
    return rows;
  }
}
~~~

~~~powershell
# 1) 只看启动期：先开一个 PowerShell 窗口跑监听，再双击 QIO
Add-Type -Path .\WinWatch.cs
[WinWatch]::Snapshot() | ForEach-Object { "SNAP $_" }
[WinWatch]::Watch(30)   # 30 秒内所有顶层窗口 CREATE/SHOW/HIDE/DESTROY 都打印出来

# 2) 只测某条命令的窗口行为（复现产品里 Command::new(...) 的效果）
[WinWatch]::WatchRun(15, 'C:\Windows\System32\reg.exe', 'query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable')
[WinWatch]::WatchRun(15, 'C:\Windows\System32\explorer.exe', '/select,D:\QIO\qio.exe')
~~~

要点：
* 一定要用 SetWinEventHook（事件流）而不是轮询 Get-Process：reg 的窗口只活 200–400ms，轮询很容易漏。
* 想复现「GUI 父进程 → console 子进程」，宿主本身必须是 GUI 子系统进程（Add-Type -OutputType WindowsApplication 编一个小 exe），否则它自带控制台，子进程会继承，看不到窗口。

### 8.2 快速版（临时看一眼可见窗口）

~~~powershell
Get-Process | Where-Object { $_.MainWindowHandle -ne 0 } |
  Select-Object Id, ProcessName, MainWindowTitle | Sort-Object ProcessName | Format-Table -AutoSize
# QIO 的进程链路
Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'qio|conhost' } |
  Select-Object ProcessId, ParentProcessId, Name | Format-Table -AutoSize
~~~

### 8.3 一句话复现清单

| 想复现哪条 | 怎么做 |
| --- | --- |
| C-1 启动闪现 | 清空 HTTPS_PROXY、删掉数据目录下的 updater-proxy.txt，开着 §8.1 监听，双击 qio.exe；看 0–3s 与 10–13s |
| C-2 taskkill 闪现 | 开着监听，在 QIO 里点更新「下载并安装」；或按 §6.2 用受控宿主跑 taskkill /PID 某pid /T /F |
| A-1 原生选择器 | 点「附件」；让 pick_attachment_file 失败可看到第二条入口 |
| A-2 / A-3 打开与定位 | 加一个 .pdf（打开）与一个 .exe（自动降级为在文件夹中显示） |
| A-4 外链 | 让回答里带 https 链接并点击 |
| B-1 启动失败框 | QIO_STARTUP_ERROR_DIALOG=0 可关；把数据目录设为不可写可看到它 |
| A-5 多实例 | 同一安装目录再启动一次 qio.exe（各写自己的 sidecar.lease.<pid>.json） |
