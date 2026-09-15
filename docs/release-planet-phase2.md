# QIO 第二阶段（话题逻辑与流动星球导航）验收报告

日期：2026-09-15 · 分支：`main` · 数据：隔离目录 `%TEMP%\qio-e2e`（123 个话题，全部为假数据）

报告结构按任务书第 89 节：修改摘要 / 原有问题 / Planet 最终工作方式 / Topic 状态定义 /
Fragment 与 Anchor / API 改动 / 测试 / 视觉验证 / 性能 / 剩余问题。

---

## 1. 修改摘要

| 方向 | 改了什么 |
| --- | --- |
| Topic 导航 | 新增 `services/navigation.py::TopicNavigationService` 作为**唯一导航入口**（进入话题 / 创建话题 / 确认切换 / 从历史继续）；API、工具、turn 编排全部改走它；新增架构守卫测试扫描「谁在直接写 anchor」 |
| Anchor | 明确「Anchor = 当前对话真实继续发生的位置」；Planet 选中、检索命中、Predictor 判断都不再触碰 Anchor；失败与取消不推进 Anchor（补了专门测试） |
| Planet 浏览流 | 新增 `frontend/src/planet/browseSession.ts`：浏览序列 + 展示窗口 + 槽位回收 + 反向还原 + 冷却 + 选中锁定 + 最小展示寿命；旋转按方位角累计推动话题流（`planet/browseFlow.ts`） |
| 展示窗口 | 同屏容量固定为 16（`VISIBLE_CAPACITY`），总话题数无上限；100+ 话题下同屏仍是 16 个点 |
| 临时布局 | 新增 `frontend/src/planet/layoutSlots.ts`：按「话题 + 浏览会话」稳定种子确定性生成环形带槽位；`nodes.meta.layout` 旧坐标保留兼容但不再是核心语义 |
| Fragment | 迁移 11 增加 `fragments.source_fragment_id`；「从历史继续」创建**新片段**并记来源，旧片段零改动；`focus_fragment` 会把来源片段抬进本轮 Focus |
| Memory | 检索仍然只读（新增测试固定这一点）；接续片段的 Focus 读的是来源片段，不读空的接续片段 |
| 数据接口 | 新增 `GET /api/planet/overview`、`POST /api/planet/browse`、`GET /api/fragments/{id}/messages`；Topic Detail 不再内联 Message 原文且 `message_count` 变成真实总数 |
| 性能 | 话题点改为固定容量对象池（`planet/dotPool.ts`），进出只换数据不 new / dispose；标签 DOM 仍然只有 hover 与选中两个 |
| 动效 | 新话题从「远处低透明」淡入（420ms），回收只发生在球体背面；动效仍是惯性而非弹性；相机后撤让球体完整落在画面内（留白） |

## 2. 原有问题

| 问题 | 原因 | 修改方式 |
| --- | --- | --- |
| 第 17 个以后的话题进不了星球 | `usePlanetScene.loadTopics` 里 `buildTopics(...).slice(0, MAX_TOPICS)`：前端把「渲染上限」当成「数据上限」，超出的点根本不生成 | 数据层改成 `overview + browse` 游标分页（无上限），视觉层容量固定 16，槽位轮流承载不同话题 |
| 星球是「固定坐标 + 转 360° 看完全部」 | 话题位置持久化在 `nodes.meta.layout`，前端按位置聚簇并渲染，旋转只是换视角 | 位置改为「当前展示布局」（确定性临时槽位）；旋转推动话题流，槽位转到背面才换数据 |
| 点击话题可能被误当成「进入话题」 | 选中态与 Anchor 的边界靠约定维持，没有统一入口 | 选中只写前端状态并锁定该槽位；只有「进入这个话题 / 从这里继续」调用 Navigator |
| 「从历史继续」会重新打开旧片段 | `continue_from_fragment` 直接把 Anchor 指向旧片段，语义上等于「继续写那一段」 | 新建接续片段（`source_fragment_id` = 来源），Anchor 指向新片段；Focus 读来源片段；旧片段零改动 |
| Topic Detail 一次带回所有片段的前 50 条消息 | 详情接口把 `messages` 内联在片段里，`message_count` 还是被 LIMIT 截断的值 | 详情只给目录与真实计数；原文走 `GET /api/fragments/{id}/messages` 按页取 |
| 预测器可能「替用户切换话题」 | 预测结果与导航动作之间没有明确的待确认状态 | 明确指令（切到/回到 + 已知话题名）直接执行；推测只产生 `TOPIC_SWITCH_SUGGESTED` 与「待确认切换」，Anchor 不动 |
| 长时间浏览会不断 new / dispose 话题点 | 每次加载都重建全部 mesh | 固定容量对象池，`place()` 只改 userData / 位置 / 尺寸 |

