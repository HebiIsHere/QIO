# 互动模式底部操作区 · 实机取证 harness（verifier 维护）

用**系统 Edge + CDP**（本机没有 Playwright）驱动真实浏览器，对互动模式底部操作区做
可复跑的逐帧 / 截图 / 状态取证。本目录是验收资产，不是产品代码，不参与打包。

## 一键运行（阶段 2 最终取证）

    powershell -ExecutionPolicy Bypass -File scripts/verify-interactive-action/run.ps1

run.ps1 会依次完成：清端口（8734 / 5199 / 8807 / 9333，只终止占用端口的那个 PID）
→ 起 fake provider（真 SSE、有请求台账）→ 起 backend(8734) + vite(5199)（隔离 QIO_DATA_DIR）
→ 建假凭据（sk-verify-fake）+ 完成首次引导 → 跑 frames.mjs → 归档 provider 台账 / 日志
→ 只关自己起的进程（记录 PID）。

- 端口：backend 8734 / vite 5199 / fake provider 8807 / CDP 9333（与既有的
  scripts/verify-pm-qio032 约定一致；**不要并发跑两份**，会互抢端口）。
- 数据域：默认 %TEMP%\qio-ia-<时间戳>（冷库迁移实测 60s+，后端就绪预算给到 180s）。
- 产物：_lead-logs/interactive-action/final/{summary.json,report.md,shots/*.png,backend.log,vite.log,provider-log.json,console.raw.log,run.log,run-meta.json}。
- 参数：-Root <工作树>（默认本仓库；可对基线工作树取证）、-OutDir <目录>、
  -NodeScript <脚本>、-ScriptArgs <透传参数>、-ProviderPort、-KeepRunning。
- 实现说明：启动器复刻了 scripts/e2e_up.py / e2e_down.py 的既有做法（venv 解释器探测、
  QIO_DEV_INSECURE / QIO_ENABLE_TEST_EVENTS、隔离 QIO_DATA_DIR、真实 TCP 探测确认端口），
  但把 PID / 日志都放在本产物目录，因此可以安全地对**其它工作树**取证而不会写到它们里面。

## 当前状态（2026-11-22）

2026-11-17 用户修订动效口径：线框「出现 / 消失」同速、共用 650ms easeInOutCubic（慢-快-慢）时间轴；
消息内容随把手同速升降、沉入操作行时同步虚化（opacity 1→0 + blur 0→6px）。harness 的到角窗口、
同速断言、非线性断言与反向/拖动场景抓取时机已按新口径更新；最终全量 **PASS 52 / FAIL 0 / UNVERIFIED 0**。
机器负载高时逐帧计时会被污染，最终全量应在空闲机器上跑。阶段 1 基线（旧 216ms 口径）仅作历史参照。

## 阶段 1 产物（改动前基线，已交付）

| 产物 | 内容 |
|---|---|
| _lead-logs/interactive-action/preview-baseline.json | 预览 QIO_Animated_Chat_Preview.html 的逐帧数组 + 结论：点击→横线到角 **218.4ms**（帧量化 220.3ms），静态 cornerFraction 0.5722，展开高度 50%/90% = 170.4/371.5ms，settle 820.3ms；收起 settle 449.9ms；720 视口到角 216.1ms |
| .../shots/preview-*.png | 预览收起/展开 + 0/60/120/180/260/420ms 关键帧（1000×600，另 720×600 对照） |
| _lead-logs/interactive-action/baseline/baseline-current.json | v0.3.4（q46-r9，HEAD 37d54344）改动前当前实现：1000×600 .ic 902×182.6 收起 / 902×280 展开，输入胶囊 902×98（中线相对面板 231），把手 75.2×26；无线框/揭示动画；PLAN §3.2 契约缺口清单 |
| .../baseline/shots/baseline-*.png | 改动前 1000×600 / 800×600 收起与展开截图 |

预览基准单独跑：

    node scripts/verify-interactive-action/preview-baseline.mjs --width=1000 --height=600 --extra-width=720

## frames.mjs（阶段 2 取证内容）

    node scripts/verify-interactive-action/frames.mjs [--only=contract,motion,...]

- **contract**：PLAN §3.2 DOM 契约（frame/glass/stream/rail/toggle/entries/composer、
  data-ic-reveal / frame-path-length / frame-horizontal / corner-reveal、槽位、镜像 path、唯一渐变 id）。
- **motion**：1000×600 收起/展开截图（另做一次 SVG 离屏渲染像素统计，防「数值有路径但视觉不可见」）；点击把手后页面内 rAF 逐帧采样（每帧记录
  data-ic-*、两段 [data-ic-frame-path] 的 strokeDasharray/getTotalLength、CSS 变量、矩形），
  算到角 / 高度 50%·90% / settle / 同帧更新 / 左右对称 / 起点=把手端点 / 竖边接续；
  另抓 0/60/120/180/260/420ms 关键帧截图。
- **reverse**：展开途中快速反向收起，检查连续更新（无跳变）并停在收起。
- **drag**：展开途中拖把手（data-ic-dragging、高度连续跟手、无跳变）、松手保留高度并写回记忆、
  收起再展开高度记忆恢复；**拖动超过阈值松手后，同一次 pointerup 的 click 不收起对话，下一次真实点击才收起**
  （Lead 2026-11-17 裁决的 suppressClick 口径）；拖动中 Esc 取消 → 高度恢复且仍展开 → 松开指针后仍展开
  （取消后的 pointerup click 只被吞一次，PASS 口径）。
- **resize**：展开中窗口变宽（1000→800）变矮（600→450）再回来：夹取、不溢出、仍展开。
- **viewports**：1440×900 / 1000×600 / 800×600 / 720×600 各拍收起+展开，检查不溢出、
  把手/模式切换/工具按钮仍可命中。
- **themes**：亮/暗主题（localStorage qio-theme + reload），各拍收起+展开，记录主题令牌计算值。
- **states**：用 fake provider 慢 SSE 制造「排队中」、提交板面制造「待确认」，各拍收起+展开。
- **keyboard**：聚焦输入框自动展开、把手 Enter/Space 开合、↑/↓ 每步 20px、Home/End 边界夹取、
  Esc 层级（先关面板再收起）。
- **reduced**：Emulation.setEmulatedMedia prefers-reduced-motion: reduce → 直接落最终几何（≤2 帧）。
- **invariants**：草稿、消息滚动位置、发送（provider 台账确有正文）、模式切换往返；
  附件保持（真实文件选择器 → 上传/登记 → 开合后 chip 仍在）；chatFrameState 通过 page 内
  import('/src/interactive/chatFrameState.ts') 读真实模块：展开发布真实矩形、收起/卸载发布 null。

### 到角测量口径（与 PLAN §5-1 对齐）

- 原点 t0 = 页面内 [data-ic-toggle] 的 click 事件时间（click 里同步开始动效）。
- 到角 = 首帧 data-ic-reveal >= data-ic-corner-reveal（回退：dash 可见长度 >= data-ic-frame-horizontal）。
- 帧量化值之外，按相邻两帧线性插值给亚帧估计；同时给帧量化值，避免过度精确。
- 到角判定（Lead 2026-11-17 裁决）：**逐帧** reveal × data-ic-frame-path-length >= data-ic-frame-horizontal；
  data-ic-corner-reveal 只是完全展开后的静态比例，不能用于动画判定（仅作对照记录）。
- PASS 窗口：0.28–0.62s（目标 380ms，650ms 口径下随几何与点击延迟浮动）；超出即 FAIL，不粉饰。
  旧预览基准 218.4ms 已随 2026-11-17 用户修订失效，只作历史参照；同速/非线性由以下两项独立断言：
  「展开与收起的 settle 时长差 ≤120ms」「25% / 50% / 75% 归一化进度符合 easeInOutCubic（≈0.06 / 0.5 / 0.94）」。
- 线框「真的画出来了吗」：除数值断言外，还会把 svg[data-ic-frame] 连同解析后的渐变颜色离屏渲染到 canvas，
  统计非背景像素（防「data 属性有路径但视觉上不可见」；阶段 1 曾用它抓到一次渐变 y1=y2=0 的瞬时问题）。

### 指针/键盘竞态的如实处理

- 展开途中「快速反向」：把手在展开时持续移动，真指针按下/松开可能落在不同元素上（浏览器把 click 派给
  最近公共祖先）。因此反向那一下用元素自身的 element.click() 触发同一条 Vue handler；650ms easeInOut
  起步很慢，反向时机取约 350ms（reveal≈0.5）而非旧的 90ms；同场景的首次展开与普通收起仍用真实鼠标
  事件（motion 场景）。报告里会写明方法。
- 展开途中拖动：点击展开后轮询面板总高，等越过 min=220 并留余量（≥232）再抓把手，随后用真实 mouseMoved
  拖动；抓得太早时 press 瞬间高度还在 min 以下，第一段位移会被 min 夹没，看起来像「不跟手」。
- 键盘 Space：CDP 的 Input.dispatchKeyEvent 必须发 key=" "（不是 key="Space"），否则原生 button 不激活。

## 产物与断言

帧表 + 几何写入 summary.json；report.md（中文）逐项列 PASS/FAIL/UNVERIFIED 与证据文件名；
shots/*.png 是截图证据。FAIL 项退出码非 0。

## 边界（如实声明）

- fake provider 只证明 QIO 自己的链路；不证明真实厂商/SDK、Tauri 原生窗口、Windows 原生拖放。
- 逐帧数值来自 rAF 采样，到角时刻在「上一帧与首帧满足帧」之间（已插值并同时给帧量化值）。
- 不新增 npm 依赖、不用 Playwright、不联网拉依赖；不修改产品代码/测试。
- 不并发跑两套（端口固定）；退出只关闭本脚本启动的进程。
- 截图/日志不含真实密钥：凭据用假 Key（sk-verify-fake），只指向本机 fake provider。

## 最终取证结果（2026-11-22，已交付）

- 命令：powershell -ExecutionPolicy Bypass -File scripts/verify-interactive-action/run.ps1 -OutDir <repo>\_lead-logs\interactive-action\final
- 结果：**PASS 52 / FAIL 0 / UNVERIFIED 0**（runId 20261122103444，HEAD 01dc16d1，系统 Edge，隔离数据域 + 本机 fake provider）。
- 关键数值（650ms 慢-快-慢口径）：点击→横线到角 **386.6ms**（帧量化 391.9ms，PASS 窗 280–620ms）；展开高度 50%/90% = 343.9/478.9ms；展开 settle **675.4ms**；收起 settle **657.6ms**（同速差 17.8ms）；
  展开归一化进度 25%/50%/75% = **0.056 / 0.492 / 0.940**，收起 = **0.055 / 0.489 / 0.940**（时间镜像、两端慢中间快）；线框 pathLen 599.24 / horizontal 363.25 / 静态 cornerReveal 0.6062；
  左右横线起点=把手端点（delta 0px，含 leverInset=4）；SVG height 245 = 胶囊中线 245；高度与揭示同帧更新 37 同帧 / 1 reveal-only / 0 height-only；无 console error。
- 展开途中拖动：抓握高度 255 → 拖动采样 253 / 273 / 280（跟手、无跳变），松手保留并写回记忆。
- 工具栏三档：1440/1000=full（添加文字可见）、900=icons（railSide 339）、800=more（railSide 289 < RAIL_TOOLBAR_ICONS_MIN 300，Lead 裁决预期即 more）、720=more（更多面板可开、详情可打开、Esc 关）。
- 排队状态：慢 SSE（单响应 40 分片 × 250ms，fake provider 独立实测 11.9s）下抓「running + queued>=1」瞬间：操作行显示「正在回复 · 已排队 1 条」，收起/展开前后一致。
- chatFrameState：page 内 import('/src/interactive/chatFrameState.ts') 读真实模块：展开 {x:70,y:331,w:860,h:212}，收起/卸载均 null。
- 截图：_lead-logs/interactive-action/final/shots/01–36（收起/展开/0/60/120/180/260/420ms 帧/快速反向/拖动/Esc/多视口/主题/排队/待确认/减少动画）。
- 中间 dryrun / smoke 目录已清理，以上面的 final/ 与阶段 1 基线为准。
