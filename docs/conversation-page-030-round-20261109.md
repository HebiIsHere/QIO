# 对话页 0.3.0 开发与修复（2026-11-09）

本轮以 **QIO 当前 main（`origin/main` @ `a20f5e1f`，v0.3.0 实际实现）** 为起点，
在分支 `fix/conv-page-030-20261109` 上完成用户提示词中的七项开发与修复。
**未合并 main、未打标签、未发布、未触发完整 CI。**

- 基线提交：`a20f5e1f7004f57758eef914d464115ce2b8cdd2`（origin/main）
- 功能与测试提交：`b5e64106f221b550b6d1b87e218625f8cc46b42c`
- 文档回填提交（最终 HEAD）：本文件所在提交
- 设计规范：`docs/superpowers/specs/2026-08-09-qio-frontend-design.md`
- 状态同步：`docs/status.md` 顶部「最后核对」、`docs/architecture.md` §4.2.1 / §20.2 / §20.5

## 一、工具失败进入任务栏的条件（§一）

**行为**：`run_shell` / `web_search` / `web_fetch` 的单次失败默认只保留在执行记录
（`tool_records` / 消息详情）里，不再自动进入任务栏当前/历史列表、需要处理数量、
收起徽标或顶部失败通知。

判定只用程序字段，不匹配错误文本、不额外调用模型：

- 先按「同一 turn + 同一工具」建立尝试链：重试/替换查询/替换来源/调整命令产生的
  非最后一次失败不单列；
- 该轮还在跑/排队、或没有真实结束事实（`turn_journal`）时暂不生成；
- 非零退出码（`code=exit_code`）不推导为整个任务失败，也不计入需要处理；
- 只有结构化事实表明**需要用户处理**（permission / missing_dependency / environment /
  startup / `search_not_configured`）或**必须持续核查**（`run_shell` 超时、无法确认
  是否生效）时才生成条目；摘要说清用户要做什么、处理后能继续什么，错误原文只进详情；
- 过滤发生在后端权威快照生成处，当前/历史/计数/徽标/顶部通知同源；执行记录原样保留
  （不删除、不改写成成功、不要求逐条「知道了」清理）；旧记录按同一规则读取，
  重连后不重放旧失败通知；
- 本轮只对上述三个工具生效，其它工具的既有展示规则不变；没有可靠任务关联来源时不
  伪造关联。

**实现**：`backend/src/agent/services/task_feed.py`（triage）、
`storage/tool_records.py` + `storage/schema.py`（迁移 75/76：`code` / `category` /
`recoverable` / `result_status`）、`core/loop.py` 与 `core/tool_feedback.py`
（`TOOL_END` 带 `code` / `result_status`）、`services/app.py`、三个工具补齐稳定 code。
**测试**：`backend/tests/test_conv030_tool_failure_task_feed.py`（新增）。

## 二、设置返回后的阅读位置恢复（§二）

**行为**：

- 离开前在底部 → 返回后**主动贴底并继续跟随**，不依赖「之后再来一条消息」；
- 离开前阅读历史 → 返回后恢复原消息及其内部阅读位置，保持停止跟随；离开期间新增
  消息只增加「回到最新」角标，不把用户拉走；
- 阅读位置保存「话题 + 轮次稳定身份（`turn_id` / 首条消息 id）+ 轮内锚点（消息 id /
  过程区）+ 文本片段 + 视口相对偏移」，旧像素只作回退；
- 恢复先按稳定身份定位轮次，再用真实布局逐帧校正到「同一内容处于相近视口位置」；
  图片 / 字体 / Markdown / 过程展开 / 窗口宽度变化只做有界后续校正；不是「列表可滚动」
  就宣告成功，也不是固定等待若干毫秒；
- 原目标不在已加载范围时按现有分页机制有界寻找，确实不可用才回退到最近可用内容并
  简短说明（不静默跳到底部、不新增「找回位置」按钮）；
