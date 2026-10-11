# C（界面与审美）验收证据与复跑脚本

这些脚本只用于**验收**，不参与产品打包。它们要回答的都是「不能靠肉眼下结论」的问题：
亮色辅助文字到底能不能读、工具栏在窄窗口到底多高、失败原文里有没有开发术语、
三声部字体是不是真的加载了。

## 文件

| 文件 | 作用 |
| --- | --- |
| `serve.mjs` | 起/停验收环境：后端 8921 + 前端 5421，**独立临时 QIO_DATA_DIR**，前端用 `frontend/vite.e2e.config.ts`（见下） |
| `capture.mjs` | 同场景「前/后」截图 + 客观测量：4 尺寸 × 明暗 × 6 场景（base / chat / coexist / fail / savefail） |
| `cdp.mjs` | 自用 CDP 驱动：元素裁剪截图、PNG 解码、**真实像素对比度**、CDP 平台字体查询 |
| `fontcheck.mjs` | 三声部字体是否真的加载（字体请求状态 + 平台字体 + `document.fonts.check`） |
| `compare.mjs` | 把 before/after 两份报告读成一张表，写出 `shots/closure-c-compare.md` |
| `shots/` | 提交入库的证据：`before/`、`after/` 同场景 PNG，两份报告 JSON，字体报告，对照表 |

## 怎么跑

```powershell
# 1) 起环境（独立临时数据目录；脚本自己会写 pid 文件，--stop 收尾）
node scripts/closure-c-verify/serve.mjs
# 2) 采前/后（同一份铺底数据与同一套步骤，所以可以直接对照）
node scripts/closure-c-verify/capture.mjs --label=before
node scripts/closure-c-verify/capture.mjs --label=after
# 3) 字体证据 + 汇总
node scripts/closure-c-verify/fontcheck.mjs --theme=light --out=scripts/closure-c-verify/shots/fontcheck-light.json
node scripts/closure-c-verify/fontcheck.mjs --theme=dark  --out=scripts/closure-c-verify/shots/fontcheck-dark.json
node scripts/closure-c-verify/compare.mjs
```

可选过滤：`--sizes=800x600 --scenes=chat,fail --themes=light`（报告会写成 `*-report-partial.json`，不会覆盖全量报告）。
`--app` / `--backend` 可以指向别的实例；端口默认 5421 / 8921。

## 环境隔离（硬要求）

- `QIO_DATA_DIR` 固定指向独立临时目录（默认 `%TEMP%\qio-closure-c-data`），**绝不继承用户真实数据目录**；
- 后端以 `QIO_DEV_INSECURE=1` 启动，只跑本机 API，不需要任何真实 Key，也不做真实模型调用；
- 端口 8921 / 5421 与其它工作区错开，浏览器调试端口 9777 + 独立 profile（启动前会清掉同 profile 的僵尸实例）。

## webfont 403：原因与处理

本工作区的 `frontend/node_modules` 是**指向别的检出的 junction**。Vite 默认的 `server.fs.allow`
按解析后的真实路径校验，junction 指出去的字体文件会被判成「不在白名单」→ woff2 一律 403，
字体静默回落到系统字体。**字体没加载就做排版/对比度验收是不可信的。**

处理：验收前端改用 `frontend/vite.e2e.config.ts`（已存在，只做一件事：把 node_modules 的
真实路径补进 `server.fs.allow`，不改产品配置）。实测结论：

- `fontcheck-light.json` / `fontcheck-dark.json`：字体请求 **13/13 全 200**（400 权重 8 片 + 600 权重 5 片）；
- CDP 平台字体查询（`.im-title`）：`Noto Serif SC ExtraLight SemiBold`，`isCustomFont: true`
  —— 标题**真的**用本地打包的中文衬线渲染，不是回落字体；
- `document.fonts.check`：serif / sans / mono 三项均为 true。

## 测量方法（都是去噪后的硬数字）

- **对比度**：先把元素裁成小图（PNG 解码到像素），取该区域出现次数最多的颜色当**真实背景**，
  取与背景亮度差最大的像素当**真实文字**，按 WCAG 相对亮度算比值。不是把令牌代进公式算。
- **工具栏高度/行数**：直接量 `[data-im="board-toolbar"]` 的 rect；行数 = `.tb-main` 直接子元素
  按垂直区间是否相交归并（只看 top 会把居中对齐的差异数成假行）。
