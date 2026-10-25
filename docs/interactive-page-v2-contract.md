# 互动页 v2 — Lead 集成契约（Phase 1 冻结）

> 基线：origin/feat/conversation-page-v2 = 13ec09357cd1477348635096a3999eb2e4ef8298
> 集成分支：feat/interactive-page-v2，工作区 D:\\qio-dev\\qio-interactive-v2
> 本文件由 Lead 维护；子智能体只读，不直接修改本文件。

## 1. 阶段与并行边界

### Phase 1（当前）
- A 公共全局栏与双向模式切换：仅 WindowChrome.vue、ConversationModeSwitch.vue 及对应测试。
- B 互动页 v2 布局外壳：只新增 frontend/src/components/interactive/v2/ 下的组件/纯函数/测试；不改 InteractiveView、BoardCanvas、ChatDock、BoardToolbar、stores、services。
- C 数据/旧入口/接口审计：只写 docs/interactive-v2/phase1-data-audit.md。
- D 对话/审批/App 门控复用审计：只写 docs/interactive-v2/phase1-chat-approval-audit.md。
- Lead：App.vue、InteractiveView.vue、store、types、router、tokens、最终集成。

### Phase 2/3/4
后续在 Phase 1 集成 SHA 上再冻结；共享文件仍由 Lead 独占。

## 2. Phase 1 冻结接口

### 2.1 共享全局栏
- 对话页与互动页共用同一条圆角栏、品牌、设置、窗口控制。
- 标题栏占位（--titlebar-height）二者均为 70px；栏本体 52px。
- 互动页搜索/保存状态通过 #qio-interactive-chrome-extra 挂载点接入：
  - WindowChrome 在互动路由渲染该容器，位于品牌与设置之间；
  - pointer-events: auto，不属于拖动面；拖动面仍是其兄弟；
  - 搜索结果浮层不得被全局栏 overflow 裁切；优先 Teleport 到 body 或使用 fixed 浮层；
  - 对话页不渲染该容器，现有布局不回退。
- 互动页不得再渲染自己的第二条窗口标题栏。

### 2.2 模式切换
- 同一组件显示当前路由模式；两个菜单项都可导航。
- 当前项 aria-current/当前标记正确；按钮 aria-label 随当前模式变化。
- 切换页面仅导航，不清草稿、附件、审批、排队和板面状态，不重复提交。

### 2.3 布局意图（B 交付，Lead 集成）
- 板面占主区域；底部工具栏与输入区明确分离，工具栏在输入区上方且内容宽度优先。
- 对话区从输入区向上展开；书签栏桌面为右侧窄栏，窄窗口为受限高度下方区域；不覆盖板面。
- 所有颜色只用 var(--*)；复用 tokens、字体、按钮高度、焦点、减少动画。
- 不把演示状态选择器、模拟失败等 HTML 演示元素带入生产。

