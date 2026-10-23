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