- 用户滚动/键盘导航立即接管并作废恢复；切话题、组件卸载换代，旧异步回调不改新话题；
- 保存发生在页面仍可测量时（卸载前），卸载后的零值与首次重建的临时 scroll 事件不污染
  已保存位置。

**实现**：`frontend/src/stores/chatViewport.ts`（新增，跨路由保留的阅读状态）、
`frontend/src/utils/readingAnchor.ts`（新增，稳定锚点纯函数）、
`frontend/src/components/MessageStream.vue`（保存/恢复/有界校正/取消/换代）、
`MessageStream` 模板增加 `data-turn-key` / `data-message-id`。
**测试**：`MessageStream.test.ts`（含新增 4 条恢复策略用例）、
`src/utils/__tests__/readingAnchor.test.ts`；另有真实虚拟列表浏览器回归（见「验收证据」）。

## 三、统一输入区辅助入口，并突出「回到最新」（§三）

**行为**：

- 「回到最新」并入输入区辅助入口行**固定顺序末尾**，与其它入口同尺寸（34px 圆形）、
  同图标尺寸（16px）、同间距；
- 入口行与真实输入胶囊放在同一个 grid 的第 2 列，**以胶囊为基准居中**，不再按包含
  左侧模式/右侧星球的整行居中；入口行参与正常布局，消息区在它上方结束；空间不足时
  有序换行、不溢出；
- 展开事项面板时向上生长，入口行与胶囊位置稳定；无任何入口时整行收起；
- 「回到最新」使用 QIO 强调色实底 + 白色向下箭头，悬停/按下使用现有强调色状态，
  不增加光晕或额外描边；新消息角标用页面底色 + 强调色文字，与实底清楚区分；
- 离开底部时显示，点击滚动到底部并恢复跟随，到达后隐藏；平滑滚动尊重减少动态效果
  设置且可被用户中断；键盘、读屏名称与用途说明保留；
- 只改变阅读位置，不取消「从历史继续」选择；取消接续也不改变阅读位置；
- 删除原独立浮动定位的 `useLatestButtonAnchor` / `utils/latestButton` 及其监听，
  避免两套规则同时控制同一按钮。

**实现**：`stores/chatViewport.ts`、`conversation/ComposerDock.vue`（统一 grid）、
`conversation/ComposerEntries.vue`（入口与强调色样式）、`MessageStream.vue`
（消费共享状态；移除浮动按钮）。
**测试**：`ComposerEntries.latest.test.ts`（新增）、`chat-box-layout.test.ts`（结构守卫
更新）、`ChatColumn.test.ts`（共享状态口径）；浏览器回归核对真实几何（见验收证据）。

## 四、过程状态、错误说明与信息层级（§四）

**行为**：

- 「本轮已结束」≠「用户目标已完成」≠「知识已保存 / 已提交审批」；只有后端结构化
  事实（如等待审批、声明核对通过）才升级具体结论；
- 单次工具失败不把整轮变成失败；失败数量/原因不再取 `toolError` 前 40 字，而是用
  稳定 `code` / `category_label` 生成可读结论（例如知识提交失败显示「知识提交未成功，
  引用依据未通过核对」），原始错误与逐项细节只在展开详情；
- 同轮知识提交先失败后重试成功时按最后一次成功证据呈现；
- 第一层只放状态 + 简短结果，耗时/阶段数/调用数降为次级信息，长阶段名不再挤占状态行；
  窄宽度允许折行，状态与详情入口不被挤出；
- 知识提交失败的 `evidence_not_found` 不属于 §一 的命令/搜索过滤范围，仍在过程区与
  工具卡中如实展示；最终答复校准链路（`TurnFacts.annotation`）会追加可读系统核对，
  不虚构提交或审批成功。

