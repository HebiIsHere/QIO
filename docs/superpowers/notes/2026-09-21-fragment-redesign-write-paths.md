# 写入入口清单（阶段 1 的交付物 1.6）

日期：2026-09-21　基线：`a664900` 之后的阶段 1–4 改动

这份清单回答一个问题：**有哪些地方会往记忆域写东西，它们各自怎么保证「归属」是对的。**
清单用 `rg -n "append_message\(|seal_fragment\(|create_child\(|mark_continuation\(" src` 的结果逐条核对过，
不是凭印象列的。

## 一、写消息的入口（全部必须带 `turn_id`）

| 入口 | 文件:行 | 角色 | 归属怎么确定 | 备注 |
| --- | --- | --- | --- | --- |
| 用户消息 | `services/turn_orchestrator.py:285` | user | `turn_id=ctx.turn_id` → 按**轮前绑定**写入 | 写入前该轮绑定已落库（`begin()`），所以后续导航变化不会把这条消息搬走 |
| 助手回答 | `services/turn_orchestrator.py:445` | assistant | 同上，且话题取 `ctx.bound_topic` | 不再读「此刻的 Anchor」（旧实现在这里搬动用户消息） |
| 系统通知轮的反馈 | `services/app.py:974` | assistant | `turn_id=ctx.turn_id`，且该轮绑定标记 `system=1` | 系统轮不占用户轮次容量；不单独触发语义切分 |

判定标准：**写入必须显式传 `turn_id`**。`MemoryWriter._resolve_fragment` 的优先级是
「轮次绑定 > 调用方显式传入的 fragment_id > 该话题当前开放片段」；绑定指向的片段若已封存，
直接抛 `BindingMismatch`（宁可失败，也不「就近写进此刻开放的那一个」）。

没有绑定的写入（`turn_id=None`）只允许出现在测试与迁移脚本里 —— 生产路径上目前没有这种调用。

## 二、改变片段结构的入口

| 入口 | 文件:行 | 作用 | 事务性 |
| --- | --- | --- | --- |
| 容量分段封存 | `memory_lifecycle.py:85`（`seal_fragment`） | 固定封存时间 / 边界原因 / 内容版本；登记摘要派生任务 | 与「派发派生任务」「登记延续信息」同一个事务 |
| 容量延续登记 | `memory_lifecycle.py:122` → `fragment.py:181` | 记下「下一段接这一段、同阶段」 | 同上事务 |
| 新片段创建（懒） | `fragment.py:110`（`get_or_create_open`） | 真有消息要写时才创建；消费上面的延续信息 | 单条 INSERT |
| 历史接续的原子交接 | `navigation.py:260` | 封存同话题的开放片段 → 建接续片段（来源=所选历史） | 是一个事务：不会出现「片段建了但意图没落实」 |
| 阶段变化的交接 | `turn_orchestrator.py:541-546` | 封存 → 建子片段（`stage_change`、`same_stage=0`） | 两步都在轮前完成，随后才写绑定 |

规则：**来源只能来自实际路径**。`create_child` 会校验来源存在、同话题、不成环；
`get_or_create_open` 只在有「待延续信息」时才写 `source_fragment_id`，否则新片段**没有来源**
（不按「时间上最近的那一段」猜）。

## 三、只读行为（明确不写）

- 浏览话题、展开历史、查看原文：`planet`/`graph` 相关接口只读；
- 检索记忆（`memory_search`、星球面板搜索）：只读，不改 Anchor、不建片段；
- 话题预测与「推测切换」：只写 `pending`（`cursor` 表），用户确认后才走导航；
- 接续登记的取消：只改 `continuation_intents.state`，不留任何片段。

## 四、导航事件与旧轮次收尾的覆盖规则

1. 轮次完成后推进位置（`advance_anchor`）时，若用户在本轮绑定之后又做了**更新的**接续选择
   （`version` 更大），本轮不推进 —— 旧轮次不得覆盖新选择；
2. 本轮执行期间发生了工具级导航（切话题 / 建话题）时，位置归新导航，本轮不再把自己的片段
   推进过去（`anchor_kept` 会记进 trace）；
3. 失败 / 取消 / 没写出消息的轮次不推进位置，接续选择留给下一次重试；
4. 首轮成功推进后消费接续提示（`intent.state=consumed`），来源关系永久保留。

## 五、已知的边界

- 队列未持久化：进程重启后排队中的消息不会自动恢复（阶段 2 明确保留了这一限制）；
- `source_fragment_id` 为空的旧片段一律标 `relation_type='unknown'`（迁移 16），
  它们的上下文退化成「同话题背景」，不享受路径隔离；
- 「改挂来源」目前没有产品入口，`would_cycle` 只为这种未来操作准备了判断。
