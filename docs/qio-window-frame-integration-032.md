# QIO 0.3.2 窗口框架集成规格（T1 · 代码级）

> 状态：**定稿（Lead 复核版 v2，2026-11-12）**。本文件是 T2/T3/T4/T5 的实现依据；所有 file:line 均按 **feat/window-frame-032 分支 HEAD 082f881f**（基于 main = v0.3.1 线）核对，spike 代码按 `D:\qio-dev\qio-window-spike\src-tauri\src` 当前快照（overlay.rs 900 行/passthrough.rs 264 行等）逐函数精读。
> 写入范围：本文件是 qio-recon 在 QIO 仓库的**唯一**写入文件；不修改其他任何文件、不触碰工作树中两个与本任务无关的既有修改（`docs/acc/acc-f-f11-events.json`、`scripts/sr-verify/output/responsiveness_event_loop_gate.json`）。
> Lead 定稿决定（已写入全文）：① css 模式 **默认 poll 穿透开启**，`QIO_PASSTHROUGH=none|poll` 可关；`forward.rs` 0.3.2 不接（不新增命令，仅留扩展点）；② overlay 类名 `QioWindowOverlay`（过滤两处同步改）；③ interactive holes 必须实现；④ 程序化建窗 + `tauri.conf` `windows:[]`；⑤ dist 目录 **`D:\qio-dev\dist-0.3.2-release`**；⑥ 版本 7 处 + `docs/releases/v0.3.2.md`；gate 用 `scripts/release_gate.py`。

---

## 0. 结论摘要（T2/T3 可直接开工的 8 条）

1. **建窗**：`tauri.conf.json` 的 `app.windows` 改为 `[]`；在 `main.rs` setup 里用 `WebviewWindowBuilder` 程序化创建 `main` 窗（css：transparent+decorations:false+shadow:false+visible:false；native：decorations:true+transparent:false+shadow:true+backgroundColor；dwm=0.3.1 等价），页面 `PageLoadEvent::Finished` 后 `show()`（有界兜底 2s）。
2. **模式**：`QIO_WINDOW_FRAME=css|dwm|native`（默认 css）；`WindowsBuild < 22000` 自动强制 native（无系统圆角的 Win10 不回退也能看，但 0.3.2 的验收基线要求 Win11；见 §7/§9）。
3. **命中**：顶层 WM_NCHITTEST 实测不被派发（WebView2 子窗覆盖客户区）→ 用 **overlay 子窗**：`QioWindowOverlay` 类、`WS_CHILD|WS_VISIBLE|WS_CLIPSIBLINGS`、无扩展样式、NULL_BRUSH；创建/布局必须在 tauri 主线程；press 期间 defer sync；每次 sync 重设 `HWND_TOP`；1 max + N titlebar(HTCAPTION) + 8 resize 环。
4. **titlebar 段 = 拖拽区 − maxButton − min/close − interactive**（设置入口/权限模式/异常入口/互动挂载点必须挖掉，保持 DOM 可点）。spike 只挖 max/otherButtons，QIO 必须新增 `interactive` 洞（`HitRegions.interactive`）。
5. **穿透**：css 模式默认 `poll`（8ms 轮询 + `WS_EX_LAYERED|WS_EX_TRANSPARENT` 加/撤 + WebView2 子窗递归 2 级 + `SetLayeredWindowAttributes(0,255,LWA_ALPHA)`）；`QIO_PASSTHROUGH=none` 关闭；`forward_click` 0.3.2 不接。
6. **版本**：7 处版本号（见 §6.1）+ `docs/releases/v0.3.2.md`；构建（Lead 定稿：**不改 `run_build.ps1`**）= 设置 `TAURI_SIGNING_PRIVATE_KEY_PATH`/`TAURI_SIGNING_PRIVATE_KEY_PASSWORD` 后直接调 `scripts\build_installer.ps1 -DistDir D:\qio-dev\dist-0.3.2-release`；gate `python scripts/release_gate.py --installer <path> --json <gate-result.json> --no-history`（参数以 `--help` 为准）。
7. **必须先改两份冲突契约测试**（§1.4）：`design-guards.test.ts`（decorations:false/shadow:true）与 `window-outline-native.test.ts`（transparent 不为 true、backgroundColor=--bg-base、`.app-shell` 不许 border-radius、main.rs 必须含 0.3.1 DWM 调用）；否则实现一动 CI 必红。
8. **回退可观测**：css 初始化任何一步失败 → 记录结构化失败原因并用 native 重建窗口（同一进程内不能改透明属性，必须销毁重建或启动前决定）；测试钩子 `QIO_WINDOW_TEST_FAIL_CSS=1`（仅测试，文档标注）；运行期日志必须能区分 mode/各步结果/overlay 数量/passthrough 切换次数。

---

## 1. QIO 现状代码地图（feat/window-frame-032，file:line）

### 1.1 仓库/分支

- 检出：`D:\qio-dev\qio`；分支 `feat/window-frame-032`；HEAD `082f881f0b0b77e41f7b49dc5c22e212bd556dae`（2026-11-11 22:31，标题 `fix(updater): 更新端点改为 GitHub 直连优先…`）→ 基于 v0.3.1 线。
- 工作树既有修改（**不要动**）：`M docs/acc/acc-f-f11-events.json`、`M scripts/sr-verify/output/responsiveness_event_loop_gate.json`；另有未跟踪 `_lead-logs/*`。
- 依赖版本（Cargo.lock）：tauri 2.11.5 / tao 0.35.3 / wry 0.55.1 / tauri-runtime-wry 2.11.4 / webview2-com 0.38.2；直接 `windows-sys 0.61.2`（其余 0.45/0.52/0.59/0.60.2 为传递依赖）。

### 1.2 Rust 壳现状（`frontend/src-tauri/src/main.rs`，125,670 bytes）

| 位置 | 现状 | 0.3.2 动作 |
|---|---|---|
| `tauri.conf.json:13-24` | 窗口静态创建：title QIO、1200×800、min 800×600、`decorations:false`(20)、`shadow:true`(21)、`backgroundColor:#171015`(22) | 改为 `windows: []`，全部由 builder 创建 |
| `main.rs:2249` `fn main()` | Builder 链：plugin(:2333-2337)、`.setup`(:2338-2395)、`.on_window_event`(:2401-2418)、`.invoke_handler`(:2419-2437)、build/run(:2438-2443) | setup 内先建窗；invoke_handler 增加 `qio_*` 命令；run 回调增加清理 |
| `main.rs:2377` `app.manage` | 运行时状态注册 | 保持在建窗之后、托盘/后端之前 |
| `main.rs:2380-2385` | `get_webview_window("main")` → `apply_windows_window_outline` | 替换为 `window_frame::outline::apply`（三模式） |
| `main.rs:2387-2391` | `setup_tray` | 不变（托盘创建不依赖窗口句柄，只依赖 `app.default_window_icon()` :2122） |
| `main.rs:2393` | `start_backend_in_background` | 不变；仍放在窗口/托盘之后 |
| `main.rs:1903-1960` | `apply_windows_window_outline`：DWMWCP_ROUND + BORDER_COLOR_NONE（0.3.1 §六） | 拆成 `window_frame/outline.rs` 三模式 |
| `main.rs:2401-2418` | `on_window_event` 只处理 `CloseRequested`（prevent_close + 线程 handle_shell_close） | **完全保留**，不改 |
| `main.rs:2009-2063` | `handle_shell_close`（ask/tray/quit 壳侧执行） | **完全保留** |
| `main.rs:2142-2225 / 2229-2239 / 2244-2247` | `request_close` / `qio_set_close_behavior` / `qio_close_ack` | 保留，不动 |
| `main.rs:1962-1993` | `show_main_window` / `hide_main_window`（托盘恢复/隐藏） | 保留；恢复后需触发 state 刷新 + overlay 重排（核 §4.4） |
| `main.rs:2096-2130` | `setup_tray` | 保留 |
| `main.rs:1966/1984/2027/2044/2077/2171/2195/2382` | 8 处 `get_webview_window("main")` | label 必须仍为 `main`（builder 创建时显式 `"main"`）；这些调用点是窗口存在性依赖的完整清单 |
| `ownership.rs:931-946` | 向外壳 pid 的顶层窗口 `PostMessageW(WM_CLOSE)` | 不受影响（仍能枚举到主窗） |
| `ownership.rs:984-1026` | `EnumWindows` 找壳顶层窗口/类名 | 不受影响 |
| `Cargo.toml:71-84` | windows-sys features：Foundation/JobObjects/Threading/ToolHelp/UI_WindowsAndMessaging/UI_Controls_Dialogs/Graphics_Dwm | **追加** `Win32_UI_Shell`、`Win32_UI_HiDpi`、`Win32_System_LibraryLoader`、`Win32_Graphics_Gdi`、`Win32_UI_Input_KeyboardAndMouse` |
| `capabilities/default.json` | 唯一 capability：`windows:["main"]`(:5)、permissions(:6-17)，含 5 个窗口权限(:12-16) | 不需要新增权限（自定义命令不走 ACL）；窗口 label 必须保持 `main` |

### 1.3 前端现状（关键锚点）

