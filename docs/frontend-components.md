# 前端组件级设计

版本：v1（2026-08-03，M11 已实现；实现偏差见下）

> 实现偏差记录：
> - 悬浮球 v1 为 SVG 图标按钮（微缩星球 3D 远景渲染留 v1.5）；
> - 记忆/知识面板在星球页详情已实现，对话页内嵌面板留后续；
> - 斜杠命令体系 v1 未实现（工具创建入口为设置页凭据 + 对话输入区，斜杠命令留 v1.5）。
前置：docs/frontend-requirements.md（硬要求）、docs/frontend-design.md（星球架构）

## 1. 对话页组件树

```
ConversationView（对话页）
├── PlanetDock            悬浮球（微缩星球 + 位置指示 + 展开入口）
├── StatusBar             状态条
│   ├── ConnectionDot     SSE 连接状态
│   ├── CapabilityBadge   模型三态（native/text/unsupported）
│   ├── FallbackBanner    降级横幅（FALLBACK 事件驱动）
│   └── CredentialAlert   凭据异常提示（点击跳设置页）
├── MessageStream         消息流（虚拟滚动）
│   ├── TurnMarker        轮次分隔（TURN_START/END）
│   ├── MessageItem       消息项
│   │   ├── TextContent   Markdown 渲染（用户/助手）
│   │   ├── ToolCard      工具调用折叠卡片（TOOL_START/END 聚合，可展开）
│   │   └── InjectionTag  记忆注入标记（MEMORY_INJECT 来源：知识/记忆）
│   └── EmptyState        空状态（引导）
├── Composer              输入区
│   ├── TopicIndicator    当前话题名 + 锚点片段
│   ├── TextInput         文本输入
│   └── MemorySlider      记忆强度滑块（0-1，注入预算）
└── ModalLayer            全局模态层
    ├── ApprovalModal     审批弹窗（三类：tool_create/credential_grant/high_impact_knowledge）
    ├── ToolCreateWizard  工具创建向导（4 步，见 §5）
    └── KnowledgeConfirm  高影响知识确认（可复用 ApprovalModal）
```

### 1.1 关键组件职责

- **PlanetDock**：渲染微缩星球（远景相机视图）；当前话题位置高亮；点击 → 相机推进全屏；可拖动、贴边收纳。低帧率模式（静态帧 + 交互动画）
- **StatusBar**：只读状态呈现，不承载操作（凭据异常提示除外——它是跳转入口）
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
TopicDetail 选中片段（或整话题）→ StartHereButton → session store 更新 anchor_fragment_id → 相机收起 → 对话页重新注入（MEMORY_INJECT 偏置该片段）→ 用户继续对话

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