# PM-QIO032 互动模式 E2E 验收脚本（Lead 维护）

## 用途
用**正常界面入口**（对话页模式切换进入互动板面）+ 本机扮演的假厂商（真 SSE、有请求台账）
+ 系统 Edge/CDP 驱动真实浏览器，取证 PM-QIO032 互动模式修复。不改产品代码。

## 一键运行
```powershell
powershell -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1
# 单场景 / 多场景：
powershell -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1 -Only s1
powershell -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1 -Only s1,s3,s4,s5
```
- 端口：后端 8734 / vite 5199 / fake provider 8807；run.ps1 会清端口、起服务、建假凭据、
  完成首次引导（干净数据域下模式切换会被「初始设置尚未完成」拦住）、跑 e2e.mjs、归档台账、收尾。
- **不要并发跑两个 run.ps1**：它们会互杀 8734/5199/8807，表现为跑到一半板面卡片消失、
  provider fetch failed。要并行请自行改端口（e2e_up.py 目前不支持）。
- 产物：`_lead-logs/pm-qio032/e2e/{summary.json,console.log,run.log,provider-log*.json,shots/*.png,...}`。
- 环境要求：Windows + 系统 Edge + 本机可运行 `backend/.venv`；不需要 Playwright/npm 包。

## 场景覆盖（e2e.mjs）
- **s1 ① 首轮交付 / 三态参与 / 不泄漏**：**参与项**文字卡 + 参与项 TXT 文件卡 + 参与项 PNG 文件卡
  承担交付断言（TXT 正文必须逐字进入 provider body；PNG 出现 image_url 时按 base64 与本地字节核对，
  无视觉链路则如实记 image_unsupported）；排除项 TXT / 隐藏项 PNG / 隐藏后恢复的文字卡只做不泄漏断言。
  > 设计修正：上一轮 ①-6b 用被设为「显示但不参与」的 TXT 卡断言交付，与 ①-7「排除项不泄漏」自相矛盾；
  > 现在交付对象与对照对象分开，排除项不泄漏的强度依赖 ①-6b 是否真的交付（detail 里如实标注）。
- **s2 ② A/B 快照一致（可选）**：提交 A → 改 B → 在互动底部对话发绑定追问，由 fake provider 脚本发
  真实 `board_read_cards` 工具调用（source=submission 期望读回 A、latest 期望读回 B）。
  本轮追问轮未执行脚本化工具调用（2 请求 / 0 tool 消息），②-2/②-3 如实记 UNVERIFIED，不伪报。
- **s3 ③ 重复提交与幂等（#80）**：双击只执行一次；成功后**不改内容**再提交，submissions 不增
  且界面必须说「已提交过 / 没有新的改动」；延迟回执、迟到事件、草稿清理失败为 [模拟]。
- **s4 ④ 800×600 与 1120/1121 边界**：窄窗 switch / 宽窗 side-by-side、面板与命中；
  **④-8 是 #05 压力场景**：有 5+ 项未提交改动时详情面板必须钳制（限高/内部滚动）、不遮提交按钮、
  任务入口可点（真实点击 tasks-entry 打开任务面板）。
- **s5 ⑤ 文件批（#44/#45）**：六件拖放只接收 5 件，批次面板逐项列出「已接收/未接收」，
  第 6 件可重新导入并真的补建卡片；首件失败可见原因 + 重试成功，后续文件不陪葬。
- **s6 ⑥ 主题 / Tab / Esc / 中文输入法**：净白/暗紫晶、连续 Tab、Esc（含聊天焦点）、组合态 Esc。

## 本轮实测结论（2026-11-13 最终轮）
见 `_lead-logs/pm-qio032/e2e/README.md` 与 `summary.json`：**38/40 PASS**，2 项 UNVERIFIED（②-2/②-3）。
① 首轮交付 14/14、③ 6/6、④ 9/9、⑤ 3/3、⑥ 5/5；#05 / #80 / #44/#45 均已修复并复验。
② 的工具读取链路未取证：追问轮没有执行脚本化工具调用，如实标 UNVERIFIED。

## 边界
- fake provider 只证明 QIO 自己的链路；不证明真实厂商 / Tauri 原生窗口 / Windows 原生拖放。
- 模拟网络故障用 CDP/page route 时在用例中标注「模拟」。
- 本目录是验收资产，不是产品代码；不参与打包。