| 位置 | 现状 | 0.3.2 动作 |
|---|---|---|
| `App.vue:101-105` | `.app-shell` 根 + `<WindowChrome />` | 外包 `.window-frame` + `.window-frame__surface` |
| `App.vue:152` | `.app-shell{width:100vw;height:100vh;overflow:hidden;padding-top:var(--titlebar-height)}` | 改为相对 surface 的 100%/inset，保留 padding-top |
| `App.vue:144-149` / `styles/base.css:2-5` | `:root`/`body` 背景 `var(--bg-base)` | css 模式透明，实底移到 surface |
| `styles/tokens.css:29-32` | `--titlebar-height:32px` 唯一来源 | 保留；新增 frame 令牌 |
| `styles/tokens.css:35/40/104-120` | 圆角 `--r-xl:20px`、`--qio-ui-radius:14px`、阴影令牌 | 新增 `--window-frame-pad:28px`、`--window-radius:20px`、`--window-shadow`（soft·A，暗/亮两套） |
| `WindowChrome.vue:63-64` | `GLOBAL_BAR_TITLEBAR_HEIGHT="70px"`（对话/互动路由） | 不变；上报时按实际 rect |
| `WindowChrome.vue:485` | `.drag-surface` `data-tauri-drag-region` | 保留；上报其 rect 作为 titlebar 基础矩形 |
| `WindowChrome.vue:486-515` | 3 个窗口按钮（min 487-495 / max 496-504 / close 505-514） | 保留 DOM；max 上报矩形给 overlay |
| `WindowChrome.vue:539-558/563-569` | 设置入口 + 权限模式 + 全局异常入口（interactive） | 上报 interactive rects；native 模式隐藏 window-controls |
| `WindowChrome.vue:127-134/424-434` | `syncMaximized()` / `onResized` 同步 | 保留；增加 window-state 监听 |
| `WindowChrome.vue:655-668` | 窗口层 z-index 240（>模态 200） | 保留 |
| `WindowChrome.vue:844-860` | 对话/互动全局栏：52px 卡片、top/left/right:8、radius 20px | 保留 |
| `WindowChrome.vue:993-1004/1111-1129/1150-1166` | 全窗遮罩/失败条/退出条 fixed | 遮罩 `inset: var(--window-frame-inset)` |
| `frontend/src-tauri/capabilities/default.json:12-16` | 5 个窗口权限 | 不变 |

### 1.4 两份冲突契约测试（**必须先改写**）

| 文件 | 现断言（行号） | 冲突点 |
|---|---|---|
| `frontend/src/design-guards.test.ts` | :163-168 `tauri.conf.json` 窗口 `decorations===false` 且 `shadow===true`；:170-182 5 个窗口权限；:184-203 标题条令牌/padding-top；:205-223 拖拽面与按钮结构 | 窗口改 builder 创建 + css 模式 `transparent:true/shadow:false` → :163-168 必红；其余保留 |
| `frontend/src/components/__tests__/window-outline-native.test.ts` | :33-42 `transparent` 不为 true、`backgroundColor === --bg-base`；:44-60 main.rs 必须含 `Win32_Graphics_Dwm`、5 个 DWM 符号、`#[cfg(windows)] fn apply_windows_window_outline`、setup 调用；:62-66 `.app-shell` 不含 border-radius | 与 CSS 圆角/透明窗/三模式直接冲突，整份改为「双模式守卫」 |

建议改写方向：测试读一个**单一模式源**（如 `src-tauri/src/window_frame/config.rs` 的常量或 `tauri.conf.json` 的 `windows:[]` + builder 参数表），按 css/native 分支断言；css 断言「frame surface 有 border-radius、DWM DONOTROUND、overlay 命中存在」；native 断言「decorations:true/shadow:true、无 overlay」。

---
## 2. spike 实现精读与移植映射（`D:\qio-dev\qio-window-spike\src-tauri\src\`）

### 2.1 `main.rs`（375 行）——建窗与启动序列

- `RuntimeConfig::from_env()`（:39）→ `WebviewWindowBuilder::new(app, "main", WebviewUrl::App(index.html?query))`（:160-174）：`.title/.inner_size/.min_inner_size(200,160)/.decorations(cfg.decorations())/.transparent(cfg.transparent())/.shadow(cfg.shadow())/.visible(!show_on_load)`；非透明模式才 `.background_color(Color(23,16,21,255))`（:168-170）。
- `on_page_load`（:179-240）：`Started` 记录；`Finished` → `shared.page_loaded=true`；`show_on_load` 时 `window.show()` + `SetForegroundWindow`；随后 emit `spike://ready`（runtime payload）+ `state::refresh_and_emit(force)`。
- 建窗后（:242-264）：存 hwnd + window 到 Shared；`show_on_load=0` 时立即 show。:266-320 记录 style/exStyle（css 模式打印 WS_CAPTION/WS_THICKFRAME 等位，用于证明非客户区来源）。
- **顺序**：`outline::apply`（:322）→ `wndproc::install`（:335）→ `overlay::ensure+sync`（:348-349）→ `passthrough::apply_mode`（:351）→ 首次 `refresh_and_emit`（:353）。QIO 移植必须保持：DWM → subclass → overlay 类注册/首布局 → poll 启动。
- `app.run` 回调（:96-131）只在 `RunEvent::Exit` 打印 hit-test 统计（top-level NCHITTEST 次数=0 的实证）与退出日志；**没有 stop_poll/destroy_all 的显式清理**（spike 是实验程序，QIO 必须补，见 §2.7.6）。

### 2.2 `config.rs`（274 行）——模式与默认值

- 三枚举：`WindowFrame{Css,Native,Dwm}`（:7-30）、`HitTestMode{Transparent,None}`、`TitlebarMode{Native,Dom}`（默认 **Native**）、`ResizeMode{Custom,Tao,None}`（默认 **Custom**）、`PassthroughMode{None,Ht,Poll}`（默认 **None**）。
- 常量：`DEFAULT_PADDING_CSS=28.0`、`DEFAULT_RADIUS_CSS=20.0`、`DEFAULT_RESIZE_BORDER_PHYSICAL=8.0`（:143-145）。
- 派生：`decorations()=frame==Native`、`transparent()=frame==Css`、`shadow()=frame!=Css`、`default_padding_css()/default_radius_css()` 仅透明模式非 0（:187-215）。
- QIO 版差异：env 前缀改 `QIO_`；**css 模式 passthrough 默认 `Poll`**（Lead 定稿），`QIO_PASSTHROUGH=none` 关闭；其余默认同 spike。

### 2.3 `outline.rs`（136 行）——DWM 三模式

- `apply(shared)`：native 直接返回（不设置，:56-65）；`SPIKE_DWM=off` 可跳过（:66-75，QIO 不需要该开关）；css→`DWMWCP_DONOTROUND`、dwm→`DWMWCP_ROUND`（:77-80）；两个 `DwmSetWindowAttribute` 都记录 name/value/HRESULT（:82-125）。
- QIO：`format_hresult` 可复用；失败只 `log::debug` 的现有语义保留（`main.rs:1943-1945/1957-1959`）。

### 2.4 `overlay.rs`（900 行）——命中核心（T2 的主件）

**设计原因（源码 :3-15）**：页面在 WebView2 子窗里、覆盖整个客户区 → 顶层永远收不到 WM_NCHITTEST；唯一拿到真实非客户区命中的办法是在目标区域叠一个原生子窗。样式必须 `WS_CHILD|WS_CLIPSIBLINGS|WS_VISIBLE`，**无扩展样式**（`WS_EX_LAYERED` 会丢命中、`WS_EX_TRANSPARENT` 会让窗口命中穿透），`hbrBackground=NULL_BRUSH`，WM_PAINT 空实现。

关键实现点（行号按当前 snapshot）：

