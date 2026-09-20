# 前端组件级设计

版本：v1（2026-08-03，M11 已实现；实现偏差见下）

> 实现偏差记录：
> - 悬浮球早期为 SVG 图标按钮；2026-09-15 第四阶段改为**全屏 Planet 的压缩态**
>   （`frontend/src/components/planet/PlanetOrb.vue`，与全屏星球共用同一套距离场数学与颜色令牌），
>   两者通过「同一个对象连续长大 / 收缩」的方式切换，不再是两个组件的淡入淡出；
> - 记忆/知识面板在星球页详情已实现，对话页内嵌面板留后续；
> - 斜杠命令体系 v1 未实现（工具创建入口为设置页凭据 + 对话输入区，斜杠命令留 v1.5）。
前置：docs/frontend-requirements.md（硬要求）、docs/frontend-design.md（星球架构）

## 1. 对话页组件树

```
ConversationView（对话页）
├── PlanetDock            悬浮球（微缩星球 + 位置指示 + 展开入口）
├── SettingsFloat         设置入口（右上角浮动 ⚙，点击打开整页设置）
├── ErrorHint             异常提示条（仅 lastError 时出现，可跳设置页）
├── MessageStream         消息流（虚拟滚动）
│   ├── TurnMarker        轮次分隔（TURN_START/END）
│   ├── MessageItem       消息项
│   │   ├── TextContent   Markdown 渲染（用户/助手）
│   │   ├── ToolCard      工具调用折叠卡片（TOOL_START/END 聚合，可展开）
│   │   └── InjectionTag  记忆注入标记（MEMORY_INJECT 来源：知识/记忆）
│   └── EmptyState        空状态（引导）
├── Composer              输入区
│   ├── TopicIndicator    当前话题名（+ 仅在历史位置时显示「从「XXX」继续」）
│   ├── TextInput         文本输入
│   └── MemorySlider      记忆强度滑块（0-1，注入预算）
└── ModalLayer            全局模态层
    ├── ApprovalModal     审批弹窗（三类：tool_create/credential_grant/high_impact_knowledge）
    ├── ToolCreateWizard  工具创建向导（4 步，见 §5）
    └── KnowledgeConfirm  高影响知识确认（可复用 ApprovalModal）
```

### 1.1 关键组件职责

- **PlanetDock**：渲染微缩星球（远景相机视图）；当前话题位置高亮；点击 → 相机推进全屏；可拖动、贴边收纳。低帧率模式（静态帧 + 交互动画）
- **SettingsFloat**：右上角浮动设置入口（⚙），点击打开整页设置页
- **ErrorHint**：对话页顶部异常提示条，仅在有 lastError 时出现（如凭据/后端异常），可跳设置页
- **ToolCard**：折叠态显示工具名 + 结果摘要（ok/error 色标）；展开显示参数、耗时、完整输出；WARNING 关联显示
- **InjectionTag**：消息流中显示"本次注入的知识条目/记忆片段"来源标记，可展开查看原文引用

## 2. 星球页组件树

```
PlanetView（全屏覆盖层，与对话页共享同一 3D 场景）
├── PlanetScene             WebGL 画布（唯一场景实例）
│   ├── PlanetBody          星球球体（半透明 + 线框）
│   ├── MarkerLayer         话题地点标记（点 + 标签 Sprite）
│   ├── ClusterVisual       地区聚类视觉（同组连线/着色，v1 连线）
│   └── Background          星空背景
├── PlanetControls          辅助控制（不依赖，探索手段）
│   ├── ResetView           相机复位
│   ├── ActiveFlip          活跃层/档案层翻转
│   └── ZoomHints           缩放提示
├── SidePanel               右侧面板
│   ├── TopicList           简明列表（查询通道）
│   │   ├── SearchInput     列表内搜索
│   │   └── TopicItem       名称 + 地区 + 活跃度
│   └── TopicDetail         选中话题详情
│       ├── TopicMeta       名称/关键词（可编辑）
│       ├── FragmentTimeline 片段列表（时间线，只读区）
│       ├── MessagePreview  片段消息预览（只读）
│       ├── EntityTags      实体标记（点击 → 跨话题提及搜索）
│       ├── KnowledgeList   知识子域条目（状态标识 + 高影响确认入口）
│       └── StartHereButton "从这里开始"（注意力偏置提交）
└── CloseButton             关闭 → 相机拉回悬浮球原位置
```