## 3. 共享文件唯一归属（Phase 1）
- WindowChrome.vue：A
- ConversationModeSwitch.vue：A
- App.vue / InteractiveView.vue / router.ts / tokens.css：Lead
- stores/interactive.ts / interactive/types.ts / interactive/board.ts / services/interactive.ts：Lead
- BoardCanvas.vue / ChatDock.vue / BoardToolbar.vue：Phase 2/3 由 Lead 或明确转移后修改
- 新增 interactive/v2/**：B
- docs/interactive-v2/**：C/D 各自文件

## 4. 已知硬约束
- 图片是 file 卡的附件，不新增 image kind，不新建第二套附件读取。
- 参与讨论是独立状态；选中/多选/框选不改变参与。
- 确认接口成功 ≠ 主循环已执行；恢复接口成功 ≠ 已续作。
- 组端点连线需要前后端同步；不能只画前端线。
- 模型固定文案只能在 backend/src/agent/prompts.py（Phase 3）。
- 不 push、不建 PR、不触发完整 CI、不合并其他分支。


## 5. Phase 2 冻结字段（Lead 决策）

### 5.1 参与讨论（唯一状态）
- 每张卡新增 participation: "included" | "excluded"；它是参与范围的唯一事实源。
- 选中/多选/框选不改变 participation；提交后不清除。
- 新文件/文字卡默认 included；QIO 结果卡默认 excluded（与旧 reply 语义一致），用户显式打开后 included。
- 组没有独立 participation 字段：由成员派生 all/none/partial；组控件批量改成员。
- checked/hidden 保留为历史兼容字段，迁移后不再决定可见性；迁移一次性：
  - hidden=true -> excluded
  - text/reply -> 保留旧 checked 意图
  - file -> included（旧规则默认可见，即使 checked=false）
  - 旧 code/url -> excluded（仍不可交付）
- 前端范围预览、后端 visible_range、selectable_cards、before/after、删除/撤回、draft 提升、board_inspect/read、工具授权必须共用同一规则。迁移标记放板面状态 participationVersion=1，不新增用户可见历史页。

### 5.2 图片比例
- 图片仍是 file 卡附件，不新增 kind。
- meta.display = { width, height, aspectRatio } 记录浏览器解码后的真实显示像素和比例；不参与材料语义版本。
- 卡片 w/h 必须满足该比例；拖动/键盘/组缩放/工具尺寸/恢复统一走比例约束。
- meta.display 未知时不写 0/NaN/猜测比例；渲染加载/失败状态，失败不破布局。
- 详细状态不保留普通标题栏高度；图片铺满卡片，圆角裁切，不用 cover 伪装，不拉伸。

### 5.3 连线端点
- 端点字符串继续使用 src/dst：卡片仍为裸 card id；组写 group:<groupId>。
- 新增纯函数解析/校验/归一化；旧数据裸 card id 不变，不需要全量迁移。
- 组端点必须校验组存在/未删/获准成员；模型可见范围不得通过组名/成员/几何/连线泄露未参与内容。
- 组收起时成员对外连线可视觉汇聚到组边缘，但真实端点身份不变；解散组删除以组本身为端点的线，成员卡片线保留。

### 5.4 版本与语义
- 卡片/组/连线如带 version 必须在前端归一化中保留；后端权威自增。
- 纯布局（移动/缩放/书签/阅读位置）不改变材料语义版本，不使意图依据失效。
- 语义变化（正文、附件版本、参与、关系含义、组成员）进入材料版本/意图依据核对（Phase 3）。

### 5.5 Phase 2 文件唯一归属
- A 纯几何/避让：interactive/v2/geometry.ts（新增）及测试；不改 BoardCanvas。
- B 组/连线组件：BoardGroupFrame.vue、BoardLinkLayer.vue、interactive/linkEndpoints.ts（新增）及测试；不改 BoardCanvas。
- C 卡片/图片：BoardCard.vue、CardInlinePreview.vue、interactive/renderers/** 及测试；不改 BoardCanvas。
- D 后端板面：backend/src/agent/interactive/{models,board,board_tools,tool_models}.py 与定向测试；迁移只能追加。
- Lead：BoardCanvas.vue、stores/interactive.ts、types.ts、board.ts、services/interactive.ts、InteractiveView.vue 的最终接线。


## 6. Phase 3 冻结字段（Lead 决策）

### 6.1 新链意图视图
- IntentView 追加 display?: { x:number; y:number }（稳定 intent_id 绑定的显示位置；与目标/授权版本分开）。
- 后端持久化到 board_intents 新列 display（JSON；只追加迁移）；旧数据缺省 null。
- 意图状态只用新链：proposed / awaiting_confirmation / approved / running / paused / completed / partially_completed / failed / cancelled / rejected；legacy 只读、不冒充新状态。
- 意图确认前是虚拟层，不执行真实板面改动；位置只影响预览/成果优先落点。
- 确认/批量确认必须显式 ids；服务端依赖/冲突/授权检查；部分失败分别反馈。

### 6.2 确认后真实续作
- confirm / batch-confirm 成功后，必须由服务端为主循环创建/恢复互动 Turn：
  - board / submission / intent ids / authorization version / attempt / operation key / topic 绑定由程序提供；
  - 不把“确认成功”只写成 approved 后停下；
  - 主循环/受理不可用时，不得假装已开始；响应必须带真实 turn_id 或明确失败。
- recover 同理：恢复成功必须触发续作，而不是只改状态。
- 同一意图同一时刻只有一个有效执行；重复确认/旧回调不能并行。
- 返回结构追加 turnId?: string、resume?: boolean；保持旧字段兼容。

### 6.3 自动/手动重试
- 新增持久化 retry 系列（迁移追加）；字段至少 series_id、intent_id、authorization_version、attempts、limit=3、status、last_error、next_action、updated_at。
- 首次失败不算自动重试次数；最多三次自动重试；耗尽停止并只呈现一次最终失败说明。
- 后续手动重试一次点击一次尝试；不重新赠送三次；重启/重连不重置。
- 自动重试由服务端调度，不用前端 setTimeout。

### 6.4 对话/审批
- 互动底聊复用现有 MessageStream/Composer/ComposerEntries/会话 store；不新建简化模型；Composer 需接受互动绑定（board/submission/intent），不透传模型自报权限。
- 输入区上方事项行最多展开一个普通事项；审批入口唯一；UnifiedApprovalLayer 唯一挂载；旧 ApprovalEntry/Modal 在聊天路由让位。
- 发送/停止/排队/历史继续/话题建议/附件/失败恢复保留；板面提交与聊天发送分离。

### 6.5 模型固定文案
- 只修改 backend/src/agent/prompts.py 的 INTERACTIVE_POLICY、INTERACTIVE_RETRY_POLICY、TOOL_DESCRIPTIONS 指定键；不得在其他文件复制正文。
- 本轮实际新增/修改固定文案必须在最终报告全文列出。

### 6.6 Phase 3 文件唯一归属
- A 意图虚拟层/位置/确认 UI：frontend/src/components/interactive/Intent*.vue、v2/IntentLayer.vue（新增）及测试；不改 store/board.ts/InteractiveView。
- B 底聊/输入区：v2/InteractiveConversation.vue（新增）、ChatDock 相关组件；不改 InteractiveView/store。
- C 审批/恢复 UI：approval/*、ComposerEntries 相关；不改 App.vue/WindowChrome。
- D 后端续作/重试/提示词：backend/src/agent/interactive/intent_service.py、submission.py、services/turn_orchestrator.py、backend/src/agent/prompts.py、新迁移/测试。
- Lead：App.vue、InteractiveView.vue、stores/interactive.ts、platform 接线和最终集成。