- 类/全局（:46-83）：类名 `QioSpikeOverlayWindow`；常量 `KIND_HIDDEN=0 / KIND_MAX=1 / KIND_TITLEBAR=2 / KIND_RESIZE_BASE=0x100`（命中码=低字节偏移）；`OVERLAY_SHARED` OnceLock；`MAX_PRESSED/MAX_HOVER/TB_PRESSED/TB_DRAGGING/TB_FORWARDED` 原子；`TB_LAST_CLICK` 手工双击判定。
- 注册/创建（:208-233 / :551-576）：`RegisterClassExW`（`hbrBackground=NULL_BRUSH`、`hCursor=null`、`style=0`——注意 **无 CS_DBLCLKS**，所以双击靠手工判定）；`CreateWindowExW(0, class, ..., WS_CHILD|WS_CLIPSIBLINGS|WS_VISIBLE, 0,0,0,0, parent, ...)`。
- **去洞**（:235-304）：`subtract_rect(base,hole)` 把一个洞从矩形切成最多 4 块；`clamp_rect` 夹进 content。
- **resize 环**（:306-350）：`resize_ring(content, band)` 在**可见内容边内侧**生成 8 块：4 角 band×band（13=HTTOPLEFT/14=HTTOPRIGHT/16=HTBOTTOMLEFT/17=HTBOTTOMRIGHT）+ 4 条边（10=HTLEFT/11=HTRIGHT/12=HTTOP/15=HTBOTTOM）；`band<1` 或内容 <8 时返回空。
- **sync（:352-549）**：`ensure` → **press 期间 defer**（`MAX_PRESSED||TB_PRESSED` 时直接 return，注释 :356-358；release 处理器再调 request_sync）→ geometry/regions/client/padding/content → 生成 desired：① maxButton clamp 后 KIND_MAX；② `titlebar==Native` 时 `regions.titlebar` 依次减去 maxButton+otherButtons 的每个洞（:385-408），每段 clamp；③ `resize==Custom && state==normal` 时 resize 环。→ 复用/懒创建 slot（:440-454）→ 未变 slot 也 `SetWindowPos(..., HWND_TOP, ..., SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE)`（:460-475，**每次 sync 重新压到 WebView2 之上**）→ 变化 slot `SetWindowLongPtrW(GWLP_USERDATA,kind)` + `SetWindowPos(HWND_TOP, rect, SWP_NOACTIVATE|SWP_SHOWWINDOW)`（:477-491）→ 多余 slot `ShowWindow(SW_HIDE)`（:492-499）→ 布局 digest 变化才写 `overlay-layout.jsonl`（:502-548）。
- **hover**（:614-630）：`emit_caption_hover` 用 `MAX_HOVER` 去重后 emit `spike://caption-hover{target:"max",hover,ts}` 并写 `caption-hover.jsonl`；`max_rect_changed && MAX_HOVER` 时发 hover=false（:421-436）。
- **最大化点击**（:632-660）：`queue_toggle_maximize` 计数 + `run_on_main_thread` 调 `commands::toggle_maximize_shared` + `refresh_and_emit(force)`。
- **NCLBUTTONDOWN 转发**（:664-693）：释放 capture 后 `PostMessageW(main_hwnd, WM_NCLBUTTONDOWN, hit_code, packed_cursor)`，与 tao `drag_window` 的做法一致（:663 注释）。
- **WndProc**（:702-900）：`WM_NCHITTEST` → kind 命中码（:736-748）；`WM_SETCURSOR` → 尺寸光标（:749-769）；`WM_NCLBUTTONDOWN|WM_LBUTTONDOWN`（:770-793）：max→SetCapture+MAX_PRESSED；titlebar→`titlebar_press`（SetCapture、记录按下点、等移动 ≥4px 才 ReleaseCapture 并 forward HTCAPTION 拖动，:93-121）；resize→forward_nc_press(WM_NCLBUTTONDOWN)；`WM_NCLBUTTONUP|WM_LBUTTONUP`（:794-821）：max→release+toggle、titlebar→release+双击判定（`GetDoubleClickTime`、≤4px，:123-150）→ 双击则 toggle；`WM_NCLBUTTONDBLCLK|WM_LBUTTONDBLCLK`（:822-844）：max/titlebar→toggle（手工双击的补充路径）；`WM_NCMOUSEMOVE|WM_MOUSEMOVE`（:845-861）：titlebar 移动/拖动；max → caption-hover=true + `TrackMouseEvent(TME_LEAVE|TME_NONCLIENT)`；`WM_NCMOUSELEAVE|WM_MOUSELEAVE`（:862-869）→ hover=false；`WM_CAPTURECHANGED`（:870-885）**只记日志不清 press 状态**（注释：SetWindowPos/hover 可能移走 capture）；`WM_ERASEBKGND`→1、`WM_PAINT` 空绘制、`WM_NCDESTROY`→DefWindowProc。
- **销毁**（:578-589）：`destroy_all()` 遍历 slots `DestroyWindow` 并 clear（主窗 WM_NCDESTROY/退出时调用）。
- **QIO 移植必修**：① 类名 → `QioWindowOverlay`，`passthrough.rs:179` 的过滤同步改（两处）；② `titlebar` 去洞列表加入 `interactive`（`HitRegions` 新增字段）；③ 已知问题：窗口 move/resize/状态变化后若 sync 迟到，**左上 strip 出现 1px 命中/右下正常**（Lead 实测）——接入时先用真实光标逐点扫描（SendMessage 不可信），必要时在 WM_WINDOWPOSCHANGED 后强制同步重排/延长 defer 逻辑测试。

### 2.5 `wndproc.rs`（411 行）——主窗 subclass（QIO 只保留非命中职责）

- `install()`（:32-60）：`SetWindowSubclass(hwnd, subclass_proc, SUBCLASS_ID=0x51494F45, 0)`；`GLOBAL` OnceLock 保存 Shared。**必须在 tao subclass 之后安装**（后装先调用）；未处理消息一律 `DefSubclassProc`（:75）。
- `handle()`（:78-202）：`WM_NCHITTEST`→`hit_test`（:86，保留作为兜底/日志；真实命中由 overlay 负责）；`WM_NCLBUTTONDOWN/UP/DBLCLK` 的 HTMAXBUTTON 处理（:87-136，overlay 存在后为兜底，保留但不应计数为主路径）；`WM_WINDOWPOSCHANGED|WM_SIZE`→`queue_refresh`+`overlay::request_sync`（:137-141）；`WM_DPICHANGED`→更新 dpi + queue_refresh + overlay sync（:142-165）；`WM_ENTERSIZEMOVE`→`in_size_move=true`+`passthrough::ensure_interactive`（:166-171）；`WM_EXITSIZEMOVE`→false+queue_refresh（:172-177）；`WM_ACTIVATE|WM_SETFOCUS` 特意不重排（注释 :178-183，避免打断 overlay press）；`WM_SHOWWINDOW`→queue_refresh（:184-191）；`WM_NCDESTROY`→`overlay::destroy_all()`+`RemoveWindowSubclass`（:192-199）。
- `hit_test()`（:291-411）：native 直接 fallthrough（:316-325）；其余按 otherButtons→maxButton→titlebar→resize→transparent-band→client 顺序给码，并写 hit-test.log（:377-410）。QIO 移植时可大幅简化（overlay 就位后真实命中不经过这里），但保留计数/日志用于验证「顶层 NCHITTEST 仍为 0」。

### 2.6 `state.rs`（572 行）——共享状态/几何/事件

- `Shared`（:19-42）关键字段：`inner{hwnd,window,dpi,subclass_installed,created_ms}`（:44-50）、`regions`、`last_emit`、`passthrough_mode`、`poll_transparent`、`poll_stop`、`poll_handle`、`in_size_move`、`refresh_queued`、`page_loaded`、`shown`、`overlay_sync_queued`、`forwarding` 等。
- 几何：`client_rect`（:314-325）、`geometry()`（:327-376：GetWindowRect + GetDpiForWindow + MonitorFromWindow/GetMonitorInfoW + IsZoomed → state/zone/scale）、`detect_zone()`（:380-440：±2 物理 px 容差，quarters→halves/thirds→stacked halves）、`effective_padding_radius()`（:442-461：非 normal 返回 0）、`content_rect()`（:463-470）、`contains_rounded()`（:269-300：圆角矩形内判定，供 poll 用）。
- payload/事件：`WindowStatePayload`（:152-183，camelCase）、`eq_ignoring_ts`（:169-183 去重）、`refresh_and_emit()`（:513-552：变化或 force 才 emit + 追加 `window-state.jsonl`）、`queue_refresh()`（:554-572：`refresh_queued` 去抖 + run_on_main_thread）。
- QIO 移植：保留 API 形状，事件名 `spike://window-state`→`qio://window-state`；jsonl 证据写入 QIO 日志目录（如 `<QIO_DATA_DIR>/logs/window-frame-*.jsonl`，或 debug 模式才写）。

### 2.7 `passthrough.rs`（264 行）——poll 穿透逐函数（用户特别点名，完整版）

**总体**：`SPIKE_PASSTHROUGH=poll` 时起 8ms 轮询线程，读取物理光标；光标在「可见圆角内容矩形」之外（阴影带/圆角外）时给主窗加 `WS_EX_TRANSPARENT|WS_EX_LAYERED`（并递归给 WebView2 子窗加 `WS_EX_TRANSPARENT`）让点击落到底层窗口；回到内容区立即撤掉。每次切换写 `spike.log` + `timeline.jsonl`（事件名 `passthrough-toggle`）。

#### 2.7.1 `apply_mode(shared, mode)`（:27-56）
1. 读旧模式 `shared.passthrough()`；不同则 `set_passthrough(mode)` 并写 timeline `passthrough-mode`；相同只 debug。
2. `Poll`→`start_poll(shared)`；`None|Ht`→`stop_poll(shared)` + `set_transparent(false,"mode-change")`（保证关掉后立刻可交互）。

#### 2.7.2 `start_poll(shared)`（:58-94）
1. 锁 `poll_handle`：已有线程且 `poll_stop==false` → 直接返回（幂等）；否则 `poll_stop=false`。
2. `thread::Builder::new().name("spike-passthrough-poll").spawn`：循环 `while !poll_stop { poll_tick(); sleep(8ms) }`；退出循环后 `set_transparent(false,"poll-thread-stop")` 并记 info。
3. 成功 → 保存 `poll_handle` + timeline `passthrough-poll-started{intervalMs:8}`；spawn 失败 → warn（不 panic）。

#### 2.7.3 `stop_poll(shared)`（:96-105）
1. `poll_stop=true`；2. `poll_handle.take()` → `join()`（**阻塞等待线程退出**，线程退出时会强制 `set_transparent(false)`）；3. timeline `passthrough-poll-stopped`。

#### 2.7.4 `poll_tick(shared)`（:113-149，每 8ms 一次）——判定点完整顺序
1. `hwnd==0` → return（窗口未就绪）。
2. **`shared.forwarding` 为真 → return**（click-forward 注入期间不许 poll 抢样式；QIO 0.3.2 不接 forward，但字段/判断保留）。
3. `shared.in_size_move` 为真 → `set_transparent(false,"size-move")` 并 return（拖动/缩放期间强制交互，避免拖动中样式抖动）。
4. `IsWindowVisible(hwnd)==0 || IsIconic(hwnd)!=0` → `set_transparent(false,"window-hidden")` 并 return（托盘隐藏/最小化时恢复可交互；不写样式再计算）。
5. `ClientToScreen(hwnd,&origin)` 失败 → return；`GetCursorPos(&cursor)` 失败 → return。
6. 取 `state::geometry`、`regions`（clone）、`client_rect`、`effective_padding_radius`（非 normal 时 padding/radius=0）→ `content=content_rect(client,padding)`。
7. `local=(cursor.x-origin.x, cursor.y-origin.y)`（客户区物理坐标）→ `content.contains_rounded(local_x,local_y,radius)` → `interactive`。
8. `set_transparent(shared, !interactive, "cursor")`：光标在可见圆角矩形内 → `want=false`（交互）；在阴影带/圆角外 → `want=true`（穿透）。

