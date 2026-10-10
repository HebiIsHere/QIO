# E1 截图断言矩阵 · 基线结果（b3245e5）

- 基线 SHA：b3245e5（工作树 D:\qio-dev\qio-src-d，分支 wt/src-d-verify）
- 环境：后端 127.0.0.1:8933 + 前端 127.0.0.1:5433（frontend/vite.e2e.config.ts），
  QIO_DATA_DIR=%TEMP%\qio-sr-data（独立临时目录，QIO_DEV_INSECURE=1，无真实模型调用）。
- 命令：node scripts/state-recovery-verify/serve.mjs
         node scripts/state-recovery-verify/capture.mjs --label=baseline
- 原始产物：shots/baseline-report.json、shots/baseline-*.png（32 张）、
  evidence/e1-baseline-run.log。

## 结果

**32 格（4 尺寸 × 2 主题 × 4 场景）全部通过采集断言：失败 0。**

| 场景 | 断言（截图前实际核对） |
| --- | --- |
| base 正常 | data-theme 与亮/暗背景亮度一致（亮 lum=0.9728 / 暗 lum=0.0061）、qio-theme 已持久化、6 张卡片、6 条关系（svg.link-layer line.line）、6 个关系文字标签、2 个组、工具栏已渲染 |
| coexist 双面板共存 | 宽窗口（≥520px）：聊天面板与批量列表都打开、批量条目 ≥1、重叠面积 0；窄窗口（480）：切换条出现且两个入口可达、只展开一个面板、重叠面积为 null（只开一个） |
| savefail 保存失败 | 页面内 fetch 桩让 PUT /state 返回 500 → 详情区保存状态如实显示「保存失败」、改动仍留在页面上（cards=7）、默认区不许出现「已保存（…）」；随后恢复传输并用界面「撤销」再保存一次 → 显示「已保存」（可恢复） |
| submitfail 提交失败 | POST /submissions 返回 500 → 默认区显示失败原因与保留情况、提交按钮变为「重新提交」（重试入口可见） |

工具栏高度（真实测量，作为窄窗口密度的客观数字）：
1440x900=51px、1024x768=80px、800x600=80px、480x600=104px（失败态更高：114 / 172px）。

## 采集口径（本轮修正）

1. 每一格截图前**先断言**：主题（属性 + 真实计算背景亮度 + 持久化偏好）、卡片/关系/组数量、
   预定面板状态、目标失败的原因与重试入口；任何一条不满足即判该格失败。
2. 主题走真实持久化路径（localStorage qio-theme → 重新加载），**加载之后再断言**主题，不靠文件名。
3. 测量与截图在同一次页面状态内完成；不满足条件的格子直接判失败，不会用空板面冒充。
4. 服务端已按版本事实保护整板写入（409 stale_state）：脚本每次都先 GET 当前 seq 再 PUT，
   在空板面与非空板面上都能重复跑（这是本轮有意的服务端保护）。

## 模拟部分（如实标注）

- savefail / submitfail 的故障由**页面内 fetch 桩**制造（PUT /state 与 POST /submissions 返回 500），
  其余全部走真实前后端：真实后端、真实 GraphQL 无关；真实 localStorage、真实渲染。
- 主题由 localStorage 偏好驱动（真实用户路径），非直接改 DOM 属性。

## 未验证 / 限制

- E1 只覆盖界面与文案/几何；它不验证 F1–F3 / N1–N6 的行为正确性（那由反例测试与 E2 负责）。
- 480x600 的 coexist 是**切换条**形态（产品设计），并非缺陷；本矩阵按该形态断言。
- 未做像素级「修前/修后」对比：本轮基线只产出 before 侧；after 侧需在集成分支上重跑同一脚本
  （--label=after），再按同一 tag 对照。集成分支复核时请注明核对 SHA。
