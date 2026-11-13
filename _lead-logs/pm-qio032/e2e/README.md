# PM-QIO032 互动模式 · E2E 验收

本目录是修复组的**可复跑 E2E 验收**资产与证据。浏览器层用系统 Edge + CDP（本机没有 Playwright，
原因见 `scripts/verify-pm-qio032/cdp.mjs` 头注释），模型调用一律打到本机 fake provider；
断言只读真实界面状态与 fake provider 的请求台账，不读产品内部状态、不 mock 业务接口。

## 跑法

```powershell
# 全场景（每次一个干净隔离数据域）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1

# 单场景 / 多场景（可重复跑，场景内会重置板面 fixture）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1 -Only s1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1 -Only s4,s6

# 复用已迁移的数据域（冷库首次迁移要 60s+，复用可显著缩短；不影响隔离性）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1 -Only s1 -DataDir "$env:TEMP\qio-pm-qio032-data"
```

前置：Windows + Edge；`backend/.venv` 可用；端口 8734（uvicorn）/5199（vite）/8807（fake provider）
空闲。**不要并发跑两个 run.ps1**：它们会互杀端口，症状是跑到一半板面卡片消失、provider `fetch failed`
（本轮就发生过一次，见 `rerun-20261113-1307/`，该目录结果作废）。

## 本轮结论（2026-11-13 最终轮）

**38/40 PASS，2 项 UNVERIFIED（②-2 / ②-3）**。逐场景：

| 场景 | 结论 | 说明 |
|---|---|---|
| ① 首轮交付 / 三态参与 / 不泄漏 | **14/14 PASS** | 参与项 TXT 正文以 `source=attachment_text` **逐字进入首轮请求**；参与项 PNG 以 `image_url` data URL 交付且 base64 与本地字节一致；排除项 TXT、隐藏项 PNG、隐藏后恢复的文字卡、提交后新建卡均不出现 |
| ② A/B 三路 | **1/3 PASS，2 UNVERIFIED** | 提交 A→改 B 后板面显示 B 已取证（②-1）；绑定追问轮**没有执行**脚本化 `board_read_cards` 调用（每轮 2 个请求、0 个 tool 消息），故 ②-2/②-3 记 UNVERIFIED，不作产品结论 |
| ③ 重复提交 / 幂等（#80） | **6/6 PASS** | 不改内容再提交：submissions 7→7，界面「已提交过：这次内容与上次成功提交相同，没有新的改动…」；延迟回执/迟到事件/草稿清理失败为 [模拟] |
| ④ 800×600 / 1120-1121 边界（含 #05 压力） | **9/9 PASS** | ④-8：6 项改动时详情面板 h=237（钳制、内部滚动），`overlapSubmit=false`，真实点击任务入口能打开任务面板 |
| ⑤ 文件六件 / 首件失败 / 路由往返（#44/#45） | **3/3 PASS** | 六件拖放接收 5 件；批次面板「本批已接收 5 份 / 未接收 1 份（单批上限 5 份）」并逐项列出，第 6 件「重新导入」后成功补卡；首件失败显示可见原因 + 重试成功，后续文件不陪葬 |
| ⑥ 主题 / Tab / Esc / 中文输入法 | **5/5 PASS** | 净白/暗紫晶、连续 Tab、聊天焦点 Esc、组合态 Esc 与中文落字 |

### 上一轮口径更正（重要）

1. **①-6b 不是产品缺口，是验收脚本设计缺陷**：旧断言把**被设为「显示但不参与」**的 TXT 卡当交付对象，
   与 ①-7「排除项不泄漏」自相矛盾。改为参与项 TXT 卡后再在同一分支复跑，正文以
   `source=attachment_text`、`text="TXT-PART-…-参与项正文"` 进入请求（`provider-log-s1.json`）。
2. **①-9 的 image_unsupported 一半是取数 bug**：旧 `bodyText()` 取不到台账 `_body`，退化成
   `JSON.stringify(整个台账条目)`，正文引号被二次转义，`/"image_url"/` 永远匹配不到。
   改为优先读 `_body` 后，参与项 PNG 的 `image_url` data URL base64 与本地 PNG 字节一致（①-6c）。
3. **#05 / #80 / #44/#45 已修复并复验**：详情面板钳制（④-8）、重复提交识别 + 界面文案（③-3）、
   批次「已接收/未接收」清单 + 重新导入 + 首件失败重试（⑤-2/⑤-3）。