#### 2.7.5 `set_transparent` / `set_transparent_for_hwnd`（:196-264）——样式加/撤与子窗递归
1. `hwnd==0` return；读 `exStyle`；`current = exStyle & (WS_EX_TRANSPARENT|WS_EX_LAYERED) != 0`；`poll_transparent.store(current)`。
2. `current==want` → **直接 return（无系统调用、无日志）**——稳定状态每 tick 只做一次 GetWindowLongPtr。
3. 计算新样式：want → OR 两个位；!want → AND NOT 两个位；`SetWindowLongPtrW(GWL_EXSTYLE)`。
4. want 时 `SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)`（**保持窗口不透明**：只改命中，不改视觉）。
5. `SetWindowPos(hwnd, null, 0,0,0,0, SWP_NOMOVE|SWP_NOSIZE|SWP_NOZORDER|SWP_NOACTIVATE|SWP_FRAMECHANGED)`（让样式立即生效）。
6. `set_webview_child_transparent(main_hwnd, want)` → 返回改变的子窗数。
7. `poll_transparent.store(want)`；写 info：`transparent ON (WS_EX_TRANSPARENT|WS_EX_LAYERED) reason=... exStyle 0x... -> 0x... webviewChildrenChanged=N`；写 timeline `passthrough-toggle{ts,transparent,reason,exStyleBefore,exStyleAfter,webviewChildrenChanged}`。

#### 2.7.6 `set_webview_child_transparent` / `apply_child_transparent`（:151-194）——**关键：子窗必须一起处理**
1. 注释（:151-154）：WebView2 子窗独立命中，只给父窗加 WS_EX_TRANSPARENT 不会让点击穿透。
2. 遍历：`GetWindow(main,GW_CHILD)` 一层 → 对每个子窗 `apply_child_transparent`；再用 `GetWindow(child,GW_CHILD)` 对**孙窗**做一遍（共 2 级；WebView2 的宿主/Chrome_WidgetWin_* 在这一层）。
3. `apply_child_transparent`（:172-194）：`GetClassNameW` → **类名 `QioSpikeOverlayWindow` 或 `TAURI_DRAG_RESIZE_BORDERS` 直接跳过**（前者必须保留命中，后者由 tao 管理）→ 读 exStyle → 已符合则返回 0 → OR/AND NOT `WS_EX_TRANSPARENT` → `SetWindowLongPtrW` → 返回 1。
4. QIO 移植：类名改 `QioWindowOverlay`（两处过滤必须同步：`overlay.rs` 类名 + 本函数白名单）；`TAURI_DRAG_RESIZE_BORDERS` 类名以真机 `GetClassName` 核对（tauri 2.11.5/tao 0.35.3 若改名，两处一起改）。

#### 2.7.7 `ensure_interactive(shared, reason)`（:107-111）
只调 `set_transparent(false, reason)`：给 `WM_ENTERSIZEMOVE`/poll 线程关闭时强制恢复交互（wndproc.rs:168 调用）。

#### 2.7.8 与 forward / UI 的条件互斥

- `forwarding`：`forward_click` 开始时 `swap(true)`（forward.rs:77-79），结束时 `store(false)`（:83）；`poll_tick` 看到 forwarding 直接 return（:119-121）。forward 的 `inject_click` 自己临时加/撤 `WS_EX_TRANSPARENT|WS_EX_LAYERED` 并递归子窗（forward.rs:174-214），与 poll 的样式操作是同一套位——若同时动作会互相覆盖，这是 forwarding 互斥的原因。
- `in_size_move`：`WM_ENTERSIZEMOVE` 置真并 `ensure_interactive`，`WM_EXITSIZEMOVE` 置假（wndproc.rs:166-177）→ poll 下一次 tick 重新接管。
- UI 不直接控制 poll；spike 另有 `spike_set_passthrough` 命令（commands.rs:160-175）可在运行期切 none/ht/poll。QIO 保留 `qio_set_passthrough` 仅供验收/诊断（默认 poll 由启动配置决定）。

#### 2.7.9 清理点（spike 缺、QIO 必须补）

| 场景 | spike 现状 | QIO 0.3.2 必须做 |
|---|---|---|
| 正常退出 `RunEvent::Exit` | main.rs:96-131 只打日志 | `passthrough::stop_poll`（join）→ `overlay::destroy_all`；顺序在窗口销毁前/后均可，但 stop_poll 必须先于窗口 Destroy |
| 主窗 `WM_NCDESTROY` | wndproc.rs:192-199 只 destroy overlay | `stop_poll` + `destroy_all` + `RemoveWindowSubclass`（即使退出路径已做，幂等） |
| 隐藏到托盘（QIO `hide_main_window` main.rs:1982-1993） | poll_tick 检测 IsWindowVisible → 强制交互 | 保持；另建议 hide 时主动 `ensure_interactive` 防 1 个 tick 间隙 |
| 最小化 | IsIconic → 强制交互 | 保持 |
| 拖动/缩放 | WM_ENTERSIZEMOVE → 强制交互 | 保持 |
| 关闭行为 ask 弹框期间 | 无特殊处理 | 弹框是 WebView DOM，窗口仍可见；poll 保持（光标在内容区=交互）；无额外动作 |
| 更新器重启（process::restart） | 进程重启，线程随进程销毁 | 进程退出路径已 stop_poll 即可；无文件锁 |
| 第二实例 | 进程级独立 | 无共享状态；overlay 只属于本进程窗口 |
| 主窗重建（若回退路径销毁重建） | 无 | 先 stop_poll/destroy overlay/subclass 卸载，再重建；重建后重新 install/ensure/apply_mode |

#### 2.7.10 已知坑清单（写进 T2 实现注意）

1. **8ms 轮询 = 125 次/秒**：稳定态只有 `GetCursorPos`+`ClientToScreen`+`GetWindowLongPtr`（样式相同时第 2.7.5.2 步直接 return，无 Set）；CPU 可接受，但轮询线程名/优先级不要提升。
2. **WS_EX_LAYERED 的副作用**：窗口被标为 layered 后 DWM 合成路径改变，透明窗/CSS 阴影/WebView2 重绘可能变化；必须真机看（验收 B）；`SetLayeredWindowAttributes(0,255,LWA_ALPHA)` 是保持不透明的关键，漏掉会整窗隐形。
3. **子窗递归只做 2 级**：WebView2 更深层若还有窗口，需要真机枚举验证（建议 debug 日志打印所有 child 类名一次）。
4. **类名白名单**：overlay 类必须跳过（否则 max/hover 失效）；tao 的 resize border 类必须跳过（否则系统缩放失效）；改名/升级 tauri 时两处同步。
5. **in_size_move 期间强制交互**：否则拖动中光标扫到留白会瞬间穿透 → 拖动丢失。
6. **隐藏/最小化强制交互**：否则恢复窗口瞬间点击落不到窗口。
7. **join 阻塞**：`stop_poll` 会 join 8ms 线程，最坏 ~8ms；不要在窗口消息线程里长时间等待（放在 Exit/后台线程可接受）。
8. **poll 与 overlay 的 press 状态**：poll 改的是主窗/子窗 exStyle，不动 overlay 窗；但 `SetWindowPos(SWP_FRAMECHANGED)` 可能触发窗口重绘/布局变化，若与 overlay press 重叠会打断 press——QIO 里 `WM_ENTERSIZEMOVE` 强制交互只覆盖 resize/drag；overlay 按钮 press 期间光标在内容区（不会触发 want=true），风险低但需真机确认。
9. **HTTRANSPARENT 路径已证伪**：不要指望顶层 WM_NCHITTEST 返回 HTTRANSPARENT 实现跨进程穿透（wndproc.rs:4-9 实证注释：top-level NCHITTEST 不被派发）；poll 是唯一已验证路径。

### 2.8 `forward.rs`（256 行）——0.3.2 **不接**，只留扩展点

- 逻辑：`find_target_window` 从 z 序顶开始枚举可见、非本进程的顶层窗并判断点是否在 `GetWindowRect` 内（:120-152）；`inject_click` 临时给主窗+子窗加 `WS_EX_TRANSPARENT|WS_EX_LAYERED`，sleep 35ms 让样式生效，`SendInput` 绝对移动（`GetSystemMetrics(SM_CXSCREEN/CYSCREEN)` 换算 65535，:216-234）+ 20/30/35ms sleep 左/右键 down/up，再恢复样式（:174-214）；`forwarding` 原子防重入；结果写 `click-forward.jsonl`（:104）。
- 限制：只按主屏尺寸换算（多屏负坐标/混合 DPI 不可靠）；SendInput 移动真实光标（用户可见）；UIPI 下无法点击管理员窗口；阻塞调用期间 120ms+。
- **0.3.2 决策**：不新增 `qio_forward_click` 命令、不启用 forward；文档标注为「poll 不够用时的下一阶段扩展点」，实现时保留 `forwarding` 字段/判断位置即可。

### 2.9 `commands.rs`（175 行）——IPC 命令清单

`spike_window_action`(:23-68)、`toggle_maximize_shared`(:70-81)、`spike_set_hit_regions`(:83-117)、`spike_probe`(:119-134)、`spike_forward_click`(:140-148，不接)、`spike_get_state`(:150-153)、`spike_get_runtime`(:155-158)、`spike_set_passthrough`(:160-175)。QIO 版改名 `qio_*` 并删除 probe/forward（probe 可保留 debug-only）。

