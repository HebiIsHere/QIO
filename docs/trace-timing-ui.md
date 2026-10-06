# 耗时面板（Trace 可解释性 · 前端展示）

> 2026-10-02 · 第二阶段 Agent C。后端 `GET /api/traces/{turn_id}` 已经返回 `phases`
> 账本（阶段名 + 毫秒 + notes），这份文档说清前端怎么把它变成用户看得懂的一句话：
> **「这次为什么等这么久」**。

## 1. 为什么要做

真实事故：一轮 `duration_ms=51498`，其中模型调用只有 1688ms —— 49.8 秒在界面上
没有任何解释。后端的第一阶段已经把时间按阶段铺满（`sum(顶层阶段) + residual = duration`），
但**前端没有任何消费者**：用户看不到，可观测性就没有闭环。

## 2. 展示什么：内部阶段 → 用户分类

用户看到的是一张**分类表**，不是内部阶段名。映射表在
`frontend/src/services/trace.ts` 的 `STAGE_CATEGORY`：

| 用户看到 | 由哪些内部阶段合成 |
| --- | --- |
| 排队 | `phases.notes.queue_wait_ms`（受理之后、真正开始之前） |
| 准备环境 | `adapter_setup`（凭据/能力探测） |
| 上下文准备 | `turn_setup`、`turn_binding`、`context_assembly`（独占部分）、`topic_prediction` |
| 记忆检索 | `retrieval`（记忆 / 知识 / 实体卡） |
| 模型 | `model_wait`（含收尾压缩整理的那次模型调用） |
| 工具 | `tool_wait`、`tool_routing` |
| 等待确认 | `approval_wait`（预算 / 无进展 / 护栏三条审批通道） |
| 保存 | `persistence`、`finalize` |
| 记忆整理 | `memory_post` |
| 其它 | `other`、`narrative`、`agent_loop` 的独占部分、`residual_ms`、**任何没被映射表认识的阶段** |

三条硬规则：

1. **未知阶段不会消失**：先进「其它」，同时把原名记进 `unknownStages`，开发者模式下
   显示（例如「未归类：brand_new_stage」）。后端将来加阶段不会被静默吞掉。
2. **residual 如实显示成「其它」**：不声称 QIO 能解释所有耗时。
3. **排队单独一行**：它是用户等待的一部分，但不属于「执行时长」；面板只在
   `notes.queue_wait_ms` 存在时才给出这一行。

## 3. 怎么算才不重复计

后端的 span 是「顶层阶段铺满时间轴 + 嵌套只作细分」。前端按 `start_ms`/`depth`
重建父子关系，然后算**独占时间**（父 − 子），再把每个 span 的独占时间归到分类：

```
agent_loop 1221ms
├─ approval_wait 881ms   → 等待确认
├─ model_wait     240ms  → 模型
└─ …（其余归工具/其它）

```

于是**父子不会重复计入**，并行工具也只算批次墙钟一次（后端只记一条 `tool_wait`）。
占比的分母是「排队 + 执行时长」，与用户实际等待一致。见
`frontend/src/services/trace.ts` 的 `attributeSpans()`，单测在
`frontend/src/services/__tests__/turnTiming.test.ts`。

## 4. 降级：不显示 0，也不留空白

| 情况 | 界面 |
| --- | --- |
| 未展开（促销总耗时已由 TURN_END 给出） | 「▸ 已完成 · 耗时 1.5 秒」——**不展开也能看见**，不显示「读取中」 |
| 正常（有 phases） | 分类表 + 占比条 + 数值 |
| 老数据（没有 phases 列） | 「耗时 · 8.4 秒」+「总耗时 8.4 秒，但这次没有分阶段记录（旧版本留下的数据）」 |
| 连 duration 都没有 | 「这次没有留下耗时记录」 |
| 接口失败 | 「这次没读到耗时明细（不影响回答本身）」，**已知总耗时继续显示**，并提供重试 |
| 消息没有 turn_id / 还在流式生成 | 整个面板不出现 |

入口**每轮只有一个**（并入统一过程区域 `TurnProcess.vue`，见 `docs/architecture.md` §12.1.2 /
§12.1.4），不再按助手消息各挂一个。折叠态的总耗时来自 `TURN_END.duration_ms`（后端权威，
缺失时退化为单调钟执行窗口），**不需要**为了看见总耗时去展开；只有用户真的展开时才去拉 trace
明细（会话内缓存，同一轮不重复请求），因此「读取中」只在**确实在请求明细**时出现，
未请求、加载中、成功但无分项、失败、旧记录缺字段是五种互不混淆的显示。

## 5. 无障碍