**实现**：`frontend/src/components/TurnProcess.vue`、`TurnTimingPanel.vue`、
`MessageItem.vue`、`NarrativeStage.vue`（等待审批状态的最小诚实修复）、
`stores/events.ts` / `stores/session.ts`（消费 `code` / `result_status`）、
`backend/src/agent/tools/knowledge_propose.py`、`backend/src/agent/core/turn_facts.py`。
**测试**：`TurnProcessStateSemantics.test.ts`、`MessageItemMetaReadability.test.ts`、
`stores/__tests__/toolFacts.test.ts`（新增）+ 既有过程区/耗时/叙事测试按新语义校准；
后端 `test_turn_facts.py`、`test_p1_c_knowledge_flow.py`。

## 五、减少重复信息并改善元信息可读性（§五）

- 普通对话每轮回答默认不再重复显示当前话题名，顶部话题栏保留；回答正文里的真实内容
  标题不受影响；`MessageItem` 的 `showTopic` 能力保留（其它复用场景核查为不需要）。
- 工具任务折叠行不再重复「工具执行」来源（标题以来源文案开头时隐藏 meta 来源；
  详情里的来源/执行者仍可查）。
- 轮次/时间等元信息统一使用 `--fs-meta` 令牌（11.5px），去掉「10.5px + 低透明度、
  悬停才可读」；颜色用 `--text-muted`（实测 5.4:1），hover 只轻微提亮，层级仍低于正文。
- 窗口最小化提示缩短为「最小化到任务栏」，按钮名称与键盘通道保留。
- 提示组件（QTip）已有真实尺寸测量、8px 视口留白与上下翻转：本轮用既有测试 + 几何桩
  验证，**未发现真实缺陷，未改组件**。

**测试**：`task-rail-source-dedup.test.ts`、`qio-tip-measure.test.ts`、
`WindowChrome.test.ts`、`MessageItemMetaReadability.test.ts`。

## 六、桌面窗口外轮廓圆角（§六）

**实现**（壳侧，不在网页容器伪装）：

- `frontend/src-tauri/src/main.rs` 新增 `#[cfg(windows)] apply_windows_window_outline`：
  `DwmSetWindowAttribute(DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)` 请求 Windows 11
  系统圆角；`DWMWA_BORDER_COLOR = DWMWA_COLOR_NONE` 去掉 Tauri `shadow:true` 在无边框
  窗口上产生的 1px 白边。失败只记 `log::debug`，不影响启动；最大化/贴边/还原由系统
  自动处理，未设置自定义窗口区域、未加网页圆角；
- `frontend/src-tauri/Cargo.toml` 只给既有 `windows-sys` 追加
  `Win32_Graphics_Dwm` feature（不新增 crate，`Cargo.lock` 未变）；
- `frontend/src-tauri/tauri.conf.json` 主窗口追加
  `backgroundColor: "#171015"`（= 默认主题 `--bg-base`，避免窗口层白边/启动白闪）；
  `decorations:false` / `shadow:true` 保持不变；
- 网页层审计：`body`/`:root` 统一深色底、`.app-shell` 无圆角无背景、固定层/弹层都在
  窗口裁切内且无负偏移；**未**在网页容器加 `border-radius` 假装原生圆角。

**重要限制（如实）**：本机没有 Rust 工具链（`rustc`/`cargo`/`rustup` 均不存在），
因此 **Rust 代码未编译、未运行、未真机验收**。已完成的可验证项：语法结构自检、
`windows-sys 0.61` 官方文档核对（feature 名 / 常量值 / `DwmSetWindowAttribute` 签名）、
沿用仓库既有 `window.hwnd()` 先例、Tauri 官方配置 schema 核对。Windows 系统圆角偏好
不提供任意像素半径，**不承诺与全局栏 20px 精确一致**；四角实际外观、阴影、最大化/
贴边/还原、缩放拖动、多 DPI、Win10/DWM 关闭场景均为**待真机验证**。

## 七、验收证据

### 浏览器回归（真实前端 + 真实虚拟列表 + 本机假厂商真 SSE）

脚本：`scripts/verify-conv030-reading-position.mjs`
证据：`docs/verification-shots-conv030/`（含 `summary.json` 与 6 张 1440×900 / 800×600
浅色/深色截图）。