### 2.1 关键组件职责

- **PlanetScene 单例**：同一 WebGL 场景两种相机状态（远景/近景）；全屏时全帧率，对话页时低帧率
- **TopicList / TopicDetail 联动**：列表选中 → 相机自动旋转聚焦 + 详情面板加载；星球点击 → 列表高亮 + 详情加载
- **StartHereButton**：提交 topic_id + fragment_id（可为空 = 整个话题）→ 对话页注入参数更新（注意力偏置）→ 返回对话页
- **TopicMeta 编辑边界**：名称/关键词可编辑；片段/消息只读；记忆摘要修正走纠错反馈（不直接改写）

## 3. 设置页组件树

```
SettingsView
├── SettingsTabs（凭据 / 偏好）
├── CredentialsTab
│   ├── CredentialList     Key 卡片（掩码显示 + 状态色标 + 预算进度）
│   ├── CredentialForm     新建/编辑（密钥只写不读、标签多选、scope 编辑器、预算）
│   ├── TestConnection     测试连接按钮（调 /credentials/{id}/test）
│   └── AuditView          审计日志（版本历史）
└── PreferencesTab
    ├── ModelEndpoints     模型端点配置（base_url/model/适配档）
    ├── MemoryDefaults     记忆强度默认值、冷热阈值
    └── BehaviorToggles    行为开关（force_continue 等）
```

## 4. 跨页面共享层

- **Store 划分**：events / session（锚点、话题、消息）/ graph（节点、坐标、指纹）/ credentials / approvals（待审批队列）/ memory / knowledge / settings
- **3D 场景单例**：PlanetSceneManager（挂载于对话页容器；悬浮球与全屏共用，相机状态切换驱动）
- **审批队列**：approvals store 订阅 APPROVAL_REQUIRED 事件 → 弹窗队列（串行展示，避免堆叠）

## 5. 关键交互流程

### 5.1 对话流程
输入 → POST turn → TURN_START 事件 → 消息流逐条渲染（assistant 流式）→ TOOL_START/END → ToolCard 聚合 → MEMORY_INJECT → InjectionTag → TURN_END + USAGE → 用量更新

### 5.2 星球展开/收起
PlanetDock 点击 → 相机推进（650ms 缓动）→ PlanetView 接管交互 → 收起：CloseButton/双击空白 → 相机拉回原悬浮球位置与朝向 → 对话页浮现

### 5.3 "从这里开始"
TopicDetail 选中片段（或整话题）→ 面板显示「已选择历史位置：<片段摘要>」→ StartHereButton（「从这里继续」）
→ POST /api/anchor(topic_id, fragment_id) → 成功后 session store 记下 `anchorHistoric=true` → 相机收起
→ 对话页 TopicIndicator 显示「从「…」继续」，下一轮该片段进入 Focus。

语义（与 `docs/architecture.md` 的 Anchor 生命周期一致，不要只当成 UI 文案）：

- 「从这里继续」= 用户明确改变历史讨论位置，优先级高于自动恢复与检索；
- **成功一轮之后**后端把位置推进到当前片段并广播 `ANCHOR(historic=false)`，
  提示随之消失（不允许几十轮后仍显示同一个旧片段）；
- `memory_search` 只是只读检索，不会改动这里的显示；
- 只有 Agent 显式调用 `continue_from_fragment` 才会产生新的历史位置提示；
- 界面不暴露 `anchor_fragment_id` 这类内部术语，只说「历史位置 / 从这里继续 / 从「XXX」继续」。

### 5.4 工具创建向导（4 步）
1. 提案展示：解释 + 工具定义（名称/描述/参数）
2. 测试结果：确定性用例明细（通过/失败）
3. 审批：双段（创建审批 → 凭据授权审批，如引用凭据）
4. 结果：注册成功 → 对话流出现"新工具可用"提示；失败 → 展示失败步骤与原因

### 5.5 审批弹窗（三类共用）
APPROVAL_REQUIRED 到达 → 队列弹出 → 展示 payload（解释/测试摘要/授权对象）→ approved/rejected → POST respond → 结果反馈 → 下一个

## 6. 技术决策（已确认 2026-08-03）

