# A 组（页面结构与导航）· 验证记录

分支 `feat/mp-20261027-a-page`，worktree `D:\qio-dev\qio-mp-a`，基线 `d8cdcc41`。

## 怎么复现

前置：前端 dev 服务（A 用 5104）与共享开发后端 127.0.0.1:8734 在跑。
```powershell
# 起前端（backend 地址由 VITE_QIO_BACKEND_URL 指定）
cd D:\qio-dev\qio-mp-a\frontend
$env:VITE_QIO_BACKEND_URL='http://127.0.0.1:8734'
npm run dev -- --port 5104

# 跑几何 + 截图（playwright-core：QIO_PLAYWRIGHT_CORE 或 NODE_PATH）
cd D:\qio-dev\qio-mp-a
$env:NODE_PATH='<装 playwright-core 的 node_modules>'
node docs/mp-verify/a-page/shot-mp-a.mjs http://localhost:5104
```
输出：本目录 `01..08*.png` + `report.json`（每一步的几何读数）。

## 截图索引

| 文件 | 场景 | 看什么 |
| --- | --- | --- |
| `01-default-1440x970.png` | 默认浏览态（1440×970，内容区 900 高） | 星球在**内容区域**水平+垂直居中；全局栏原样；右上角页面工具行（搜索 + 管理） |
| `02-managing-1440x970.png` | 管理展开 | 只剩**右半球**（左半球被视口左边缘切掉）；球心在内容区 x=0；管理区 640px 在右侧；工具行左移让开 |
| `03-knowledge-tab.png` | 知识标签页 | 三标签切换不换景；B 的知识面板整块接管管理内容区 |
| `04-memory-overview.png` | 记忆 → 话题概览 | 「正在浏览 / 对话当前话题」并排写清；底部「回到对话继续」；讨论记录/关联知识/关联实体分区 |
| `05a-pending-view.png` | 待处理（辅助入口） | 辅助入口占据同一块管理内容区；返回恢复进入前标签页 |
| `05b-global-search-results.png` | 全局搜索（有结果） | 关键词「默认」→ 话题分组命中；搜索独立于当前标签页 |
| `05c-global-search-empty.png` | 全局搜索（空结果） | 空态说清「没有匹配」，不是空白列表 |
| `06-collapsed-1440x970.png` | 收起管理 | 回到默认浏览态（球心回到内容区中央） |
| `07-narrow-900x820.png` | 窄窗口（900×820） | 上下排布：星球在上、管理内容在下；无横向滚动 |
| `08-narrow-scrolled.png` | 窄窗口滚动后 | 内容区整体一层纵向滚动，管理内容可读 |

## 关键读数（`report.json`）

窗口 1440×970，`--titlebar-height` = 70px，内容区 = 1440×900：

| 项 | 读数 | 判定 |
| --- | --- | --- |
| 默认态球体屏幕半径 | **453px** | 基线在 1440×900（画布 900 高）实测 452.67px —— 同一画布高度下差 0.07%，美术一致 |
| 默认态球心 | (720, 520) | 内容区中心 = (1440/2, 70 + 900/2) ✓ |
| 管理态 canvas 中心 x | **0.0** | 球心落在内容区左边缘（±2px 内） |
| 管理态球体半径 | **453px**（与展开前相同） | 画布尺寸全程不变 → 直径绝对不变 |
| 管理区宽度 / 左边界 | 640 / 800 | 球体右边界 = 0 + 453 < 800 ✓ 右半球完整可见，管理区不压球 |
| 展开 / 收起过程中的 `--mp` 中间值 | 4 / 4 个 | 有过程，不是瞬跳（采样间隔受 CDP 往返限制，实际曲线见 C 的逐帧工具） |
| 文档横向溢出 | 0 | `scrollWidth <= clientWidth` ✓ |
| 窄窗口（900×820） | stacked = true，`overflow-y: auto`，横向溢出 0 | 上下排布 + 内容区一层纵向滚动 |
| 窄窗口滚动 | scrollTop 可移动，横向 0 | ✓ |

## 与契约/Lead 口径的对应

- 进度只消费 C 的 `usePlanetManageTransition`（`--mp` / `data-mp` / `.managing`）；
  **`.managing` 类名与进度同步**（progress > 0 才撤），所以收起时管理区与裁切都有完整过程。
- 管理展开**不做任何重对焦/相机动作**：画布尺寸不变、只有 CSS 位移；画布尺寸观察器只调 `planet.resize()`。
- 窄窗口判定在脚本里（内容宽 ≥ 1024 且「内容宽 − 管理区宽 ≥ 球体半径 × 1.15」），球体半径取
  `planet.sphereScreenRect()` 的真实值，不是固定像素。
- 星球美术、相机参数、入口小球 ↔ 全屏连续体未改（连续体编排的既有测试仍在跑）。

## 已知限制 / 未做项

1. **待处理 / 整理记录内容暂时复用基线 `MemoryPendingPanel`**：B 的 `PendingItems.vue` /
   `MaintenanceRecords.vue`（契约 §4.2）还没落地。管理区里这是**唯一两处**替换点
   （`ManagementArea.vue` 对应两个 `<div data-view-panel>` 分支 + 两行 import）。
   副作用：两个辅助入口进去看到的是同一个基线组件（它自己还有内部分组）。
2. **`stores/planetPending.ts` 是薄占位**：只读 `api.listMemoryPending().total`；
   实体更新候选的合并由 B 的真实实现负责（B 交付后整文件覆盖）。角标数字因此只统计「内存待确认」。
3. **`usePlanetManageTransition.ts` 已换成 C 的真实实现**（连同 C 的 `planet/manageMotion.ts` 与
   `composables/usePlanetScene.ts` 原样带入本分支，内容未改）——见提交信息。
4. 单元测试里**管理态的逐帧进度走「减少动态效果」路径**：jsdom 的 rAF 时间戳与 `performance.now()`
   不同源，动画中间态在单测里不可靠；真实动画路径由本目录的浏览器截图 + `data-pm` 读数覆盖
   （C 另有逐帧工具）。这不是产品缺陷，是测试环境限制。
5. `docs/status.md` 没有改：三组会同时改同一个文件，留给 Lead 在集成时统一回填。
6. 基线 ↔ 改版的美术像素对比（球心对齐裁剪 + 像素扫描）按 Lead 口径由 **C** 出图；
   我这里只提供「同一画布高度下的球体半径」这一项数值对照（453 vs 452.67）。