### 2.10 移植差异总表（T2 必读）

| spike | QIO 0.3.2 |
|---|---|---|
| env `SPIKE_*` | `QIO_WINDOW_FRAME / QIO_PASSTHROUGH / QIO_UI_QUERY?`（仅保留必要项） |
| `QioSpikeOverlayWindow` | `QioWindowOverlay`（overlay.rs + passthrough.rs 两处） |
| 默认 passthrough None | **css 默认 Poll**，`QIO_PASSTHROUGH=none` 关闭 |
| titlebar 洞 = max+otherButtons | **+ interactive**（QIO 新增） |
| evidence 目录 D:\qio-dev\qio-window-spike\evidence | QIO 日志目录（`<QIO_DATA_DIR>/logs/`），jsonl 仅 debug/验收模式写 |
| main 窗 1200×800/200×160 最小 | 1200×800 / **800×600**（min） |
| spike:// 事件/命令 | qio:// 事件、qio_* 命令 |
| 无退出清理 | 补齐 §2.7.9 |
| overlay 已知 1px 迟滞 | 真机光标扫描验收；必要时每次 WM_WINDOWPOSCHANGED 强制同步 |

---
## 3. 集成契约（shell ↔ 前端，QIO 版）

### 3.1 模式与环境变量

| 变量 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `QIO_WINDOW_FRAME` | `css` / `dwm` / `native` | `css` | 启动时读一次；运行期不切换；日志记录生效值 |
| `QIO_PASSTHROUGH` | `none` / `poll` | **`poll`（仅 css 模式生效）** | `none` 关闭穿透轮询；dwm/native 强制 none |
| `QIO_WINDOW_TEST_FAIL_CSS` | `1` | 未设置 | **仅测试用**：模拟 css 初始化失败 → 走 native 回退路径并记录失败原因；发布构建默认忽略（`cfg!(debug_assertions)` 或显式文档标注） |
| `QIO_UI_QUERY`（可选） | 页面 query | 未设置 | 调试/验收用，如 `theme=light` |

- 模式选择：`windows_build < 22000` → 强制 `native`（无系统圆角，回退保护；日志注明）；`QIO_WINDOW_FRAME` 只在 build ≥22000 生效。
- 失败回退（任一）：builder 创建失败 / transparent 设置失败 / DWM 调用失败且 `QIO_WINDOW_TEST_FAIL_CSS=1` / WebView2 创建失败 → 记录 `[qio] window-frame css-init-failed reason=...` → **销毁已建窗口**，以 `native` 参数重建（同一进程只能重建，不能改透明属性）。

### 3.2 事件（Rust → JS）

| 事件 | payload | 触发 |
|---|---|---|
| `qio://window-state` | `{state:"normal"|"maximized"|"snapped", zone:string|null, rect:{x,y,w,h}, workArea:{x,y,w,h}, dpi:number, scaleFactor:number, padding:number, radius:number, frame:"css"|"native"|"dwm", passthrough:"none"|"poll"|"ht", titlebar:"native"|"dom", ts:number}` | 启动一次（force）+ 每次几何/状态/DPI/模式变化（去重后） |
| `qio://caption-hover` | `{target:"max", hover:boolean, ts:number}` | overlay 覆盖最大化按钮期间 hover 进出（去重） |
| `qio://frame-ready` | runtime payload（同 `qio_get_window_frame`） | 页面加载完成、壳完成建窗/outline/overlay 初始化后一次 |

- 像素约定：`rect/workArea/padding/radius` 全部**物理像素**（padding/radius = CSS×dpi/96）；`dpi` 为窗口 DPI（本机 192）。
- 现有事件不冲突：QIO 已有 `qio://close-requested` / `qio://quit-requested`（`services/desktop.ts:37-56` 监听），新增事件用独立名字。

### 3.3 命令（JS → Rust）

| 命令 | 参数 | 返回 | 用途 |
|---|---|---|---|
| `qio_set_hit_regions` | 见 §3.4 | `{ok}` | 上报 overlay 布局输入；前端 mount/resize/onResized/state/路由变化后 rAF 合并发送 |
| `qio_get_window_frame` | — | `{frame,titlebar,pad,radius,resizeBorder,dpi,state,passthrough}` | 启动读一次，决定 DOM 行为/隐藏窗口按钮 |
| `qio_window_action` | `{action:"minimize"|"toggle_maximize"|"maximize"|"unmaximize"|"close"|"start_drag"|"set_foreground"}` | `{ok,state?}` | 可选（overlay 内部直接调；DOM 路径可继续用 `@tauri-apps/api/window`） |
| `qio_set_passthrough` | `{mode:"none"|"poll"}` | `{ok,mode}` | 验收/诊断用；产品 UI 不暴露 |

- 权限：自定义命令不走 capability；**不需要新增** capabilities 权限（`capabilities/default.json` 保持 windows:["main"] + 现有 10 项权限）。
- 现有前端直连的窗口 API（minimize/toggleMaximize/close/isMaximized/onResized）全部**保留可用**；最大化鼠标点击由 overlay 接管，键盘 Enter 仍走 DOM（见 §3.5）。

### 3.4 `qio_set_hit_regions` 结构（物理像素、窗口客户区坐标）

```json
{ "dpr": 2, "framePadding": 56, "radius": 40, "resizeBorder": 8, "state": "normal",
  "titlebar": {"x":56,"y":56,"w":2288,"h":104},
  "maxButton": {"x":2160,"y":56,"w":92,"h":102},
  "otherButtons": [{"x":2068,"y":56,"w":92,"h":102}, {"x":2252,"y":56,"w":92,"h":102}],
  "interactive": [{"x":1900,"y":56,"w":160,"h":104}],
  "cornerRadius": 40 }
```

| 字段 | 来源（前端） | 语义 |
|---|---|---|
| `dpr/framePadding/radius/resizeBorder/cornerRadius` | `devicePixelRatio` + CSS 令牌 | 物理像素；`framePadding` 用于 poll/overlay 的 content rect |
| `state` | `qio://window-state` 最新值 | normal/maximized/snapped；壳在非 normal 时不生成 resize 环 |
| `titlebar` | `.drag-surface` 的 `getBoundingClientRect()*dpr` | HTCAPTION 基础矩形（QIO 含整个全局栏） |
| `maxButton` | 最大化按钮 rect | `HTMAXBUTTON` |
| `otherButtons` | 最小化/关闭按钮 rect | 从 titlebar 中挖掉（保持 DOM click） |
| `interactive`（**QIO 新增**） | 设置入口/权限模式文字/全局异常入口/互动挂载点 rect 的并集 | 从 titlebar 中挖掉（保持 DOM hover/click） |

- 前端实现位置：`WindowChrome.vue`（`getBoundingClientRect` 已有先例）→ 新增 `services/windowFrame.ts` 节流上报；`onResized`（:424-434）与 window-state 监听触发。
- 启动早期未上报：壳不生成 max/titlebar overlay（仅 resize 环可按几何生成），DOM 拖动/按钮仍可用，不阻塞启动。

### 3.5 WindowChrome 对接点（file:line）

| 位置 | 现状 | 0.3.2 动作 |
|---|---|---|
| `WindowChrome.vue:424-461` `onMounted` | `getCurrentWindow()` + `isMaximized` + `onResized` + listenShellEvent | 增加 `qio_get_window_frame`、`qio://window-state`、`qio://caption-hover` 监听；首报 hit regions |
| `:127-134` `syncMaximized` | isMaximized 轮询式同步 | 保留；`maximized` 图标同时参考 window-state.state（snapped 口径待产品定） |
| `:485` `.drag-surface` | DOM 拖拽 | 保留（overlay 就位后 HTCAPTION 接管该区域；空白/启动早期仍可 DOM 拖动） |
| `:486-515` 三按钮 | DOM click | 最小化/关闭保持 DOM；最大化鼠标点击由 overlay 的 HTMAXBUTTON 接管（DOM @click 保留给键盘/浏览器/native 模式） |
| `:539-558/563-569` interactive 控件 | DOM | 上报 interactive rects；native 模式仍显示（native 用系统标题栏，DOM 按钮隐藏） |
| `:993-1004` 关闭遮罩 | fixed inset:0 | 改 `inset: var(--window-frame-inset)` |
| `:1111-1129/1150-1166` 失败条/退出条 | fixed top/bottom | 加 `var(--window-frame-inset)` 偏移 |
| `:655-668` z-index 240 | 保留 | 保留（overlay 是原生窗，不在 DOM 层级里，但视觉上在标题条之上；max hover 由事件补） |

- 关闭协议（ask/tray/quit、ACK 兜底、托盘）**完全不变**（`main.rs:2009-2063/2142-2247`、`services/desktop.ts`）。

---

## 4. 壳侧改动清单（T2 执行；逐文件/函数/行号）

### 4.1 `frontend/src-tauri/Cargo.toml`

- :71-84 windows-sys features 追加（不新增 crate，Cargo.lock 的 features 不进锁文件）：`Win32_UI_Shell`（SetWindowSubclass/DefSubclassProc/RemoveWindowSubclass）、`Win32_UI_HiDpi`（GetDpiForWindow/WM_DPICHANGED）、`Win32_System_LibraryLoader`（GetModuleHandleW）、`Win32_Graphics_Gdi`（ClientToScreen/GetStockObject/BeginPaint/EndPaint）、`Win32_UI_Input_KeyboardAndMouse`（GetCursorPos/SetCursorPos/SetCapture/ReleaseCapture/TrackMouseEvent/GetDoubleClickTime/SetCursor/LoadCursor）。
- 版本行 :3 改 0.3.2（§6）。

### 4.2 `frontend/src-tauri/tauri.conf.json`