## 3. Planet 最终工作方式

- **Topic 如何进入展示**：后端 `_sequence()` 用「稳定哈希打底 + 少量近期活跃加权」生成确定性顺序，第一圈把当前所在话题排到首位；前端按游标取批，填进窗口空位。
- **Topic 如何离开**：只有当某个槽位转到球体背面（用户看不见）、且它已经展示够 `minLifetimeMs = 720ms`、且没被用户锁定时，才把这个槽位换成队列里的下一个话题。
- **旋转如何推动话题流**：渲染循环读 `OrbitControls.getAzimuthalAngle()`，累计到 0.4 rad（约 23°）推进一步；快速甩动最多只有一步残量，真正的节奏由「最小展示寿命」兜底。
- **反向旋转如何处理**：只有**真的掉头**（相对上一次流向）才把刚离开的话题放回原槽位；继续同向旋转一律引入新话题。刚打开就反向时没有可还原记录，同样引入新话题 —— 世界在两侧延伸，不会出现「反向什么都不发生」。
- **如何避免频繁重复**：一次会话内序列本身是排列（同一圈不重复）；换圈时换种子重排；近期展示过的话题进冷却队列（默认 24），队列见底后从「离开过的话题」池里按 FIFO 取，因此长时间浏览后旧话题会自然重新出现，但不会马上从另一侧回来。
- **如何被选择**：点击/悬停只做命中与聚焦；选中的话题在查看期间被锁定，不会被回收；搜索或列表命中的话题会被注入展示窗口（不需要它本来就在某个固定地点）。
- **如何进入 Conversation**：面板底部按钮在未选片段时是「进入「X」」，选中历史片段时是「从这里继续（这个历史位置）」，两者都走 Navigator；成功后更新 Anchor、收起星球、回到对话页。

## 4. Topic 状态定义

| 状态 | 含义 | 谁能改 | 改变时机 |
| --- | --- | --- | --- |
| 当前 Topic / Anchor | 当前对话真实继续发生的位置（话题 + 片段） | `TopicNavigationService` 唯一入口 | 进入话题、创建话题、确认切换、从历史继续、成功一轮后位置推进 |
| 选中 Topic | 用户在 Planet 上正在浏览/查看的话题 | 前端选中态 | 点击点或列表项；**不改变 Anchor** |
| 展示 Topic | 此刻在展示窗口里的那一批（≤16） | 浏览会话 | 旋转推动 / 搜索注入 / 窗口初始化 |
| 引用 Topic | 为回答当前问题临时读取的另一个话题 | 只读 | 检索 / Focus / 实体卡命中；**不改变 Anchor** |
| 待确认切换 | 预测器认为「可能属于另一个话题」的建议 | 前端登记 + 用户表态 | 收到 `TOPIC_SWITCH_SUGGESTED`；确认后才是切换，拒绝保持原话题 |

## 5. Fragment / Anchor

- **从历史继续如何实现**：`TopicNavigationService.continue_from_history()` 校验片段属于该话题后，写入一条新的**开放**片段并记 `source_fragment_id = 来源片段`，Anchor 指向新片段。来源已经是当前开放片段时不开新片段（位置本来就停在那里）。
- **为什么不会修改旧历史**：新行只增加、不改旧行；`AnchorService.focus_fragment()` 把来源片段作为**只读引用**抬进 Focus（接续片段本身是空的，不注入空内容）。
- **Anchor 何时改变**：进入话题 / 创建话题 / 确认待确认切换 / 从历史继续 / 一轮**成功**后的位置推进。
- **Anchor 何时不能改变**：检索、预测、Planet 选中、失败轮次、取消轮次。`tests/test_anchor_no_advance.py` 与 `tests/test_topic_navigation.py` 固定了这两组事实。

## 6. API 改动

