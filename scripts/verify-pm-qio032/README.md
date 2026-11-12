# PM-QIO032 互动模式 E2E 验收脚本（Lead 维护）

## 用途
用**正常界面入口**（对话页模式切换进入互动板面）+ 本机扮演的假厂商（真 SSE、有请求台账）
+ 系统 Edge/CDP 驱动真实浏览器，取证 PM-QIO032 互动模式修复。不改产品代码。

## 一键运行
```powershell
powershell -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1
# 单场景：
powershell -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1 -Only s1
```
- 端口：后端 8734 / vite 5199 / fake provider 8807；run.ps1 会清端口、起服务、建假凭据、
  完成首次引导（干净数据域下模式切换会被「初始设置尚未完成」拦住）、跑 e2e.mjs、归档台账、收尾。
- 产物：`_lead-logs/pm-qio032/e2e/{summary.json,console.log,run.log,provider-log.json,shots/*.png,...}`。
- 环境要求：Windows + 系统 Edge + 本机可运行 `backend/.venv`；不需要 Playwright/npm 包。

## 场景覆盖（e2e.mjs）
- s1 ① 首轮交付 / 三态参与 / 不泄漏（文字卡 + TXT/PNG 文件卡 + 显示但/隐藏且不参与 + 恢复显示）
- s3 ③ 重复提交与幂等
- s4 ④ 800×600 窄窗单开/组合开与命中
- s5 ⑤ 文件批 / 首件失败 / 路由往返
- s6 ⑥ 主题 / Tab / Esc / 中文输入法

## 本轮实测结论（2026-11-13）
见 `_lead-logs/pm-qio032/e2e/SUMMARY.md`：通过 ①-0..①-4a、④-0、④-1；其余场景脚本未跑通或
断言未捕获，均如实标 UNVERIFIED，不宣称通过。

## 边界
- fake provider 只证明 QIO 自己的链路；不证明真实厂商 / Tauri 原生窗口 / Windows 原生拖放。
- 模拟网络故障用 CDP/page route 时在用例中标注「模拟」。
- 本目录是验收资产，不是产品代码；不参与打包。