- :13-24：`app.windows` → `[]`（窗口由 builder 创建）；`version` :4 改 0.3.2。其余（build/frontendDist/security/bundle/plugins）**不动**。

### 4.3 `frontend/src-tauri/capabilities/`

- 目录下只有 `default.json`（485 bytes）；`windows:["main"]` 不变（builder 创建时 label 必须是 `main`）；permissions 现有 10 项不变；**不新增** `allow-show`（show 由壳侧 `PageLoadEvent::Finished` 触发）。
- 核对：`design-guards.test.ts:170-182` 会检查 5 个窗口权限存在 → 保持。

### 4.4 `frontend/src-tauri/src/main.rs`（改动点）

| 行号 | 改动 |
|---|---|
| :2249 `fn main()` | 启动最早处 `let frame_cfg = window_frame::config::FrameConfig::from_env_and_build();`；日志一行记录 mode/passthrough/pad/radius/build/dpi |
| :2338-2395 `.setup` | 顺序改为：① 注册日志插件（现有 :2339-2370 不动）→ ② `install_direct_fallback_guard()`（:2375 不动）→ ③ **建窗**（新 `window_frame::create_main_window(app, &frame_cfg)`：builder 创建 main + `on_page_load` show 握手 + 兜底计时）→ ④ `app.manage`（:2377）→ ⑤ `window_frame::outline::apply`（替换 :2380-2385）→ ⑥ `window_frame::wndproc::install` → ⑦ `window_frame::overlay::ensure+sync` → ⑧ `window_frame::state::init_and_emit` → ⑨ `setup_tray`（:2387-2391 保留）→ ⑩ `start_backend_in_background`（:2393 保留） |
| :2401-2418 `.on_window_event` | **不改**（CloseRequested 逻辑保留）；新增的窗口事件在 wndproc subclass 里处理，不用 Tauri WindowEvent 重复处理 |
| :2419-2437 `.invoke_handler` | 追加 `window_frame::commands::qio_set_hit_regions / qio_get_window_frame / qio_window_action / qio_set_passthrough`；现有命令全部保留 |
| :2440-2443 `.run(|app,event|)` | `RunEvent::Exit | ExitRequested` → 现有 `shutdown_runtime(&runtime)` 之外，追加 `window_frame::shutdown(app)`（stop_poll → destroy overlay → log 汇总） |
| :1962-1980 `show_main_window` | show/unminimize/focus 后追加 `window_frame::state::queue_refresh("tray-show")` + `overlay::request_sync` |
| :1982-1993 `hide_main_window` | hide 成功后追加 `passthrough::ensure_interactive("tray-hide")`（防 1 tick 间隙） |
| :1903-1960 `apply_windows_window_outline` | 删除/改为 `window_frame::outline::apply`（三模式）；失败日志沿用 `log::debug` |
| `ownership.rs` / `attachment_picker.rs` / `backend_lifecycle.rs` | **不改**（与窗口层无耦合；ownership 仍能 EnumWindows 找到主窗） |

**时序核对结论**（Lead 要求的第 4 点）：
- `setup_tray` 在 `app.handle()` 上创建，仅依赖 `default_window_icon()`（:2122），不要求 main 窗口已存在 → 建窗提前不影响。
- `start_backend_in_background`（:1796）只用 `AppHandle`+runtime，不用窗口 → 顺序可保持在建窗之后。
- `ownership` 的 WM_CLOSE 兜底（`ownership.rs:931-946`）在**另一实例退出流程**里枚举顶层窗，主窗一直是顶层窗口（label/类名不变）→ 不受 builder 创建影响。
- 8 处 `get_webview_window("main")`（§1.2）在 setup 之后才调用（tray/close/quit 路径），builder 创建后均可用；唯 :2382 是旧 DWM 调用点，被替换。

### 4.5 新增模块（`frontend/src-tauri/src/window_frame/`）

| 文件 | 内容 | 来源 | 预计行数 |
|---|---|---|---|
| `mod.rs` | `pub mod config; pub mod outline; pub mod overlay; pub mod passthrough; pub mod state; pub mod wndproc; pub mod commands;` + `create_main_window/shutdown` | 新写 | 150-250 |
| `config.rs` | FrameConfig/FramMode/TitlebarMode/ResizeMode/PassthroughMode/常量（28/20/8） | 移植 spike config.rs | ~200 |
| `outline.rs` | DWM 三模式 + HRESULT 日志 | 移植 | ~130 |
| `overlay.rs` | 11 overlay 子窗 + 去洞 + press/sync/hover/双击 | 移植（类名/interactive 改） | ~900 |
| `wndproc.rs` | 主窗 subclass（状态/几何/尺寸移动/DPI/NCDESTROY） | 移植并简化命中 | ~250 |
| `state.rs` | Shared/geometry/zone/padding/content/contains_rounded/emit | 移植 | ~450 |
| `passthrough.rs` | poll 穿透逐函数（§2.7） | 移植（默认 poll，清理点补齐） | ~300 |
| `commands.rs` | qio_* 命令 | 移植/改名 | ~150 |

- 代码量合计约 2,500 行（含注释）；T2 可整体复制 spike 模块后按 §2.10 差异表修改，避免重写。
- 日志：所有模块用 `log::info!` 前缀 `[qio] window-frame ...`，沿用 QIO 日志插件落点（`<QIO_DATA_DIR>/logs/qio.log`）。

### 4.6 可观测性（日志/诊断必须能复现问题）

启动日志（一次，INFO）：`[qio] window-frame mode=css passthrough=poll pad=28 radius=20 resizeBorder=8 dpi=192 build=26200 framePadPx=56 radiusPx=40 overlayClass=QioWindowOverlay`。
关键事件（INFO）：css-init-failed（含 reason + 回退 native）、DWM 每个属性的 HRESULT、overlay sync（数量/是否 defer）、passthrough 每次切换（reason/exStyle before→after/webviewChildrenChanged）、overlay 点击 toggle（via=overlay）、窗口状态变化（state/zone/rect/workArea）。
debug 级：hit-test 决策、hit-regions 上报、overlay 消息（可开关）。
验收模式可把 `window-state.jsonl / overlay-layout.jsonl / passthrough-toggle.jsonl / caption-hover.jsonl / hit-test.log` 写到 `<QIO_DATA_DIR>/logs/window-frame/`（仅 `QIO_WINDOW_EVIDENCE=1` 时启用，避免产品常态写盘）。

### 4.7 失败回退 / 回退开关

| 场景 | 行为 |
|---|---|
| `build < 22000` | 强制 native，日志 INFO |
| `QIO_WINDOW_FRAME=native` | 完整系统窗框（decorations:true/transparent:false/shadow:true/backgroundColor），不创建 overlay、不启 poll；前端 `html[data-window-frame=native]` 隐藏 DOM 窗口按钮 |
| `QIO_WINDOW_FRAME=dwm` | 0.3.1 等价（decorations:false/shadow:true/ROUND/border none），不透明、无留白、不创建 overlay/poll；用于回归对照 |
| css 初始化失败 | `QIO_WINDOW_TEST_FAIL_CSS=1` 或真实失败 → 销毁窗口 → native 重建 → 事件/window-state 报 frame=native；日志含失败点 |
| poll 线程启动失败 | 记录 warn，退化为 none（点击留在窗口，不穿透）；不影响其他功能 |
| overlay 创建失败 | 记录 warn，退化为「无 overlay 命中」（DOM 拖动 + tao 缩放可用）；window-state 仍发 |

---
## 5. 前端改动清单（T3 执行；逐文件 file:line）

### 5.1 根结构 / 令牌 / 透明背景

| 位置 | 改动 |
|---|---|
| `index.html` / `App.vue:144-149` / `styles/base.css:2-5` | css 模式 `html/body/:root` 背景透明；实底移到 surface |
| `App.vue:151-152` | 新增 `.window-frame`（fixed inset:0，padding `var(--window-frame-pad)`，pointer-events:none）+ `.window-frame__surface`（height:100%，radius/shadow/overflow:hidden/背景 `var(--bg-base)`，pointer-events:auto）；`.app-shell` 改 100%/inset，保留 `padding-top:var(--titlebar-height)` |
| `styles/tokens.css:29-32` 后 | 新增 `--window-frame-pad:28px; --window-radius:20px; --window-shadow:<soft·A 暗>; --window-frame-inset:var(--window-frame-pad)`；亮色主题 alpha×0.5；`html[data-window-frame="native"|"dwm"]` 与 `html[data-window-state="maximized"|"snapped"]` 归零 pad/radius/shadow/inset |
| 新增 `styles/window-frame.css`（或并入 base.css） | `.window-chrome` 等顶部锚定层加 inset；全窗层 `inset: var(--window-frame-inset)`（避免逐组件写死） |

### 5.2 全窗 fixed 层（11 处，必须内缩 `var(--window-frame-inset)`）

| # | 组件/选择器 | file:line | 说明 |
|---|---|---|---|
| 1 | `.qio-modal-scrim` | `styles/base.css:315` | 中央弹窗遮罩 |
| 2 | `.qio-confirm-scrim` | `styles/base.css:340` | 确认层遮罩 |
| 3 | `.ua-mask` | `components/approval/UnifiedApprovalModal.vue:1195` | 统一审批弹窗遮罩 |
| 4 | `.restore-scrim` | `components/planet/MaintenanceRestoreDialog.vue:310` | 维护撤销对话框 |
| 5 | `.onboarding` | `components/onboarding/OnboardingWizard.vue:615` | 首次引导全屏层 |
| 6 | `.ic-scrim` | `components/interactive/v2/ImpactConfirmLayer.vue:734` | 互动影响确认 |
| 7 | `.close-ask-scrim` | `components/WindowChrome.vue:994` | 关闭确认（z 高于标题条） |
| 8 | `.conversation-workspace::before` | `views/ConversationView.vue:621` | 整窗背景层（z -1） |
| 9 | `.planet-view` | `views/PlanetView.vue:1676` | 星球页整层（含背板） |
| 10 | `.planet-boot` | `components/PlanetBoot.vue:20` | 星球懒加载占位 |
| 11 | `.qio-tip-layer` | `components/ui/QTipLayer.vue:94` | 全局提示层（pointer-events:none；内部 `.qio-tip` :100 fixed 定位也要按内容视口 clamp） |