结果：**27 项 PASS / 0 FAIL**（2026-11-09）。关键项：

- 8 轮长回答种子后，离开前在底部 → 设置往返 → **主动贴底且继续跟随**（不依赖新消息）；
- 滚到第 5 组长回答内部（`MARKER-G5-P22`）→ 设置往返（离开期间后端真实完成一轮新消息）
  → 同一段落的视口偏移 **180.19px → 180.19px**（不是只比较 scrollTop；期间列表总高度
  从 28944 变为 32558px，真实虚拟列表测量变化）；
- 离开期间新消息不把阅读位置拉走（保持停止跟随），角标显示「2 条新消息」；
- 入口行中心与真实输入胶囊中心误差 **0px**；「回到最新」34px 圆形、强调色实底
  `rgb(197, 27, 125)` + 白色箭头、位于入口行末尾；点击后到底部并隐藏；
- 减少动态效果下点击立即贴底；长输入、左栏收起、800×600 最小支持窗口下入口行仍以
  胶囊为中心且不溢出；接续选择提示在点击「回到最新」前后原样保留。

说明：浏览器内截图只证明网页布局与滚动行为，**不证明 Windows 原生窗口圆角**。

### 定向/全量测试（实际执行）

- 后端定向：§一 新文件 24 passed；相关批次 235 passed、迁移/补偿/import smoke 112 passed；
  §四 相关 106 passed；前端通知既有装置 10 passed。
- 前端定向：§四 相关 21 files / 222 tests；§五/§六 相关 11 files / 101 tests；
  §二/§三 相关 MessageStream 25 / readingAnchor 10 / ComposerEntries 5 /
  chat-box-layout / ChatColumn / Composer 等全部通过。
- 前端全量：`npx vitest run` → 534 files / 4605 tests，其中 4603 passed；
  2 项 `router-aliases.test.ts` 在「与后端全量同时运行、机器高负载」时因动态 import
  超过 5s 超时失败；单独复跑该文件 3/3 passed（既有装置对并发负载敏感，不是本轮断言
  变化）。因此**不宣称本轮前端全量一次干净全绿**。
- 后端全量：`uv run --frozen pytest` 在本机两次启动均因环境极慢（约 1%/分钟，
  预计 >1 小时）未在合理时间内完成，**未得到全量结论**；已完成的定向后端批次见上。
- `npx vue-tsc --noEmit` → exit 0；`python scripts/check_docs.py` → 通过。

## 八、未完成 / 待验证边界

1. **Windows 原生窗口外轮廓**：本机无 Rust 工具链 → 未编译、未构建安装包、未真机
   验收；四角外观/阴影/最大化贴边还原/多 DPI/缩放拖动/点击命中均为待真机验证。
2. **真实厂商与真实服务**：本轮前端/后端验证使用本机假厂商（真 SSE）与 fake
   search/fetch；未调用真实模型、未联网搜索/抓取，不证明真实厂商行为。
3. **真实 run_shell 沙箱**：§一 的 run_shell 过滤用工具单测与同名 demo tool 端到端
   覆盖，未对真实 shell 沙箱跑整轮审批+执行。
4. **视觉**：窄窗口/长文本、等待审批卡片警示色、元信息对比度已做源码守卫与浏览器
   截图，但未做 Windows 安装版真机视觉验收；`backgroundColor` 为默认深色，
   浅色主题用户启动瞬间可能先见深色底再切浅色（不影响圆角与去白边）。
5. **任务栏过滤边界**（记录在案）：无 `turn_journal` 结束事实的旧失败默认不展示
   （执行记录仍在）；沙箱边界类 code 当前不在可展示白名单内；跨轮同类配置失败会
   各自成条（三工具没有可靠任务关联来源，不伪造合并）。

## 九、口径

- **已实现**：上述七项代码与测试。
- **已验证**：后端/前端定向与全量测试、浏览器回归 27/27、文档一致性。
- **待真机验证**：Windows 原生窗口圆角与阴影、安装版视觉、真实厂商/联网/真实 shell。