- `<details>/<summary>` 原生展开语义，键盘可用。
- summary 上带 `aria-label`：**「总耗时 1.8 秒，其中等待确认 881 毫秒、模型 240 毫秒、…」**
  —— 读屏听到的是完整一句话，不是一串碎片。
- 时间一律带单位与可读小数：`920 毫秒` / `1.7 秒` / `1 分 5 秒`（等宽字体）。
- 占比条只是视觉辅助（`aria-hidden`），数值才是权威。
- 颜色全部走 `var(--*)` 令牌，没有硬编码色值。

## 6. 性能与上限（真实数字）

| 场景 | 实测 |
| --- | --- |
| 501 个 span 的一轮，展开渲染（jsdom） | **2.7–2.9 ms** |
| 5001 个 span 的归属计算 | **2.1 ms** |
| 渲染行数 | 只与分类数有关（≤10），与 span 数无关 |

上限策略：DOM 行数是常数级（分类固定），归属计算是 O(n log n) 的排序 + 一次栈扫描；
超长 trace 不会造成渲染膨胀。单测里同时断言了耗时上界，超过就会红。

## 7. 隐私：展示路径只吃 phases

真读了一条真实 trace（`D:\QIO-data\app.db`，只读）逐字段检查：

- `injection.items[].preview`：**用户原文 / 记忆预览**（这是产品设计：用户自己的数据、本地存储）；
- `final_preview`：助手回答原文（截断到 500 字）；
- `tool_runs[].args_preview`：工具参数，整库扫描里有 **Windows 路径**（文件工具）；
- 整库扫描（7 条 trace / 34 条 tool_calls / 34 条 tool_records）：**没有密钥形态命中**。

而面板**只消费 `phases`**，并且实测（故意把路径与假密钥塞进用户消息、工具参数与错误）：

```
phases 含 WINPATH: False
phases 含 SECRET 形态: False
phases 含用户原文片段: False
阶段名: adapter_setup / agent_loop / context_assembly / …
detail 取值: None / call#1 / call#2 / calls=1 / user_message
对照：同一行 final_preview 含 WINPATH = True
```

前端另有一条测试保证：**即使接口响应里带着路径与密钥，面板也不会把它们渲染出来**
（`TurnTimingPanel.test.ts` 的「展示路径不碰 trace 里的敏感/隐私字段」）。
本任务没有新增任何后端出口（没有新事件、没有新接口），写入路径仍由
`agent/trace/redact.py` 把关。

## 8. 真实视觉检查（可复现）

```powershell
cd frontend
$env:TEMP="<worktree>\.tmp"; $env:TMP=$env:TEMP
npx vite build --config vite.visual.config.ts      # 演示页（假数据，不联网、不碰后端）
npx vite preview --config vite.visual.config.ts    # http://127.0.0.1:5299/timing-demo.html
node visual/probe.mjs                              # Edge 截图 + 控制台错误检查
```

产物：`frontend/.visual-out/shots/timing-1280.png`、`timing-900.png`（1280/900 两档宽度，
0 横向溢出、0 控制台错误）。演示页覆盖四种状态：正常一轮、长一轮（65 秒）、
开发者模式（含未归类阶段）、老数据（没有 phases）。

C2 的两条隐私检查脚本也在这里（只读，不打印原文）：

```powershell
# 真实开发库逐列扫描（只读，模式 path / 密钥形态；D:\QIO-data\app.db 是 2026-09-29 的真实数据）
cd backend; uv run --frozen python ../frontend/visual/scan/scan_db_sensitive.py
# phases 子文档的敌意内容检查（构造带路径与假密钥的一轮，看账本里有没有）
$env:QIO_DATA_DIR=''; uv run --frozen python ../frontend/visual/scan/scan_phases_privacy.py
```

## 9. 已知限制（诚实）

- 面板展示的是**阶段账本**，不是逐次工具调用的明细（那是工具卡片的职责）。
- `排队` 只有新格式 trace（有 `notes.queue_wait_ms`）才有；老数据不会显示这一行。
- 历史消息只有在带上 `turn_id` 时才有入口；没有 `turn_id` 的早期消息不显示面板。
- 折叠态总耗时来自 `TURN_END`（权威台账 `duration_ms`，缺失时退化为进程内单调钟的执行窗口）；
  老记录两者都没有时显示「这次没有留下耗时记录」，**不伪造 0**。
- 并行分项不求和冒充总耗时（实测三个并行工具之和可达批次墙钟的约三倍）；整轮结束后的后台整理
  单独成句，不计入上面的耗时。
- 供应商是否真的停止生成/计费，QIO 无法解释，也不在本面板的语义里。