## 产物

- `summary.json`：最终逐条断言（38/40，②-2/②-3 的 detail.kind=unverified）、`mergedRuns`、`defects`
- `summary-s1.json` / `summary-s4.json`：上一轮单场景中间件（历史，保留不删）
- `provider-log-s1.json`：① 最终轮的请求台账（含 `_body` 请求正文，TXT/PNG 交付的直接证据）
- `provider-log.json`：全场景跑最后归档的台账（s6 时段；① 的正文证据看 `provider-log-s1.json`）
- `console.log` / `console-s1.log` / `console.raw.log` / `run.log`
- `backend.log` / `vite.log` / `provider-server.log`
- `shots/*.png`：每个关键步骤截图（800×600、5+ 改动详情、1120/1121、两主题、批次清单等）
- 中间轮目录：`baseline-s1/`（修 s1 断言后的基线，14/14）、`s1-rerun/`、`s4-rerun/`、
  `rerun-20261113-1307/`（并发跑污染，**作废**）

## 未验项（如实列出）

- **② 板面读工具链路**：绑定追问轮没有执行脚本化工具调用（toolRequests=0）；是「板面工具在该轮未暴露」
  还是「话题标题请求吃掉了脚本首步」尚未定位。②-1 已证明板面显示 B、提交快照不再等于板面，
  但「快照 A / latest B」的工具返回值未取证。
- **② 读文件一路**（read_attachment）：A/B 用例目前用文字卡，文件读取未纳入。
- **⑤ 旧卡迁移**：需要旧 schema 板面 fixture；本轮只覆盖新卡与文件批。
- **⑤ 隐藏成员 / 撤销连线**：画布连线手势与组成员选择未纳入本轮 CDP 驱动（i-canvas 有单测/报告，
  但本 E2E 未驱动）。
- **原生 Tauri 窗口拖放**（真实路径 `dropped_path`）：浏览器层只能测字节拖放；
  Windows 原生拖放缺环境时单列待验。

## 对 harness 的修复（产品代码零改动）

1. **provider 台账 0 请求**：启动 shell 带 `HTTP(S)_PROXY` 时 OpenAI 客户端把 `127.0.0.1` 也送去代理；
   run.ps1 启动前清空代理并设 `NO_PROXY=127.0.0.1,localhost,::1`。
2. **干净数据域被首次引导拦住**：补 `POST /api/onboarding/complete`。
3. **正文无法取证**：共享 `scripts/verify_stream_provider.py` 只记元数据；新增
   `scripts/verify-pm-qio032/fake_provider.py`（import 后子类化，不改共享脚本），把请求 `_body` 附在同一条台账上。
4. **正文取数 bug**：`bodyText()` 优先读 `_body`（否则引号二次转义，image_url 永远匹配不到）。
5. **s1 设计修正**：交付断言只用参与项卡；排除/隐藏卡只做不泄漏断言（详见 `scripts/verify-pm-qio032/README.md`）。
6. **场景隔离**：每个场景开始时用板面状态 API 重置空白 fixture（`resetBoard`），这是测试前置，不是产品入口。
7. **日志抢写**：run.ps1 的原始 stdout 落 `console.raw.log`，e2e.mjs 独占 `console.log`。
8. **④ 断言**：DOMRect 转纯对象；1120/1121 量 `contentViewport` 实际可用宽度（本机 inset=28px，
   窗口 1177/1176 对应 1121/1120）；④-8 用 `innerHeight` 取视口高度（旧版误取 undefined 导致误判）。
9. **⑤ 断言**：改用批次面板 DOM（`add-material-status[data-batch-id]`、`add-batch-item[data-status]`、
   `card-attach-retry`），覆盖「已接收/未接收」清单、重新导入、首件失败重试。

## 边界（结论能读到哪）

- provider 是**本机扮演的假厂商**（真 SSE、有请求台账）。结论只证明「QIO 自己的链路对」，
  不证明真实厂商兼容性/网络行为。
- 浏览器层是 Edge headless + CDP，不是 Playwright；没有自动等待/严格模式语义。
- 允许且只允许以下模拟：CDP Fetch 延迟/中断（等价 route 延迟/abort）、`/api/events/test` 注入迟到事件、
  `Storage.removeItem` 抛错模拟草稿清理失败、CompositionEvent 模拟中文输入法；均在 summary `simulated` 列明。
- 不改产品代码、docs、测试与依赖；本目录只包含验证脚本与证据。