1. Markdown 渲染：remark（unified）系——remark-gfm + remark-directive + 自定义 Vue 渲染器。
   理由：为内联结构化内容保留迭代路径（记忆引用/知识链接/内嵌组件）；AST 三层解耦可复用
   （导出/搜索/版本化）。代价：初始封装成本高 30-50%，包体积用 tree-shaking 控制。
2. 虚拟滚动：@tanstack/vue-virtual——原语级（渲染完全自控）+ 维护活跃。
   聊天流锚定与动态测量由应用层组装（measureElement + 底部跟随，约百行）。
   弃用 vue-virtual-scroller：维护放缓 + 封装形态限制扩展。
3. 悬浮球位置：贴边收纳为主（默认右下半隐）+ 四边可吸附拖动，位置持久化。
4. 搜索框：星球页列表顶部（话题级过滤）；跨域历史检索走对话（agent memory_search 工具），
   前端全局搜索 v1.5 视反馈再定。
5. 话题详情编辑：轻字段就地（名称点击编辑、失焦保存）；重字段弹窗（关键词列表、
   知识条目走状态机 revoke/更新）。
6. 工具创建入口：输入区图标按钮（发现性）+ 斜杠命令体系并存（/tool /topic /memory），
   命令带候选提示。

## 7. v3 组件原语（2026-09-15 第四阶段）

第四阶段的目标不是「再多做几个组件」，而是让**新功能不再自带一套视觉语法**。
下面这些原语定义在 `frontend/src/styles/base.css`，新增界面优先复用它们；
组件内的局部规则仍然生效（scoped 优先级更高），所以这是收敛而不是大爆炸重写。

| 原语 | 类 | 关键规则 |
| --- | --- | --- |
| Button | `.qio-btn`（`.primary` / `.danger` / `.danger-solid` / `.quiet` / `.success` / `.mini` / `.busy`） | default → hover → pressed → loading → success → failed → disabled 全部连续；disabled 不得看起来可点 |
| Card | `.qio-card`（`.qio-card--quiet` / `--focus`，状态用 `data-state="running\|waiting\|ready\|failed"`） | 状态推进**原位发生**：不换新卡、不堆卡、完成后内容不突然消失 |
| List row | `.qio-row`（`.is-active` / `.is-selected` / `[aria-selected]`） | hover 只加一档亮度；selected 必须与 hover 可区分；focus 可见 |
| Toolbar | `.qio-toolbar` | 分段标题 + 操作区，统一右对齐与间距 |
| Tag / State badge | `.qio-tag` / `.qio-state`（`.ok` / `.warn` / `.err` / `.info` / `.quiet`） | 全站唯一的状态底色来源，不再各写一套徽章变体 |
| Inline edit | `.qio-inline-edit` | 默认阅读态；编辑就地进入，退出即恢复 |
| Feedback | `.qio-feedback`（`.ok` / `.warn` / `.err` / `.info`） | 四级反馈的唯一渲染原语（字段 / 组件 / 任务 / 全局） |
| Floating / Glass | `.qio-floating` / `.qio-glass`（`--chip` / `--panel`） | 晶体玻璃只用于 Planet 周围浮层、轻量信息卡、关键确认层、少量局部悬浮 UI |
| Confirm | `.qio-confirm`（`--inline` / `--popover` / `--layer`）+ `.qio-confirm-scrim` | 取代全部浏览器原生 confirm/alert；按危险程度分档；高风险确认按钮不用品牌色 |
| 过渡 | `.qio-fade-*` / `.qio-rise-*` / `.qio-list-*` / `.qio-collapse-*` / `.qio-swap-*` | 供 `<Transition>` / `<TransitionGroup>` 共用；列表增删与重排不跳变，Tab/面板切换不瞬切 |

卡片家族的共同契约（Approval / Tool Creation / Subagent / Knowledge Candidate / 工具调用）：
**对象名（标题）→ 状态徽章 → 一行说明 → 可展开详情**，且状态变化只能体现在同一张卡上。

### 7.1 与早先记录的偏差更新

- 悬浮球不再是「SVG 图标按钮」：入口小球现在是**全屏 Planet 的压缩态**（`PlanetOrb`，与全屏星球共用
  `frontend/src/planet/sdfRings.ts` 的距离场数学），并与全屏星球构成进入/退出连续体。
- 记忆/知识面板仍在星球页详情与面板内，对话页只在回答完成后显示高影响知识候选卡。