- 全窗遮罩同时加 `border-radius: var(--window-radius)`；native/dwm 下 inset=0 行为同 0.3.1。

### 5.3 `innerWidth/innerHeight` + `100vh/100dvh`（12 处 JS + 高度上限）

| # | 位置 | 现状 | 动作 |
|---|---|---|---|
| 1-4 | `composables/useFloatingWindow.ts:120/248/406/443` | `window.innerWidth/innerHeight` 作为可用视口（初始/贴边/拖动/resize 钳制） | 改内容视口（inner − 2×inset）；新增 `utils/windowFrame.ts` 统一 |
| 5 | `components/ui/QTipLayer.vue:42` | `computeTipPosition(..., {innerWidth,innerHeight})` | 内容视口 |
| 6 | `components/interactive/v2/InteractiveChromeTools.vue:143` | `placeChromePopover(anchor, {innerWidth,innerHeight})` | 内容视口（`chromePlacement.ts:58-88` 纯函数不改） |
| 7 | `components/interactive/SelectionMenu.vue:163` | `clipBox` 初始框 = innerHeight/innerWidth | 内容视口，再与裁剪祖先求交 |
| 8-9 | `views/InteractiveView.vue:48/53` | `innerHeight - 306` 底聊最大高度 | 减 2×inset |
| 10 | `components/interactive/v2/InteractiveConversation.vue:98` | `viewportHeight()=innerHeight` | 减 2×inset |
| 11 | `stores/conversationShell.ts:101` | `viewportWidth=innerWidth` | 内容宽（或注释保持窗口宽） |
| 12 | `components/MessageStream.vue:599` | `viewportWidth: innerWidth`（测量/缓存键） | 先核查影响面，再改内容宽 |
| 13 | `views/SettingsView.vue:62` | `narrow = innerWidth <= 900` | 内容宽（桌面 minWidth 800，影响小） |
| 14 | `components/feedback/feedbackOffsets.ts:11-23` | `feedbackTopPx = --titlebar-height + 12 + notes`（从窗口顶算） | 加 inset |

**100vh/100dvh 高度上限**：`App.vue:152`（根 100vw/100vh）、`components/ui/QModal.vue:326/345`（`max-height:calc(100vh - 2*sp6)`）、`components/DevTaskEntry.vue:343/345`（`100vh/100dvh - offset - reserve`）、`DevTaskEntry.vue:346`/`QTipLayer.vue:102`/多处 `calc(100vw - …)`（宽度上限）→ 改按内容区（`100vh - 2*pad` 或父级 100%）。
**不需要改**：`getBoundingClientRect` 系（145 处：composerMetrics/useQioTip/readingAnchor/interactive viewport/overlayPlacement/BoardCanvas 等）；`composerMetrics.ts:115` 的 `visualViewport` 只是 resize 事件源；`position:absolute` 相对父级系（PlanetView content 等）。

### 5.4 QIO 真实浮层清单（file:line，验收 C 要逐层看）

| 类别 | 组件/选择器 | file:line | 三态（normal/snapped/max）注意 |
|---|---|---|---|
| 锚点菜单/浮层 | QTipLayer（全局 500ms 悬停提示） | `components/ui/QTipLayer.vue:42/94/100` | clamp 改为内容视口 |
| 锚点菜单 | SelectionMenu（卡片整理菜单） | `components/interactive/SelectionMenu.vue:163` | clipBox 改内容视口 |
| 锚点浮层 | 互动顶栏搜索/保存弹层 | `components/interactive/v2/InteractiveChromeTools.vue:130-151` + `chromePlacement.ts:58-88` | 输入 viewport 改内容视口 |
| 锚点菜单 | QModal / QConfirm（Teleport body） | `components/ui/QModal.vue:326/345`、base.css:301-344 | 高度上限改内容区 |
| 顶部提示 | TopNotice / GlobalIssuePanel / `.top-notes` | `components/feedback/TopNotice.vue:240`、`GlobalIssuePanel.vue:189`、`App.vue:157-165` | 顶部偏移加 inset |
| 审批浮层 | UnifiedApprovalLayer 通知栈 | `components/conversation/UnifiedApprovalLayer.vue:314` | 顶部偏移加 inset |
| 审批遮罩 | UnifiedApprovalModal `.ua-mask` | `components/approval/UnifiedApprovalModal.vue:1195` | 全窗内缩 + 圆角 |
| 引导层 | OnboardingWizard | `components/onboarding/OnboardingWizard.vue:615` | 全窗内缩 + 圆角 |
| 关闭确认 | WindowChrome `.close-ask-scrim` | `components/WindowChrome.vue:994` | 全窗内缩 + 圆角 |
| 维护/影响确认 | MaintenanceRestoreDialog / ImpactConfirmLayer | `...MaintenanceRestoreDialog.vue:310`、`...ImpactConfirmLayer.vue:734` | 全窗内缩 + 圆角 |
| 退出/失败条 | `.chrome-failure` / `.close-busy` | `components/WindowChrome.vue:1112/1151` | top/bottom 加 inset |
| 星球入口/星球页 | PlanetDock（useFloatingWindow）/ PlanetView / PlanetBoot | `components/PlanetDock.vue:118`、`views/PlanetView.vue:1676`、`components/PlanetBoot.vue:20` | 浮动球不落留白带；星球层内缩 |
| 待办/任务 | DevTaskEntry 面板 | `components/DevTaskEntry.vue:324-352` | max-height 改内容区 |

### 5.5 WindowChrome 增量（T3）

- `onMounted`（:424-461）增加：`qio_get_window_frame`、listen `qio://window-state`、listen `qio://caption-hover`；`qio_set_hit_regions` 首报。
- 上报函数：量 `.drag-surface`、最大化按钮、最小化/关闭按钮、`interactive` 控件（设置按钮 `button[aria-label^="设置"]`、`.chrome-mode`、GlobalIssueEntry 容器、`#qio-interactive-chrome-extra`）→ `getBoundingClientRect()*devicePixelRatio` → rAF 合并 → invoke。
- `caption-hover` → 给最大化按钮加 `.is-hover`（overlay 覆盖后 DOM hover 失效）。
- `maximized` 图标来源：`state==="maximized"` 或原 `isMaximized()`；snapped 口径由产品定（建议还原图标，验收记录）。
- native/dwm：`html[data-window-frame]` 分支隐藏 `.window-controls`（:486）；DOM 全局栏视觉保留。
- 不改关闭协议、不改标题条高度切换（:159-180）。

### 5.6 测试改写（T3 与 T2 同步做）

- 重写 `design-guards.test.ts:163-168`（按模式断言）与 `window-outline-native.test.ts:33-66`（双模式：css 有 frame surface border-radius + DWM DONOTROUND + overlay；native=0.3.1 行为）；保留 :170-182 权限、:184-203 令牌、:205-223 拖拽面断言。
- `WindowChrome.test.ts` 补：window-state/caption-hover 监听、hit-regions 上报参数、native 隐藏按钮。
- 新增前端守卫：11 处全窗层必须含 `var(--window-frame-inset)`；`useFloatingWindow` 等 12 处不得直接使用裸 `innerWidth/innerHeight`（改为 `utils/windowFrame`）。
- 后端/其它测试不受影响；`scripts/check_docs.py` 会检查三处里程碑状态一致（T4 改 docs 时注意）。

---

## 6. 版本 0.3.2 与构建/发布（T4）

### 6.1 版本号 7 处（照 v0.3.1 release commit fc658ebd 的模式）

| # | 文件:行 | 现状 | 改为 |
|---|---|---|---|
| 1 | `frontend/src-tauri/tauri.conf.json:4` | `"version": "0.3.1"` | `0.3.2` |
| 2 | `frontend/src-tauri/Cargo.toml:3` | `version = "0.3.1"` | `0.3.2` |
| 3 | `frontend/src-tauri/Cargo.lock`（`name = "qio"` 块 version 行） | `0.3.1` | `0.3.2`（构建后 cargo 会重写；手工改也行） |
| 4 | `frontend/package.json:4` | `"version": "0.3.1"` | `0.3.2` |
| 5 | `backend/pyproject.toml:3` | `version = "0.3.1"` | `0.3.2` |
| 6 | `backend/src/agent/__init__.py:3` | `__version__ = "0.3.1"` | `0.3.2` |
| 7 | `backend/src/agent/api/server.py:686` | `FastAPI(title="QIO", version="0.3.1")` | `0.3.2` |

- 发布说明：新建 `docs/releases/v0.3.2.md`（参考 `docs/releases/v0.3.1.md`，94 行；内容：窗口框架重写/三模式/20px CSS 圆角/poll 穿透/回退开关/已知边界）。
- 版本一致性：`release_gate.py` 会核对 latest.json 与 tauri.conf 版本；`python scripts/check_docs.py` 三处里程碑状态一致。

### 6.2 构建命令与顺序

