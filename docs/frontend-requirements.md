# 前端硬要求（设计约束基准）

版本：v1（2026-08-02，用户确认）
状态：设计期基准文档；后端实现以 docs/architecture.md 为准

本文档定义前端设计的不可妥协约束。前端设计只能在边界内发挥；
E 节为自由区，不受约束。

## A. 协议与接口（不可改）

1. 通信只走 localhost HTTP + SSE；14 类事件：
   CAPABILITY / FALLBACK / MEMORY_INJECT / APPROVAL_REQUIRED /
   APPROVAL_RESULT / CREDENTIAL_STATUS / SUBAGENT_STATUS / USAGE /
   TURN_START / TURN_END / TOOL_START / TOOL_END / WARNING / ERROR
2. 事件信封固定：{ "type": str, "id": str, "ts": str, "data": object }
3. 前端必须消费的后端端点：
   - GET /api/events（SSE 流）
   - POST /api/approvals/{id}/respond（审批响应）
   - 凭据 CRUD 与测试连接（M2 设计，实现中）
   - 对话与注入端点（M9 设计接入，实现中）
4. 前端是薄壳：不直接读写 SQLite、不直接访问系统凭据库；
   一切数据经后端 API

## B. 凭据与安全（不可妥协）

5. 密钥只写不读：输入表单可填，展示永远掩码，不允许"显示明文"入口
6. 密钥不参与任何导出/导入
7. 凭据类别标签：main-loop / subagent / vision / video / audio /
   research / embedding + 自定义；授权范围（scope）需可展示；
   子 agent 的 Key 可改可重填
8. 审批流必经（三类）：
   - 工具创建（tool_create）
   - 凭据授权（credential_grant）
   - 高影响知识确认（high_impact_knowledge）
   前端弹窗呈现（含解释、测试摘要），结果如实回传
   approved / rejected，不能绕过
9. 记忆强度滑块（0–1）是前端的注入预算控制项

## C. 运行环境（技术约束）

10. 目标机器可能无独立显卡：渲染走 WebView2（软件渲染兜底），
    避免强 GPU 依赖；网状图（d3-force）需考虑低配性能
11. 窗口最小 800×600，单窗口应用
12. SSE 断线重连与连接状态展示是前端职责
13. 长对话场景：消息流增长不能导致卡顿（虚拟滚动或等价方案）

## D. 交互流程（体验硬要求）

14. 网状话题导航：对话组织是话题图（用户根 / 实体 / 话题三类节点），
    不是并列会话列表
15. 话题切换防抖：pending → confirm / discard；切换后旧位置保留
16. 锚点必须有 UI 呈现：当前话题 + 片段位置是导航基准
17. 工具创建流程完整 UI 步骤：
    提案解释展示 → 测试结果展示（通过用例明细）→ 审批 → 注册结果；
    子 agent 型工具标注"执行能力 v1.5"
18. 记忆/知识面板可呈现：
    - 记忆三层：原文 / 摘要 / 目录
    - 知识状态机：draft → pending_review → verified → active →
      expired / revoked 状态标识
    - 高影响知识的用户确认入口
19. USAGE 事件可呈现：会话用量与凭据预算状态
20. 话题指纹（标题、关键词、最近活动）可见，作为跨会话导航载体

## E. 位置映射（2026-08-03 前端设计确认）

- 记忆/知识浏览（D18）、话题指纹（D20）：位于星球页右侧详情面板
- 高影响知识确认：对话页模态（对话中触发）与星球页入口并存
- 凭据管理（B7）：设置页凭据区
- 工具创建向导/审批（D17、B8）：对话页模态
- 记忆强度滑块（B9）：对话页输入区
- 星球页与对话页之间的切换：悬浮球 + 单一 3D 场景相机过渡（见 frontend-design.md）

## F. 设计自由区（不受上述约束）

- 视觉风格、配色、排版、组件库选型
- 图视图的具体形态（力导向图 / 树状 / 列表入口组合）
- 消息展示样式、Markdown 渲染方案
- 设置页组织方式、弹窗交互细节
- 动画、微交互、空状态设计

---

## 约束来源索引

- A：docs/architecture.md 4.2（事件协议）、4.3（存储）、4.4（凭据）
- B：docs/architecture.md 4.4（凭据体系）、4.13（工具生命周期）
- C：环境调研（无显卡机器、WebView2 软件渲染）
- D：docs/architecture.md 4.11（图导航）、4.9（记忆域）、4.10（知识域）、
  4.13（工具生命周期）、5.4（注入预算）