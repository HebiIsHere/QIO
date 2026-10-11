# 子智能体 A（布局与审美）报告 —— 互动板修复与第二轮审美优化

- 工作区：`D:\qio-dev\qio-ui-a`（分支 `wt/ui-a-visual`，基线 `12266d9`）
- 只写了自己名下的文件：`components/interactive/{BoardToolbar,AddMenu,BoardSearchPanel,BoardCard,BoardGroupFrame,SelectionMenu}.vue`
  与 `components/interactive/__tests__/BoardToolbar.test.ts`；本报告与 `docs/interactive-ui-screenshots/`。
- 未碰：`views/InteractiveView.vue`、`interactive/types.ts`、`services/interactive.ts`、
  `scripts/interactive-verify/fe-scenarios.mjs`、`interactive/board.ts`、`stores/*`、`styles/interactive-shell.css`、
  `SubmitCluster.vue` 等一切非我名下文件。

## 1. 结论（先说数字）

```
                       改前（旧代码）        改后（本轮）          目标
800×600  工具栏高        285px                84px                 ≤ 约 96px  ✓
800×600  工具栏行数      4                    2                    ≤ 2 行     ✓
1024×768 工具栏高        220px                84px                 —          ✓
1440×900 工具栏高        172px                60px（一行）         —          ✓
浮层相交面积（6 组档位×主题，共 18 项检查）  全部为 0 px²
```

5 个组件 + 1 个新组件 + 1 个测试文件，`589 insertions(+), 527 deletions(-)`。

## 2. 改了哪些文件、各自为什么

| 文件 | 改动 |
| --- | --- |
| `BoardToolbar.vue` | 常态只留：添加、搜索、撤销/重做、一组「视图」（选择/框选/连线）；分组类操作整块移除；短回执改成**浮在栏外**（不参与高度）；去掉重复分隔线；窄窗口让提交区独占一行、工具栏吃满可用宽度 |
| `SelectionMenu.vue`（新建） | 选中对象附近的整理菜单：所选成组 / 加入组 / 移出组 / 解除组 / 设为有序 / 取消有序 / 合并组 / 删除所选。条件不满足的操作**不出现**（不是禁用）；面板位置按**实际可用区域**（含板面滚动区的裁剪）算，朝卡片外侧展开 |
| `BoardCard.vue` | 信息分层：材料类型 → 标题 → 正文 → 元信息（状态文字 + 时间）；长网址/长名称换行、代码块自身横向滚动；悬停态与选中态区分；局部工具栏只留卡片自身操作（编辑/复制/折叠/隐藏/书签/删除/勾选）并接上 SelectionMenu；**只由所选里的最后一张卡片渲染工具栏** |
| `BoardGroupFrame.vue` | 组框改实线细边 + 很淡的底（比卡片轻），头部不再压一块实心底色；按钮去掉实底；成员数量退到次级；默认名提示不再拼「组 N」 |
| `BoardSearchPanel.vue` | 阴影走 `--elev-floating` 令牌、标题字号、结果悬停态统一 |
| `AddMenu.vue` | 五类入口从「五行标题+说明」（实测 320×369）改成**一行的五个入口 + 一行说明**（520×77）；说明文字改为读屏可见 + 悬停提示；按钮字号与工具栏对齐 |
| `__tests__/BoardToolbar.test.ts` | 按 §9.6 新规则重写（含反例） |

## 3. 跑过的命令与真实输出

### 3.1 类型与单测

```
> cd frontend; npx vue-tsc --noEmit
=== tsc exit: 0 ===            （无输出）

> npx vitest run src/interactive src/components/interactive
 ✓ src/interactive/__tests__/submission.test.ts (16 tests)
 ✓ src/interactive/__tests__/board.test.ts (60 tests)
 ✓ src/interactive/__tests__/impactDialog.test.ts (8 tests)
 ✓ src/interactive/__tests__/batchTray.test.ts (10 tests)
 ✓ src/interactive/__tests__/approval.test.ts (46 tests)
 ✓ src/interactive/__tests__/viewport.test.ts (25 tests)
 ✓ src/components/interactive/__tests__/BoardToolbar.test.ts (11 tests)
 ✓ src/interactive/__tests__/boardCanvas.test.ts (24 tests)
 ✓ src/interactive/__tests__/chat.test.ts (24 tests)
 Test Files  9 passed (9)      Tests  224 passed (224)
=== vitest exit: 0 ===

> npx vitest run            （整仓回归）
 Test Files  112 passed (112)  Tests  1166 passed (1166)
=== vitest exit: 0 ===
```

改前基线同两条命令：tsc 0、`Tests 220 passed (220)`（我的测试文件从 7 条变成 11 条）。

### 3.2 实机（后端 8911 / 前端 5411）