- 带签名入口（**Lead 定稿：不改 `run_build.ps1`**）：T4 自行设置 `TAURI_SIGNING_PRIVATE_KEY_PATH`（默认 `<检出>\..\dist\qio-updater.key`）与 `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`，然后直接调用 `powershell -File scripts\build_installer.ps1 -DistDir "D:\qio-dev\dist-0.3.2-release"`——与 `run_build.ps1` 所做的事等价，只是省掉参数转发，避免动发布脚本。
- `build_installer.ps1` 顺序（脚本头注释 :1-29）：fetch_model（校验 sha256）→ build_runtime（Python runtime）→ build_sidecar（后端，必须在打包前）→ build_uninstall_helper（在 sidecar 之后）→ 真产物校验 → `tauri build`（含 .sig）→ 校验签名产物 + 生成 latest.json + 复制到 dist + 更新 SHA256SUMS.txt。
- **dist 目录**：脚本 `DistDir` 默认 `Join-Path (Split-Path $root -Parent) "dist"`（:178-181）= `D:\qio-dev\dist`；本阶段目标 **`D:\qio-dev\dist-0.3.2-release`** → T4 按 Lead 定稿**显式传 `-DistDir "D:\qio-dev\dist-0.3.2-release"`**（不通过 run_build.ps1，也不改发布脚本）。
- 无签名口令时只能用 `-UnsignedTestArtifact`（产物带 `-UNSIGNED-TEST`、不写 latest.json/.sig）——只能用于内部测试，不得发布。

### 6.3 产物与校验点

- 期望产物：`QIO_0.3.2_x64-setup.exe`、`.sig`、`latest.json`（version=0.3.2、windows-x86_64 url 指向本包、signature 为 minisign 结构）、`SHA256SUMS.txt`、`<installer>.build.json`（build_installer 写出：product/version/commit/built_at/bundles/installer sha256/sidecar sha256/updater_artifacts/signing）。
- Gate（Lead 定稿命令）：`python scripts/release_gate.py --installer <安装包路径> --json <gate-result.json> --no-history`（离线、不装；具体参数以 `--help` 为准）。核对项：安装包存在/sha256 与 SHA256SUMS 一致、latest.json 版本与 tauri.conf 一致、签名 key id 与内嵌公钥一致、**sidecar.fresh 依赖 `target/release/qio-backend.exe` 与 `binaries/qio-backend-*.exe` 逐字节一致（构建脚本保证）**、模型清单、NSIS 卸载契约、`<installer>.build.json` 身份、当前 commit。`--no-history` 表示不写历史文件；gate-result.json 落 dist 目录（与 0.3.1 同约定）。v0.3.1 的 `gate-result.json` 是 19 项 PASS/0 FAIL 的样例，0.3.2 目标同样全 PASS。
- 诚实边界：release_gate.py **不做 ed25519 验签**，只做结构/key-id 核对；发布前仍需 tauri 官方验签路径复核（v0.3.1 同样口径）。

---

## 7. A–G 验收映射（T5；A–E 与 T5 任务书一致，F/G 为穿透与记录）

| 项 | 内容 | 关联本规格 |
|---|---|---|
| A 外观/DPI | 100%/125%/200% 四角（20 CSS px→物理 20/25/40）、亮暗阴影、启动白闪（30×50ms 连拍）、无白边/黑角；运行中切 DPI 并恢复 | §1.3/§3.1/§5.1；参数见 T1 前序 spike 摘要（pad28 + soft·A） |
| B 交互 | 八方向真实拖拽缩放；阴影带/圆角外点击到达下层（poll）；快速跨越边缘；标题栏拖动/双击；最大化按钮；拖边贴靠；Win+方向；Snap 悬停+点选；贴靠/还原无桌面缝/误判/跳位 | §2.4/§2.7/§3.2-3.5 |
| C QIO 回归 | 锚点菜单、审批遮罩/引导层三态定位裁切；最小窗 800×600 内容可用；托盘隐藏/恢复；关闭行为 ask/tray/quit；首屏状态 | §5.2-§5.5 |
| D 模式与回退 | 安装版分别 `QIO_WINDOW_FRAME=css/dwm/native` 启动验证；初始化失败回退路径（`QIO_WINDOW_TEST_FAIL_CSS=1`）；日志证据 | §3.1/§4.7 |
| E 安装/升级 | 干净安装启动即 20px 圆角；0.3.1→0.3.2 升级后设置/用户数据可用 | §6 |
| F 穿透专项 | poll 开/关（`QIO_PASSTHROUGH=none|poll`）、阴影带/圆角外点击、快速跨越不误触、轮询 CPU/日志无异常、最小化/托盘/拖动时强制交互 | §2.7 |
| G 证据与边界 | 每项 PASS/FAIL/UNVERIFIED + 数值 + 截图路径；Win10/混合 DPI 无环境→UNVERIFIED；证据写 `D:\qio-dev\qio-032-evidence\acceptance\`（T5 任务书指定，禁止写 QIO 仓库） | §9 |

**UNVERIFIED 策略（Lead 定稿）**：`build < 22000`（Win10）自动 native（无系统圆角时 css 不启用）；混合 DPI 双屏本机无环境 → UNVERIFIED 并注明「build<22000 默认 native、混合 DPI 未验证」；100%/125%/200% 本机可切 200%（其它可临时切换系统缩放后恢复），切换失败则 UNVERIFIED。

---

## 8. 风险与冲突（T2/T3/T4 逐条看）

1. **两份冲突契约测试**（§1.4）必须先改，否则任何实现提交 CI 必红。
2. **overlay 1px 迟滞**（已知）：move/resize/状态变化后左上 resize strip 可能 1px 偏移（右下正常）；用真实光标逐点扫描验收，不能只信 `SendMessage`/布局 JSON；必要时每次 `WM_WINDOWPOSCHANGED` 强制 sync 并把 defer 条件收窄。
3. **interactive 洞**：QIO 标题条内有 4 类交互控件，若漏掉某类，overlay 会吞掉点击（设置/权限/异常/互动搜索保存失效）——验收 C/B 必须逐控件点。
4. **poll 与 WebView2**：`WS_EX_LAYERED` 改变合成路径，透明窗 + CSS 阴影 + WebView2 重绘可能异常（验收 B/A）；`SetLayeredWindowAttributes(0,255,LWA_ALPHA)` 漏掉会整窗不可见。
5. **poll 子窗递归 2 级**：升级 Tauri/WebView2 后子窗层级变化会让穿透失效——debug 日志打印 child 类名一次，便于回归。
6. **class 名两处同步**：`QioWindowOverlay` 与 `TAURI_DRAG_RESIZE_BORDERS`；改一处漏一处会导致 overlay 被透明化（按钮失效）或 tao 缩放边框被透明化。
7. **启动白闪/桌面闪**：builder visible:false + 页面加载完成 show；QIO 首帧是 BootStatus（非空白），但后端未就绪时页面可能长时间停留——验收 A 的 30×50ms 连拍必须覆盖「先显示 BootStatus 再显示主界面」。
8. **最小化/托盘/maximized 下的 overlay**：隐藏/恢复、最大化切 sn/还原后必须重排；验收 B/C。
9. **混合 DPI/Win10**：无环境 → UNVERIFIED；策略见 §7。
10. **双击标题条**：overlay 类 `style=0` 无 CS_DBLCLKS → 依赖手工双击判定（`GetDoubleClickTime`≤4px）；QIO 还必须保证「双击拖动面」与「单击设置按钮」不串（interactive 洞保证）。
11. **关闭协议零改动**：`request_close`/`qio_set_close_behavior`/`qio_close_ack`/`CloseRequested`/托盘/退出全部保留，回归重点验证 ask/tray/quit 三条路径（窗口层改造的唯一硬边界）。
12. **Cargo features 只加不改**：不新增 crate、不改版本（windows-sys 0.61.2 已是直接依赖）；Cargo.lock 仅因版本号变化。

---

## 9. 边界 / 未验证（最终报告必须如实写）

- 本机仅 Win11 25H2 Build 26200、单屏 200%；Win10（build<22000）、混合 DPI 双屏、RDP → UNVERIFIED（策略：build<22000 默认 native）。
- 100%/125% 缩放需临时切系统缩放；若验收环境不允许切换 → UNVERIFIED 并注明。
- `forward_click` 0.3.2 不接（无命令）；poll 是唯一已验证穿透路径；3D 星球/动画在透明窗 + poll 下的性能只在 T5 真机看。
- release_gate.py 不做密码学验签（结构/key-id 核对）；发布前手工 tauri 验签复核。
- overlay 1px 迟滞为已知未修问题：若 T2 未能修复 → 最终报告必须如实写「左上 strip 1px 命中偏移，右下正常」，并附真实光标扫描证据。

---

## 10. 交接（T2/T3/T4/T5）

1. **T2 壳侧**：按 §2（逐模块移植）+ §3（契约）+ §4（改动清单）实现；先跑通 `QIO_WINDOW_FRAME=native` 等价 0.3.1 → 再 css 无 overlay → 加 overlay → 加 poll；每步可回退。
2. **T3 前端**：按 §5 改根结构/fixed/viewport/WindowChrome/tests；与 T2 的 IPC 契约以 §3 为准（字段名/像素单位不可自行改）。
3. **T4 发布**：按 §6 做版本号/发布说明/构建（dist 目录经 `-DistDir` 显式指定）/gate 校验。
4. **T5 验收**：按 §7 A–G 矩阵 + §9 边界写报告；证据目录 `D:\qio-dev\qio-032-evidence\acceptance\`。
5. 契约变更：任何字段/命名改动先 send_message Lead + 受影响的 T2/T3，确认后改本文件（本文件是唯一规格源）。