# A 组（页面结构与导航）· 验证记录

分支 `feat/mp-20261027-a-page`，worktree `D:\qio-dev\qio-mp-a`，基线 `d8cdcc41`。

## 怎么复现

前置：前端 dev 服务（A 用 5104）与共享开发后端 127.0.0.1:8734 在跑。
```powershell
cd D:\qio-dev\qio-mp-a\frontend
$env:VITE_QIO_BACKEND_URL='http://127.0.0.1:8734'
npm run dev -- --port 5104

cd D:\qio-dev\qio-mp-a
$env:NODE_PATH='<装 playwright-core 的 node_modules>'
node docs/mp-verify/a-page/shot-mp-a.mjs   http://localhost:5104   # 几何 + 截图
node docs/mp-verify/a-page/frames-mp-a.mjs http://localhost:5104   # 逐帧曲线
```

## 截图索引

| 文件 | 场景 | 看什么 |
| --- | --- | --- |
| `01-default-1440x970.png` | 默认浏览态（内容区 900 高） | 星球在**内容区域**水平+垂直居中；全局栏原样；右上角页面工具行 |
| `02-managing-1440x970.png` | 管理展开 | 只剩**右半球**；球心在内容区 x=0；管理区 640px 在右侧；工具行左移让开 |
| `03-knowledge-tab.png` | 知识标签页 | 三标签切换不换景 |
| `04-memory-overview.png` | 记忆 → 话题概览 | 「正在浏览 / 对话当前话题」并排；底部「回到对话继续」 |
| `05a-pending-view.png` | 待处理（辅助入口） | 辅助入口占据同一块管理内容区 |
| `05b-global-search-results.png` / `05c-global-search-empty.png` | 全局搜索 | 三类分组 + 空态 |
| `06-collapsed-1440x970.png` | 收起管理 | 球心回到内容区中央 |
| `07-900x700-managing.png` | 900×700 展开 | 断点下调后仍**左右排布**、球体直径不变（317→317） |
| `08-stacked-760x820.png` / `09-stacked-scrolled.png` | 760×820（真正放不下） | 上下排布 + 内容区一层纵向滚动 |

## 关键读数（`report.json`）

窗口 1440×970，`--titlebar-height` = 70px，内容区 = 1440×900：

| 项 | 读数 | 判定 |
| --- | --- | --- |
| 默认态球体屏幕半径 | **453px** | 基线同画布高度 452.67px —— 差 0.07% |
| 默认态球心 | (720, 520) | 内容区中心 ✓ |
| 管理态 canvas 中心 x | **0.0** | 球心落在内容区左边缘 |
| 管理态球体半径 | **453px**（与展开前相同） | 画布尺寸全程不变 |
| 管理区宽度 / 左边界 | 640 / 800 | 球体右边界 453 < 800：右半球完整可见 |
| 900×700：stacked / 半径 | **false** / 317 → **317** | 断点 880 后 900 宽仍左右排布、直径不变 |
| 760×820：stacked | **true** | 真正放不下才上下排布（内容区一层纵向滚动，横向溢出 0） |
| 文档横向溢出 | 0 | `scrollWidth <= clientWidth` ✓ |
| 单次 `dispatchEvent(MouseEvent)` | 只切换一次（false → true） | 页面上没有第二个 click 监听 |

## 本轮修复（Lead 逐帧反馈）

1. **展开首帧不再被首次挂载阻塞**：管理区内容改为在**进场结束后预挂载**（`preMountManagement`，
   `layersIn` 变 true 后一帧挂），展开路径的顺序是「`setExpanded` 起跑 → 内容补挂（正常是空操作）」。
   逐帧采样：点击后第一帧仍是 mp=0，随后 2–46ms 内出现 0.009 / 0.067 / 0.123，
   前 300ms 内有 4 个中间值（此前是「0 停 200–330ms → 一帧跳到 0.73」）。
2. **管理区宽度不再逐帧改**：`.panel` 固定 `width: var(--mp-w)`，用
   `transform: translateX(calc((1 - var(--mp, 0)) * 100%))` 滑入；页面工具行的让位同样改成 transform。
   Chrome Performance 指标（1s 窗口）：LayoutDuration **1ms**、LayoutCount **7**、RecalcStyle 13ms ——
   转场期间没有逐帧布局。
3. **上下排布断点 1024 → 880**（见下）。

## 与契约/Lead 口径的对应

- 进度只消费 C 的 `usePlanetManageTransition`；`.managing` 与**进度**同步（progress > 0 才撤），
  收起时管理区/裁切有完整过程。
- 管理展开不做任何重对焦/相机动作：画布尺寸不变、只有 CSS 位移；观察器只调 `planet.resize()`。
- 窄窗判定（脚本）：内容宽 ≥ **880** 且「内容宽 − 管理区宽 ≥ 真实球体半径 × 1.15」才左右排布。
  为什么是 880 而不是 1024：上下排布会把星球区压到 `clamp(240px, 40vh, 360px)`，球体半径随之变小
  （=「展开后直径不变」不成立）。900×700 实测 900 − 414 = 486 ≥ 316.87 × 1.15 = 364，放得下；
  1024 更宽裕。再窄时管理区最小 380 会吃掉过多可用宽度、右半球开始被压，才退上下排布。
- 星球美术、相机参数、入口小球 ↔ 全屏连续体未改（连续体编排测试仍在跑）。

## 关于逐帧 `maxStep` 的诚实说明

本机是**共享**环境（多个任务同时在跑），headless + swiftshader 的合成帧间隔呈双模态：
同一份代码有时 7ms/帧（143fps），有时 100–180ms/帧。对照实验（直接写 `--mp` 但去掉所有消费它的 CSS、
以及给等大 div 做同样时长的 CSS transform）都无法稳定复现/消除这一模态，说明它由机器负载与软件合成决定，
不是实现里的 JS/样式布局成本（后者已有上面的 Chrome Performance 指标为证）。
因此 `maxStep` 请以 Lead/C 在自有环境（~30fps）的复测为准；我这里能稳定给出的是：
**首帧从 0 开始、前 300ms 有 ≥4 个中间值、无逐帧布局、几何不变式全部成立**。

## 已知限制 / 未做项

1. **待处理 / 整理记录内容暂时复用基线 `MemoryPendingPanel`**：B 的 `PendingItems.vue` /
   `MaintenanceRecords.vue`（契约 §4.2）未落地，管理区里只有两行 import + 两个分支是替换点。
2. **`stores/planetPending.ts` 是薄占位**：只读 `api.listMemoryPending().total`；实体候选合并在 B 的实现里。
3. `usePlanetManageTransition.ts` / `planet/manageMotion.ts` / `composables/usePlanetScene.ts`
   按 C 的交付**原样带入**（C 已授权），集成时以 C 最新版覆盖。
4. 单元测试里管理态进度走「减少动态效果」路径（jsdom rAF 时间戳与 performance.now 不同源）；
   真实动画由本目录的浏览器脚本覆盖。
5. `docs/status.md` 未改（三组同文件冲突，留给 Lead 集成时统一回填）。
6. 基线 ↔ 改版的美术像素对比（球心对齐裁剪 + 像素扫描）按 Lead 口径由 **C** 出图；
   这里提供「同一画布高度下的球体半径」数值对照（453 vs 452.67）。