| 接口 | 变化 |
| --- | --- |
| `GET /api/planet/overview` | 新增。第一层数据：`topics[{topic_id,title,fragment_count,last_activity,summary_preview,visual_seed}] + total + visible_capacity`；不返回 Message |
| `POST /api/planet/browse` | 新增。返回 `{seed, pass_index, cursor, prev_cursor, next_cursor, has_more, pass_changed, total, visible_capacity, items}`；`direction ∈ {forward,backward}`，`count` 夹到 1..32 |
| `GET /api/graph/topics/{id}` | 片段的 `messages` 移除；`message_count` 改为真实计数（不再被 LIMIT 50 截断）；新增 `created_at` |
| `GET /api/fragments/{id}/messages?offset&limit` | 新增。第三层数据：原文按页取，`total` 为真实条数，`limit` 夹到 1..200 |
| `POST /api/anchor` | 走 Navigator；新增 `continue_from_history: true` 表示「新建接续片段」，响应增加 `created_fragment_id` / `source_fragment_id` |
| `POST /api/topic-switch/confirm` / `reject` | 新增。确认或拒绝「待确认切换」 |
| 认证 | 新接口全部落在既有 `/api/**` 会话令牌中间件下，`tests/test_api_auth.py` 的敏感路由清单已加入三条新路由 |

## 7. 测试

全部为仓库内可复跑的自动化测试；命令见文末。

| 测试 | 目标 | 结果 |
| --- | --- | --- |
| `backend/tests/test_planet_browse.py` | 概览轻量、同 seed 确定性、分页不重复、反向拿回上一屏、首批含当前话题、跨圈重排、容量与总数分离、空库安全 | 通过 |
| `backend/tests/test_planet_api_layers.py` | 三层接口：概览/详情不带原文、真实计数、原文分页、browse 参数校验 | 通过 |
| `backend/tests/test_topic_navigation.py` | 统一导航入口、跨话题片段拒绝、接续片段新建与来源、待确认切换、检索与预测只读、anchor 写入集中化守卫 | 通过 |
| `backend/tests/test_topic_switch_policy.py` | 明确切换直接执行、推测切换只发事件、确认/拒绝、HTTP 层确认与拒绝 | 通过 |
| `backend/tests/test_anchor_no_advance.py` | 失败/取消不推进 Anchor、不吞掉待确认切换 | 通过 |
| `backend/tests/test_api_auth.py` | 新接口仍要求会话令牌 | 通过 |
| `frontend/src/planet/__tests__/browseSession.test.ts` | 同屏上限、单槽位替换、反向还原、同向旋转不原地打转、选中锁定、最小寿命、冷却、预取、搜索注入 | 通过 |
| `frontend/src/planet/__tests__/layoutSlots.test.ts` | 同 seed 稳定、不同 seed 有差异、不重叠（12 与 16 两档）、不集中极区、背面排序 | 通过 |
| `frontend/src/planet/__tests__/dotPool.test.ts` | 对象池数量固定、复用不 dispose、离开窗口只隐藏、单槽位替换、朝向正确 | 通过 |
| `frontend/src/planet/__tests__/browseFlow.test.ts` | 拖动量不足不动、够一步动一格、甩动不连跳、程序性移动不留残量 | 通过 |
| `frontend/src/views/__tests__/PlanetView.test.ts` | 原有相机/面板/竞态用例 + 选中不改 Anchor、进入话题才改、搜索命中注入窗口、选中锁定 | 通过 |
| `frontend/src/components/__tests__/TopicSwitchPrompt.test.ts` | 文案、两个动作、忙碌禁用、不是模态 | 通过 |
| `frontend/src/stores/__tests__/topicSwitch.test.ts` | 建议不改 Anchor、确认才切、拒绝保持、失败如实报错、跨轮不堆积 | 通过 |
| 全量回归 | 后端 `uv run --frozen pytest` 全绿；前端 `npx vue-tsc --noEmit` + `npm test`（45 个文件 / 406 项）全绿 | 通过 |

## 8. 视觉验证

真实运行的应用（后端 8734 + Vite 5199，隔离数据目录，123 个话题：其中 100 个是
`scripts/baseline/seed_phase2_topics.py` 写入的验收话题），用
`scripts/baseline/planet-phase2-probe.mjs`（Playwright + headless Edge / SwiftShader）驱动。