后端：`backend\.venv` 在本工作区不存在（未提交），改用主检出解释器 + 本工作区源码：

```
$env:QIO_DEV_INSECURE=1; $env:QIO_DATA_DIR="$env:TEMP\qio-ui-a"; $env:PYTHONPATH="D:\qio-dev\qio-ui-a\backend\src"
& 'D:\qio-dev\qio-ui\backend\.venv\Scripts\python.exe' -m uvicorn agent.main:create_app --factory --host 127.0.0.1 --port 8911
INFO:     Uvicorn running on http://127.0.0.1:8911

$env:VITE_QIO_BACKEND_URL='http://127.0.0.1:8911'
node frontend/node_modules/vite/bin/vite.js --config frontend/vite.e2e.config.ts --port 5411 --host 127.0.0.1
VITE v6.4.3  ready in 1201 ms   ➜  Local:   http://127.0.0.1:5411/
```

探针：**本机 Chrome 未使用**，按提示改用 Edge 驱动 `node scripts/visual_probe_d3.mjs "@<steps.json>"`
（脚本内 `CHROME = msedge.exe`，CDP 9666，独立 profile）。步骤文件放在 `%TEMP%\qio-a-probe\`（不污染仓库）。
场景：真实点开「添加」菜单加 6 张卡片（4 文字 + 1 网址 + 1 代码 + 1 文件），其中两张用编辑器写入真实文字，
再逐档位逐主题「选中卡片 → 打开整理菜单 → 打开添加菜单 → 打开板内搜索」，每次都量几何。

## 4. 实测数字（截图与探针同一次运行）

工具栏（`[data-im="board-toolbar"]` 的 getBoundingClientRect）：

| 档位 | 主题 | 高 | 宽 | 行数 | 距底 |
| --- | --- | --- | --- | --- | --- |
| 1440×900 | 暗 | **60** | 1069 | 1 | 16 |
| 1440×900 | 亮 | 60 | 1069 | 1 | 16 |
| 1024×768 | 暗 | **84** | 888 | 2 | 12 |
| 1024×768 | 亮 | 84 | 888 | 2 | 12 |
| 800×600 | 暗 | **84** | 680 | 2 | 8 |
| 800×600 | 亮 | 84 | 680 | 2 | 8 |

行数按 `.tb-main` 直接子元素的顶边分带计数（改后按 24px 分带，因为同一行内两个子元素高度不同会差 9px；
改前按 8px 分带）。改前基线同口径：1440×900 = 172px/3 带，1024×768 = 220px/4 带，800×600 = 285px/4 带。

提交区位置：右端同一行（1440×900：`提交区 w680 h50`，提交按钮 `97~116×29~41`）；
≤1080 独占一行（800×600：`提交区 w662 h34 t549`，提交按钮 `w97 h29`，仍在视口内且不被遮挡）。

浮层相交（18 组检查，全部 0 px²）：

| 检查项 | 1440×900 | 1024×768 | 800×600 |
| --- | --- | --- | --- |
| 卡片工具栏 × 卡片本体 / 标题 / 正文 / 编辑框 | 0 | 0 | 0 |
| 整理菜单 × 卡片工具栏 / 卡片 / 标题 / 编辑框 / 四个连接点之和 | 0 | 0 | 0 |
| 添加菜单 × 卡片工具栏 / 卡片 | 0 | 0 | 0 |
| 板内搜索 × 卡片工具栏 / 整理菜单 | 0 | 0 | 0 |
| 短回执（浮在栏外）× 卡片工具栏 / 整理菜单 | 0 | 0 | 0 |
| 任一浮层伸出视口 | 否 | 否 | 否 |

整理菜单实测位置（贴工具栏外侧、不进卡片）：1440×900 `l827 t125 r1099 b226`、
1024×768 `l64 t125 r336 b226`、800×600 `l41 t125 r313 b226`（都在视口内）。

另外三项实机检查（800×600 暗）：

```
zoom-before:100  → 在整理菜单上滚轮 240 →  zoom-after:100     （浮层滚动不穿透板面缩放）
Esc           →  {"menu":false,"cardToolbar":true,"points":4}  （Esc 先关菜单，选择与连接点保留）
Tab 后聚焦撤销 →  {"active":"undo","outline":"2px solid rgb(232, 120, 189)"}  （= --focus-ring，键盘焦点可见）
```

## 5. 逐条对照契约 §9.6

- 工具栏常态只留必要入口 ✓；分组/加入组/移出组/解除组/有序/合并/删除所选全部移出，进入选中对象附近的整理菜单 ✓；
  未选中时**没有一整排禁用按钮**（未选中时工具栏里唯一可能禁用是撤销/重做）✓
- 消除重复分隔线 ✓：提交区分界线只画一条（原来 `.tb-submit` 与 SubmitCluster 各画一条）；
- 消除重复包裹：工具栏从「4 个 cluster + 3 条分隔线 + 计数 + 提交」收成「编辑组 + 提交区」两块 ✓
- 信息层级：已选数量从工具栏移到整理菜单标题（`已选 N 张 · 组：…`），卡片元信息行放状态文字 + 时间 ✓
- 卡片分层、组框更轻、正式内容实线（组框改实线细边）、选中≠悬停、正文 14.5px / 主要操作 12.5px（不缩成元信息字号）✓
- 令牌：我的 6 个文件里**没有任何硬编码色值**（`#hex` / `rgb()` / 具名色）实测 grep 为零；
  阴影用 `--shadow-1/2`、`--elev-floating`，焦点环用 `--focus-ring` ✓
