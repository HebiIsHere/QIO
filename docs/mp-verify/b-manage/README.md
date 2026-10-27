# B 组（管理流程）视觉验证

分支 `feat/mp-20261027-b-manage` · 基线 `d8cdcc41`

验证环境（本机真实跑起来，不是静态稿）：

- 后端：`127.0.0.1:8751`，`QIO_DATA_DIR=D:qio-dev.mp-data-manage`（隔离数据目录），
  `QIO_DEV_INSECURE=1` / `QIO_DISABLE_DB_CHECK=1` / `QIO_ALLOWED_ORIGINS=http://127.0.0.1:5105`。
- 前端：`npm run dev -- --port 5105`，`VITE_QIO_BACKEND_URL=http://127.0.0.1:8751`。
- 截图：仓库自带的无依赖 CDP 探针 `scripts/visual_probe.mjs`（1440×900 / 980×800 两档窗口）。
- 数据：知识（3 条生效 + 1 条待审核提案）、实体卡 2 张（含关系与用户修正）、
  1 条实体更新候选、1 个维护批次（2 条真实操作 + 1 条整理事项）——全部通过产品自己的
  服务（KnowledgeService / EntityCardService）写入开发库；维护记录按迁移 47 的真实表结构写入。
  这些只是**截图用的开发数据**，没有进入任何产品代码路径。

> 截图时的挂载方式：新 IA 里的「待处理 / 整理记录」由 A 的 `PlanetView.vue` 接入；B 不修改
> `PlanetView.vue`。为了真实看到组件，截图时在本地**临时**把 `PendingItems` / `MaintenanceRecords`
> 挂到「记忆」页签下（截图后立即 `git checkout` 还原，未提交）。因此图中面板顶部约 8px 被
> 全局栏压住，是**基线 PlanetView 布局**（A 的 `.planet-content` 会把内容整体放到全局栏下方），
> 与 B 的组件无关。

## 截图与一句说明

| 文件 | 说明 |
| --- | --- |
| `b-knowledge-list.png` | 知识列表：搜索、分类/状态/来源筛选、每条的来源/范围/话题；点条目进详情 |
| `b-knowledge-detail.png` | 知识详情：来源/分类/状态/适用范围（可改）/关联话题/依据数/版本与时间，批准/打回/标记结束/归档都在 |
| `b-knowledge-draft.png` | 草稿化编辑：明确的草稿输入层（「保存后才写入新版本」），保存前不改服务端 |
| `b-knowledge-unsaved.png` | 未保存修改时返回：先出确认层（继续编辑 / 放弃修改并返回），不悄悄丢弃 |
| `b-entity-list.png` | 实体列表：名称/属性数/关系数/类别，点条目进详情 |
| `b-entity-detail.png` | 实体详情（阅读态）：摘要/类别/身份/创建依据/证据数/别名/属性（含置信度）/关系/修订与时间 |
| `b-entity-draft.png` | 实体草稿态：摘要、类别、别名、属性（值+置信度）、关系的增删全部只改本地；顶部一行说明「还没提交」 |
| `b-entity-unsaved.png` | 实体未保存返回：同样的未保存确认层 |
| `b-pending-list.png` | 待处理列表：知识建议 / 整理问题 / 实体更新候选合并在同一个清单，数量是两类接口的 true total |
| `b-pending-detail.png` | 事项详情：正文、分类、范围、依据、版本与更新时间；确认 / 修改后确认 / 不保存 |
| `b-records-list.png` | 整理记录列表：状态 / 触发原因 / 起止时间与耗时 / 统计 |
| `b-records-summary.png` | 单次整理详情：摘要、受影响条目（默认折叠）、整理事项；整批撤回走中央弹窗 |
| `b-records-detail.png` | 点受影响条目才展开修改前后对比（默认不铺开全部差异），另可见「恢复这一项…」入口 |
| `b-records-narrow.png` | 窄窗口（980×800）：内容仍可读可操作，无横向滚动 |
| `b-list-knowledge.png` | 排版核查（集成轮）：知识列表卡左对齐、正文占满卡片宽度；长话题名单行省略且只出现一次 |
| `b-list-entity.png` | 排版核查：实体列表卡「名称 + 类别」一行、元数据在下一行，全部左对齐 |
| `b-list-pending.png` | 排版核查：待处理卡层级为「标签行 → 加粗标题 → 正文」，一条竖线左对齐 |
| `b-list-records.png` | 排版核查：整理记录批次卡「状态 + 触发原因（左）/ 时间（右）」+ 统计行，左对齐 |
| `b-list-records-detail.png` | 排版核查：单次整理详情里的受影响条目同样左对齐（折叠差异区不歪） |

## 排版核查（2026-10-27 集成轮：列表卡被全局行式类居中）

四个列表卡的根类（`.k-item` / `.e-item` / `.p-item` / `.mr-batch` / `.mr-item`）此前同时挂了
全局行式类 `qio-row`（`align-items:center`），列方向下变成「内容水平居中 + 缩窄」。
现在四个根类都不再挂行式类，改为显式 `align-items: stretch; justify-content: flex-start`，
hover / 焦点 / 选中表面改成组件内自写（视觉不变）。合并树上真实浏览器实测：

| 元素 | alignItems | 卡片 left / width | 内容 left / width |
| --- | --- | --- | --- |
| `.k-item` | stretch | 799 / 612 | 812 / 586（= 卡片 − 两侧 13px padding） |
| `.e-item` | stretch | 799 / 612 | 名称 812、元数据 812 |
| `.p-item` | stretch | 799 / 612 | 标题 812 / 586（font-weight 600） |
| `.mr-batch` / `.mr-item` | stretch | 799 / 612 | 左对齐 |

- 知识列表关联话题：卡片内话题名**只出现 1 次**（scope 里的「话题：X」在有链接时不再打印），
  长名单行省略（`text-overflow: ellipsis` + `white-space: nowrap`），`title` / `v-qio-tip` 给全名。
- 窄窗口 980px：列表滚动区与整页横向溢出均为 0。
- 回归兜底：`__tests__/mp-b-list-alignment.test.ts` 用源码静态断言守住
  「四个列表根类必须 stretch、不得再挂 qio-row、hover/焦点表面仍在」以及
  「实体列表元数据与详情元数据不得共用类名」（此前同名导致列表文字被详情列样式居中）。

## 客观测量（探针输出）

- 管理面板宽度：1440 窗口 = **640px**；980 窗口 = **490px**（仍是真实面板，不是抽屉）。
- 横向溢出：整理记录滚动区 `scrollWidth - clientWidth = 0`；整页 `document.scrollingElement` 溢出 0；
  980 窗口下同样为 0。
- 圆角：受影响条目的计算样式 = `14px`（`var(--qio-ui-radius)`）。
- 控制台：0 error / 0 exception；网络：0 个 4xx/5xx（WebGL 软渲染的 perf warning 与本次无关）。
