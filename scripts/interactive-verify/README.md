# 互动模式验收脚本（Lead 维护）

这些脚本用来做**真实界面**的验收：驱动本机 Chrome 走用户实际会走的路径，截图并用接口核对结果。
它们不是产品代码，不参与打包。

## 文件

| 文件 | 作用 |
| --- | --- |
| `ui-scenarios.mjs` | 互动模式的主验收：加材料 / 写两条注释 / 只勾选一条 / 多选成组 / 真实鼠标拖动 / 提交 / 演示意图 / 板面虚线预览 / 批量条件，共 18 项断言，每步截图 |
| `run-probe.mjs` | 薄封装：调用 `scripts/visual_probe.mjs`，把 JSON 结果打印成人能读的报告 |
| `vite.e2e.config.ts` | 只给验收用的 Vite 配置：把 junction 出去的 `node_modules` 真实路径补进 `fs.allow`，让 @fontsource 字体不再 403。**不改产品配置** |
| `steps-*.json` | 单点探针（入口点击、卡片 DOM、编辑器、选择调试等） |

## 怎么跑

1. 后端（**不要占用别人正在用的 8734**）：
   ```
   $env:QIO_DATA_DIR = "$env:TEMP\qio-e2e-im"; $env:QIO_DEV_INSECURE = "1"
   backend\.venv\Scripts\python.exe -m uvicorn agent.main:create_app --factory --host 127.0.0.1 --port 8791
   ```
2. 前端：`node frontend/node_modules/vite/bin/vite.js --config scripts/interactive-verify/vite.e2e.config.ts --port 5299 --host 127.0.0.1`
   （环境变量 `VITE_QIO_BACKEND_URL=http://127.0.0.1:8791`）
3. 跑验收：`node scripts/interactive-verify/ui-scenarios.mjs`（可用 `IM_APP` / `IM_BACKEND` 覆盖地址）
4. 截图输出在 `%TEMP%\qio-visual\shots`。

## 截图对应关系

| 截图 | 说明 |
| --- | --- |
| `im-40-conversation-entry.png` / `im-41-entered-by-link.png` | 对话页底部的「互动模式」入口，以及点击后进入 `#/interactive` |
| `im-10-note-editing.png` / `im-11-two-notes-one-checked.png` | 两条文字注释、只勾选其中一条（未勾选的写明「QIO 看不到它的文字」） |
| `im-12-two-cards-grouped.png` | 多选两张卡片后成组（组框 + 成员） |
| `im-20-dragging-preview.png` / `im-21-after-drop.png` | 真实鼠标拖动过程与放下后的结果 |
| `im-13-submitted.png` | 提交之后：本次有效改动、允许查看范围、勾选自动取消、以及「保存不调用 QIO」的说明 |
| `im-30-demo-intents-and-previews.png` | 四项演示意图、辅助区任务列表、板面上的虚线预览、批量列表 |

## 这些脚本**没有**覆盖的

- 真实模型调用：第一阶段没有接入，脚本只验证「提交落库 + 明确说明没有接入」。
- 手感与动画质量：探针只能给几何与状态，不能替代人工手感判断。
- 窄窗口与滚动细节：`ui-scenarios.mjs` 目前只在 1440×900 下跑。