- **裁切**：浮层/工具栏/面板的 rect 是否越出视口；**竖排**：叶子文本节点宽 < 1.6em 且高 > 2.4 行高；
  **不可达按钮**：中心点 `elementFromPoint` 命中的不是它自己或其后代。
- **开发术语扫描**：把整页可见文本与关键文案拼起来，扫
  `stale_check / stale_state / checkId / impact_confirmation_required / /api/ / draft_too_long / undefined / NaN / " seq"`。

## 结论（前 → 后，全量数字见 `shots/closure-c-compare.md`）

| 项 | 前 | 后 |
| --- | --- | --- |
| 快捷键说明对比度（亮色 / 暗色） | 3.28 / 2.86 | **4.75 / 4.73** |
| 草稿状态、占位说明 | 3.28 / 2.86 | **4.75 / 4.73** |
| 失败原因正文对比度 | 4.07 | **9.85** |
| 可见文本里的开发术语 | `/api/` | **无** |
| 1440×900 常态工具栏 | 51px / 1 行 | 51px / 1 行（无回退） |
| 800×600 常态工具栏 | 80px / 2 行 | 80px / 2 行（≤96px 达标） |
| 480×600 常态工具栏 | 108px / 2 行 | **104px / 2 行** |
| 480px 快捷键说明 | `display:none`（不可达） | 可见，4.73–4.75 |
| 裁切 / 逐字竖排（全部场景） | 0 / 0 | 0 / 0 |
| 聊天 × 批量列表重叠面积 | 0（1440/1024/800 明暗实测） | 0（同上；480 走切换条，只展开一个） |
| 明暗两主题覆盖 | — | 38 个场景 = 4 尺寸 × 2 主题 × 5 场景 |

## 已知限制 / 未验证项（如实标注）

- **480×600 常态工具栏 104px**：96px 那条口径是 800×600 的要求（实测 80px 达标）；480 只做到
  「两行、无竖排、无裁切、按钮全部可达」。再压就要动「允许查看范围必须常驻可见」这条契约，故未做。
- **错误态的工具栏更高**：1440×900 失败场景 101px（含失败原因 + 保留情况两行）。这是刻意保留的
  ——上一轮验收明确要求「很长的失败原因在默认区完整显示」，本轮不去截断它。
- **12 个「组框按钮被覆盖」**：`BoardGroupFrame` 的 `设为有序 / 解除组` 与组内卡片的 `移出`
  在部分尺寸下被卡片或 `p.banner.view-hint` 压住（`elementFromPoint` 命中别的元素）。
  这是**改造前就存在**的结构问题（组框 `z-index: 2` 在卡片 `z-index: 4` 之下），本轮未改：
  抬高组框会反过来盖住卡片标题与拖动区域，属于跨组件权衡，需产品决定。
- **聊天与批量列表共存（实测）**：1440×900 / 1024×768 / 800×600 在**明暗两主题**下都直接并排展开，
  重叠面积 **0**、批量入口与聊天入口都不压工具栏（`shots/closure-c-compare.md` 的 coexist 段，前后一致）；
  480×600 触发切换条「空间不足，只展开一个面板（内容都还在）」并给出「看对话 / 看审批列表」两个入口，
  实际只展开批量列表（聊天入口仍在视口内：x 389–468，工具栏 x 16–376）。几何计划由
  `interactive/overlayLayout.ts` 负责，本轮 C 未改这部分行为，只做实测记录。
- **一次采集时序问题（已复现排除）**：800×600 亮色那一格曾拍到「批量入口被聊天面板盖住 5640px²、
  批量列表没打开」；随后**单独重跑 2 次都是重叠 0、批量列表正常展开**，因此判定为采集时几何还没落位
  （`capture.mjs` 里对 coexist 场景已加「先刷新意图 → 写批次记录 → 重载 → 再点开」的等待）。
  这一类偶发不当作界面缺陷，但也没有被算作「已通过」的证据。
- **reduced-motion**：`base.css` 在 `prefers-reduced-motion`/`data-motion=reduced` 下把过渡归零，
  本轮没有新增动画，也没有单独跑该开关下的截图。
- 截图来自 headless（swiftshader 软件光栅）真实渲染，字体已确认实际加载；但**手感**（拖动的跟手程度）
  不是这些脚本能覆盖的。
