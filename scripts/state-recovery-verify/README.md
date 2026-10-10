# 第八轮状态恢复收尾 · 独立验收装置（子智能体 D）

本目录只放**独立命名的验收脚本与证据**；前端反例在 frontend/src/acceptance-state-recovery/，
后端反例在 backend/tests/test_state_recovery_d_acceptance.py。不改任何产品代码。

## 文件

| 文件 | 作用 |
| --- | --- |
| config.mjs | 共用配置：端口 8933/5433、独立临时 QIO_DATA_DIR（%TEMP%\qio-sr-data）、板面 board_default |
| serve.mjs | 起/停验收环境（后端 uvicorn + 前端 vite.e2e.config.ts），pid 记在本目录 .pids.json |
| cdp.mjs | 自用 CDP 驱动：视口、真实点击/拖动、受控网络延迟、真实请求集合、截图（QIO_SR_* 环境变量） |
| metrics.mjs | E1 的页内测量脚本（主题/卡片/关系/面板/失败区文案/重叠面积） |
| capture.mjs | E1：4 尺寸 × 2 主题 × 4 场景的截图断言矩阵 |
| journey.mjs | E2：真实浏览器旅程 J1–J5（真实后端 + 真实持久 profile + 进程关闭重开） |
| probe.mjs | 排查用最小探针（打开应用、打印主题与关键元素） |
| probe-intents.mjs | 排查用探针：按真实 API 走「铺材料 → 生成演示意图 → 批准 → 推进完成」并打印响应 |
| probe-ls*.mjs | 排查用探针：profile 里 localStorage 在「关闭重开」后是否保留（多实例互相覆盖的实测证据） |
| evidence/ | 基线红证据、E1/E2 报告与运行日志 |
| shots/ | 截图与 JSON 报告（按 --label 分目录） |

## 怎么跑

    # 1) 起环境（独立临时数据目录；用完 --stop）
    node scripts/state-recovery-verify/serve.mjs
    node scripts/state-recovery-verify/serve.mjs --status

    # 2) E1 矩阵（约 30 分钟；--sizes/--scenes/--themes 可缩范围）
    node scripts/state-recovery-verify/capture.mjs --label=baseline

    # 3) E2 旅程
    node scripts/state-recovery-verify/journey.mjs --label=baseline
    node scripts/state-recovery-verify/journey.mjs --label=baseline --scenarios=J1

    node scripts/state-recovery-verify/serve.mjs --stop

端口 / 数据目录可用 QIO_SR_BACKEND_PORT / QIO_SR_APP_PORT / QIO_SR_DATA_DIR 覆盖；
浏览器调试端口 QIO_SR_PROBE_PORT（默认 9788）、profile QIO_SR_PROFILE（默认 %TEMP%\qio-sr-edge）。

## E1 的采集口径（本轮修正的点）

1. **截图前实际断言**：主题（data-theme + 计算背景亮度 + qio-theme 持久化）、
   6 张测试卡片 / 6 条关系（svg.link-layer line.line）/ 2 个组、预定面板状态、
   目标失败的原因与重试入口；任何一条不满足就把该格判失败，不会用空板面冒充有效场景。
2. 主题走真实持久化路径（localStorage qio-theme → 重新加载），加载之后再断言主题。
3. 测量与截图在**同一次页面状态**内完成（测量后不重新加载、不改状态）。
4. 场景：base（正常）/ coexist（双面板共存）/ savefail（保存失败：页面内 fetch 桩让 PUT /state 返回 500，
   断言「保存失败」如实显示、改动仍在页面上、恢复传输后能再保存成功）/ submitfail（提交失败：
   POST /submissions 返回 500，断言原因、保留情况与「重新提交」入口）。
5. 尺寸 1440x900 / 1024x768 / 800x600（桌面最小窗口）/ 480x600（浏览器窄窗口），亮/暗各一套。
6. 服务端已按版本事实保护整板写入（409 stale_state）：脚本每次先 GET 当前 seq 再 PUT，
   在空板面与非空板面上都能重复跑（这是本轮有意的服务端保护，不是脚本 bug）。

## E2 的旅程与分层

| 场景 | 内容 | 层 / 模拟 |
| --- | --- | --- |
| J1 | 草稿冲突 → 选服务器稿 → 本机删除失败 → 重试 → 关闭浏览器进程 → 同一持久目录重开 → 编辑入口恢复 | ④真实 HTTP + ⑤真浏览器 + ⑥正常关闭；存储故障为模拟 |
| J2 | 删除失败未重试就**强制结束**进程 → 重开后处理入口与真实事实仍在 | ⑤ + ⑥强制结束；存储故障为模拟 |
| J3 | R3 连续编辑（两版正文）+ N6 完整附加字段 → 关闭重开 → 恢复且不自动形成正式改动 | ④ + ⑤ + ⑥正常关闭 |
| J4 | N3 待决定撤回项：界面「继续」真的发出带 decisionIds 的撤回执行请求 | ④ + ⑤ |
| J5 | F1 关键路径：取消影响确认 + 受控延迟下用真实指针移动卡片 | ④ + ⑤；CDP 传输层延迟为受控模拟 |

## 装置注意（实测踩过的坑）

- **每个旅程场景必须用全新 profile**：同一 profile 被历史实例用过时，旧实例退出会把旧 localStorage
  快照写回，看起来像「已删除的本机记录又出现」。journey.mjs 已按场景自动生成新 profile
  （%TEMP%\qio-sr-profiles/<场景>-<时间戳>）。
- **「正常关闭重开」要用 closeGraceful（CDP Browser.close）**，直接 child.kill() 不会让
  Chromium 把最近的 localStorage 写入落盘（本装置实测）；J2 的强制结束是**刻意**用 kill。
- 服务端 PUT 已按版本事实保护：任何写板面的脚本都要先 GET 当前 seq 再写。

## 在集成分支上复核

E2/E1 的脚本默认按自身位置解析工作树；复核集成分支时**不需要**把脚本复制过去，
用 QIO_SR_ROOT 指向那个工作树即可（不会写对方的工作树）：

    $env:QIO_SR_ROOT='D:\qio-dev\qio-src-lead'
    $env:QIO_SR_DATA_DIR=(Join-Path $env:TEMP 'qio-sr-data-after')
    node scripts/state-recovery-verify/run-with-env.mjs -- capture.mjs --label=after --scenes=coexist,savefail,submitfail --themes=light,dark
    node scripts/state-recovery-verify/run-with-env.mjs -- journey.mjs --label=after

前端/后端的**反例测试**仍然要在被验收的工作树里跑（它们 import 那一份源码）：
把 frontend/src/acceptance-state-recovery 与 backend/tests/test_state_recovery_d_acceptance.py
一并带过去（或先合并本分支）。跑完在报告里注明**核对时的确切 SHA**。
本次第一步的基线红证据来自 b3245e5；E1/E2 的 before 证据来自同一 SHA。