- 作用域：这 6 个组件只被互动板引用（`InteractiveView.vue` / `BoardCanvas.vue`），对话页、设置页、星球导航不受影响 ✓
- 文案：新增文案里没有 adapter / 接口 / 求差 / 状态机 / DOM 之类开发用语 ✓

## 6. 没做到 / 没验证 / 需要别人配合

1. **提交区（C 的文件）太占高度**：`SubmitCluster.vue` 自带「已保存长句 + 提交状态 + 可见范围」三行文字与
   重复的 `border-left`，800×600 实测它一个组件就 151px 高。我在 `BoardToolbar.vue` 里用
   `:deep()` 做了**容器级收束**（排成一行、收起与顶栏 `save-status` 重复的那句、可见范围最多一行、
   窄档隐藏「本次改动条数」摘要、提交按钮收一档），换来 84px 的总高。
   **这是临时措施，按冻结的 data-im 钩子选中，C 把 SubmitCluster 自己做短之后可以整块删掉**；
   如果 C 认为不该由容器管，请改成组件自身默认紧凑，我这边删掉 `:deep()` 块后重新测量。
2. **多选时工具栏位置仍是画布的旧坐标**：`BoardCanvas` 只在「恰好选中 1 张」时重算浮层位置
   （`primarySelectedId` 要求 `selection.length === 1`），多选后位置停在最后一次单选处。
   我做的是「只由所选里的最后一张卡片渲染这一份工具栏」（原来每张选中卡片都渲染同一份、位置相同、
   叠成一摞，点击命中的是最上面那张 = 板面顺序最后的一张，可能作用到用户没在看的卡片上）。
   **要彻底修需要 `BoardCanvas.vue`（B 的文件）在 `selection.length > 1` 时也重算 overlay**。
3. **编辑框打开时没有专门复测**「浮层不遮正在编辑的内容」：探针里 `cardToolbar_editor` 与 `menu_editor` 两项
   在未进入编辑态时是 0（因为没有编辑框元素）。几何上工具栏被画布放在卡片外（实测卡片工具栏与卡片本体
   相交 0），但**「打开编辑器后再开菜单」这一条我只有代码推断，没有截图**。
4. **缩放（≠100%）后浮层坐标**：`placeOverlay` 把「客户端坐标 − 板面左上角」直接当行内 left/top，
   而 `.board-surface` 带 `transform: scale()`，坐标会被二次缩放。这是 B 的 `BoardCanvas.vue`，
   本轮我只验证了 100% 缩放下的准确性（所有档位都是 100%）。**缩放后是否仍然准确我没有实测**。
5. **没有动画**：本轮我没有新增任何过渡/动效（拖动仍然直接跟手、无 transition），所以
   「减少动态效果」这条只是没有回退，不存在新验证。
6. **探针用 Edge**（按提示），不是 Chrome；`chrome devtools not reachable` 第一次出现时是因为上一轮探针
   残留的 msedge 子进程占着 profile，清理 16:20 之后启动的 msedge 进程并删掉 profile 目录后可正常复跑。
   页面控制台里两条 `Could not establish connection. Receiving end does not exist.` 与页面无关
   （`httpFails` 为空，无 404/500），疑为扩展相关，未影响任何断言。

## 7. 截图（`docs/interactive-ui-screenshots/`，30 张，已随提交进仓库）

命名：`a-<档位>-<主题>-<场景>.png`，`<档位>` ∈ {1440x900, 1024x768, 800x600}，`<主题>` ∈ {dark, light}，
`<场景>` ∈ {board, card-toolbar, selection-menu, add-menu, search}。

- `a-800x600-dark-selection-menu.png`：整理菜单（`已选 1 张` + `删除所选`）贴在工具栏左侧，未压卡片与连接点
- `a-1440x900-light-selection-menu.png`：宽窗口一行工具栏 + 卡片分层 + 整理菜单
- 其余为各档位各主题的板面 / 卡片工具栏 / 添加菜单 / 板内搜索对照