| 场景 | 判断 | 结果 |
| --- | --- | --- |
| 1 星球初始打开 | 是否保持足够留白 | 运行。球体完整落在画面内、四边有留白；同屏 16 个槽位（正面可见约 8 个），打开耗时约 0.83–0.94 s。截图 `%TEMP%\qio-baseline\phase2\shots\planet-initial.png` |
| 2 持续滚动/拖动 | 是否不断出现新 Topic | 运行。14 次拖动累计遇到 **31–33 个不同话题**（初始窗口 16 个），不是转同一批点 |
| 3 多周期旋转 | 是否仍有探索感 | 部分运行。同一会话内累计遇到 33 个话题、跨圈后顺序重排；未做「长时间连续旋转」的疲劳观察 |
| 4 Topic 进入/离开 | 是否连续自然、看不到数据替换 | 部分运行。替换只发生在背面槽位（代码约束 + 结构测试），淡入 420ms；**没有逐帧录屏**，肉眼只看了静帧 |
| 5 快速拖动后松手 | 惯性是否自然、无回弹 | 未运行（没有做惯性曲线测量；拖动沿用 OrbitControls damping，无弹簧回弹） |
| 6 悬停/选择 Topic | 信息是否足够但克制 | 运行。悬停只显示标题、选中有一个聚焦环 + 标题，无数据可视化叠加。截图 `planet-panel.png` |
| 7 100 Topic 数据集 | 星球是否仍然简单 | 运行。123 个话题下同屏仍是 16 个点、侧栏列表 123 条可滚动，画面没有变成点云。截图 `planet-mid.png` / `planet-panel.png` |
| 控制台 | 过程中有没有报错 | 运行。无控制台错误 |

截图目录：`%TEMP%\qio-baseline\phase2\shots\`；机读报告：`%TEMP%\qio-baseline\phase2\report.json`。

## 9. 性能

| 指标 | 实测 |
| --- | --- |
| Topic 数量 | 123（其中 100 个为本次写入的验收话题） |
| Fragment / Message 数量 | 每个话题 1–3 个片段、每片段 2 条消息（约 250 个片段 / 500 条消息） |
| 星球初始加载 | 只读概览聚合 + 一批 16 个话题，不读任何 Message 原文；打开到可用约 0.83–0.94 s（含懒加载 three.js chunk） |
| 当前渲染节点数量 | 固定 16 个话题点 mesh（对象池），标签 DOM 恒为 2 个（悬停 + 选中）；页内调试读取显示 `visible = 16 / capacity = 16` |
| 持续浏览后的对象数量 | swaps 持续增加时 mesh 数量不变（对象池复用），队列与历史池有上限（容量 × 4） |
| 是否发现对象泄漏 | 未发现：本轮没有新增每帧创建对象；被回收的话题只是改数据 + 隐藏。**未做长时间内存曲线采样**（见剩余问题） |

## 10. 剩余问题

- **触控板差异未测**：拖动/滚轮的实现沿用既有 OrbitControls 路径，只在鼠标拖动下实测；macOS 触控板双指滚动、横向滚动的手感与步长没有验证。
- **惯性既没有测量也没有调参**：本次只保证「不弹性、不回弹」，没有采样松手后的角速度衰减曲线。
- **过渡连续性没有逐帧验证**：话题进入/离开的淡入淡出只有静帧证据与结构约束，没有逐帧录屏确认「完全看不到替换」。
- **浏览排序仍需调参**：当前是「稳定哈希 + 小幅近期活跃加权 + 曝光抑制」，没有评估过「用户是否觉得顺序有意义」；如果将来要调，应先在 `services/params.py` 里集中阈值。
- **跨圈时前端会重建浏览队列**：`pass_changed` 后新一圈顺序会变，队列里没展示过的旧项会被丢弃，属于有意行为（避免机械循环），但没有针对「跨圈瞬间」做视觉观察。
- **Tauri 打包环境未运行**：只在 Vite dev + 无头浏览器里验证过，没有在打包产物里跑过星球。
- **真机 GPU 未验证**：无头环境使用 SwiftShader 软件渲染，帧率不能代表真实设备；星球仍是最重的场景（填充率与着色器成本），帧率结论待真机复测。
- **`__qioPlanetWindow` 调试钩子**：只在 `import.meta.env.DEV` 下注册，生产构建里不存在；它只读，不写任何状态。
