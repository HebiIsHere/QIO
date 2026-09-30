# 最终结论的事实校正（规格）

**日期：** 2026-09-30
**范围：** 后端：主循环收尾、工具结果接口、开发工作区工具、工具创建提交路径、新工具 `declare_completion`。前端不在本规格内。

## 背景（缺口与根因）

已复现的真实形状：工具失败的结果**已经**如实回填给模型（这一层由
`core/tool_feedback.py` 保证），模型随后仍然说「测试全部通过，已提交审批」，
而这句没有证据的话照样成为最终答复。

根因不是「失败没被反馈」，而是**没有任何一层记得本轮到底做了什么**：

- 没有一个地方把「任务 / 版本 / 验证 / 审批 / 注册」记成本轮的事实；
- 最终答复（`TurnResult.final_content`）不经过任何核对就直接落库、发给用户；
- 于是「模型说的」和「后端知道的」是两套互不相干的东西。

明确不采用的做法：**用关键词判断模型有没有在说「完成」再去改话**。那会误伤
正常表述（「如果测试通过就可以使用」），也修不了根因。

## 需求

1. **本轮事实台账。** 每一轮在内存里维护一份事实台账，来源只有两处：
   工具调用的最终结局（成功 / 失败 / 取消，后端本来就知道），以及开发类工具
   自己上报的任务状态。台账不落盘、不新增数据库迁移。
2. **结论声明协议。** 新增工具 `declare_completion(task_id, version, claims)`：
   模型要把「完成 / 可用」当作结论说出来之前，先用它把结论报给后端核对。
   `version` 用工作区内容摘要（`DevWorkspace.content_digest`），即「哪一版代码」。
3. **声明逐条核对。** 判定来源必须是后端已有的权威记录，不新增影子状态：

   | claim | 判定 | 缺证据时告诉模型什么 |
   | --- | --- | --- |
   | `test_passed` | `status["test_evidence_current"]` 为真且 `last_test_passed` 为真，且声明的 `version` 等于当前 `content_digest` | 「测试证据不存在 / 已失效（内容改过，需重跑）/ 这一版没有通过记录」 |
   | `registered` | 工作区 `submitted` 为真、`submitted_digest` 等于声明的 `version`、且注册表里还能拿到该工具 | 「未提交」/「声明的是旧版本」/「注册表里已经没有这个工具」 |
   | `usable` | `registered` 且（`function` 型工具还需要 `test_passed`；`subagent` 型工具不需要） | 把上面两条里没满足的那条原样说出来 |

   全部满足 → `ToolResult.ok = True`，正文写清依据（版本摘要前 12 位、测试摘要、
   注册情况），`facts` 里带上 `{"declaration": {"accepted": true, "basis": "…"}}`；
   有任一条不满足 → `ToolResult.ok = False`，正文逐条列出缺什么、怎么补。
   审批不单列成 claim：注册必须经过用户批准（见 `tools/lifecycle.py`），
   所以「已注册」本身就蕴含了「用户批过」。
4. **兜底注记。** 收尾时按以下规则决定要不要在最终答复末尾追加一段事实说明：

   - 本轮**没有**未解决的失败 → 什么都不加（正常对话完全无感）；
   - 本轮**有**未解决的失败，且本轮**没有**被接受的声明 → 追加；
   - 本轮有未解决的失败，但**有**被接受的声明 → 不追加（模型已经按账本说过结论了）。

   「未解决的失败」只有两种来源：

   - **工具调用**：同一个工具在本轮里的**最后一次**终态是 `failed`（后续重试成功就不算）；
   - **开发任务**（只算本轮碰过的）：测试失败、测试证据失效（曾经通过但内容已改），
     或本轮碰过却**还没有任何测试证据**。

   **取消不算失败**（用户自己按的，界面已有「已停止」状态行）。

   **已知代价（接受它，写在这里免得以后当成 bug）：** 一轮只做到一半就结束时
   （比如刚建好任务、还没写测试），末尾会带一行「还没有测试证据」。它说的是事实，
   而且很短；换来的是不会出现「没验证却被当成已完成」的轮次。真嫌吵的话，缩小
   这条的触发条件是后续可以单独决定的事，不影响协议本身。
5. **不重写模型正文。** 注记是后端加在末尾的事实说明，不修改、不删除模型写过的字。
6. **注记的硬约束。**
   - 先过 `agent/trace/redact.py::redact_text` 脱敏，再限长（默认 600 字符）；
   - 最多列 3 条失败，其余归并成「另有 N 项」；
   - 文案里不出现花括号 —— `services/maintenance.py` 用 `re.search(r"\{.*\}")`
     从 `final_content` 里取 JSON，注记不能干扰这类既有解析；
   - 后台维护轮天然不会加注记（它们用空 `ToolRegistry()`，调不了工具）。
7. **注记会进入历史。** 追加后的文本就是落库、展示、下一轮上下文里的 assistant
   消息（与现有「静默失败收口」补 `_stop_note` 的行为一致）。这是有意为之：
   事实应该留在记录里，而不是只在当轮闪一下。

## 非目标

- **不做前端「已核对」标记。** 要把核对结论渲染成用户可见的小标记，需要先决定
  「随 assistant 消息持久化」的方式（新增迁移还是复用现有列），再动界面；本规格
  只保证后端产出这份事实，见文末「后续」。
- 不做关键词 / 正则判断模型意图，不重写、不删除模型正文。
- 不新增数据库迁移，不改审批语义（单次使用、过期、摘要校验、拒绝 / 超时 / 取消）。
- 不覆盖子 agent 内部循环（子 agent 的 `AgentLoop` 有自己的轮次与事实）。
- 不把「用户没有验证过的普通闲聊结论」纳入核对 —— 只核对开发任务这类有后端记录的结论。

## 契约

```python
# agent/tools/base.py —— 给工具结果加一个机器可读的事实通道
@dataclass
class ToolResult:
    ok: bool
    content: str = ""
    error: str | None = None
    category: str | None = None
    recoverable: bool | None = None
    facts: dict[str, Any] | None = None   # 新增：只给主循环 / 事件用，不直接展示


# agent/core/turn_facts.py（新增）
DEV_TEST_NONE = "none"
DEV_TEST_CURRENT = "current"
DEV_TEST_STALE = "stale"

@dataclass(frozen=True)
class DevTaskFact:
    task_id: str
    tool_name: str
    phase: str | None
    version: str | None            # content_digest（哪一版）
    submitted: bool
    test_state: str                # none / current / stale
    test_passed: bool | None
    test_summary: str | None

class TurnFacts:
    def record_tool(self, *, call_id: str, tool_name: str, ok: bool,
                    status: str, category: str | None, error: str | None) -> None: ...
    """记一次工具调用的终态。status 取 core.tool_feedback.status_of 的结果。
    同一 tool_name 后写覆盖先写：本轮最后一次结局才算这个工具的结果。"""

    def record_dev_task(self, fact: DevTaskFact) -> None: ...
    """同一 task_id 后写覆盖先写（本轮最后一次操作后的状态才算数）。"""

    def record_declaration(self, *, accepted: bool, basis: str | None) -> None: ...

    @property
    def declaration_accepted(self) -> bool: ...
    def unresolved(self) -> list[str]: ...
    """未解决失败的人话描述（每条一行，已脱敏）。"""
    def annotation(self) -> str | None: ...
    """None = 不加注记；否则是可直接追加到最终答复末尾的文本块。"""


# agent/tools/dev_workspace.py
class DevWorkspace:
    def fact_for(self, task_id: str, tool_name: str) -> dict: ...
    """当前任务事实的机器可读快照（形状见下）。取不到任务时返回 {}。"""


# agent/tools/declare_completion.py（新增）
class DeclareCompletionTool(Tool):
    name = "declare_completion"
    description = TOOL_DECLARE_COMPLETION_DESC
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "开发任务（工作区）id"},
            "version": {"type": "string", "description": "要声明的版本：工作区内容摘要"},
            "claims": {
                "type": "array",
                "items": {"type": "string",
                          "enum": ["test_passed", "registered", "usable"]},
                "description": "要核对的结论，只能从这三项里选",
            },
        },
        "required": ["task_id", "version", "claims"],
    }
    def __init__(self, workspaces, registry, tool_store=None) -> None: ...


# agent/core/loop.py
class AgentLoop:
    # 每轮新建一份台账；回填工具结果时同步记账
    def _record_turn_facts(self, call, result: ToolResult) -> None: ...
    # _run 收尾（final_content 定稿之后）：
    #   note = self.turn_facts.annotation()
    #   if note:
    #       final_content = (final_content or "") + "\n\n" + note
```

开发工具上报的事实形状（`ToolResult.facts`）：

```json
{"dev_task": {"id": "ws_ab12cd34ef56", "tool_name": "dev_run_tests",
              "phase": "testing_failed", "version": "a1b2c3…（64 位摘要）",
              "submitted": false,
              "test": {"state": "stale", "passed": true, "summary": "1/2 tests passed"}}}
```

上报方（都要带上**操作之后**的状态，而不是操作之前的）：

- `create_tool`：新任务，`test.state = "none"`、`submitted = false`；
- `dev_write_file`：写入完成后的状态（这一步通常会把证据打成 `stale`）；
- `dev_run_tests`：本次测试结果（`current`）；
- `dev_submit_tool`：提交（含失败）之后的最终状态。

注记文案（固定模板，`{…}` 只是占位说明，实际文本里不出现花括号）：

```text
—— 系统核对（后端事实，不是模型的说法）：
· dev_run_tests（任务 ws_ab12cd34ef56）：测试失败（断言不匹配）；证据已失效，需重跑。
· 任务 ws_ab12cd34ef56 未提交。
这几项没有通过验证，不能当作「已完成 / 可使用」。
```

## 验收方式

- 单元：`TurnFacts` 的四种组合 —— 干净不加、有未解决失败加、有被接受的声明不加、
  取消不加；注记经过脱敏与限长。
- 单元：`declare_completion` 判定矩阵 —— 版本不符 / 证据失效 / 未提交 / 未知任务 /
  `subagent` 型工具（不要求测试）/ 全满足。
- 集成：模型在工具失败后说「测试全部通过」→ 最终答复带注记（真事故形状）。
- 集成：模型先声明、核对通过 → 最终答复**不带**注记，工具结果里写明依据。
- 集成：失败后重试成功 → 不算未解决失败，不加注记。
- 回归：`maintenance` 的后台轮（空注册表）不会加注记；`re.search(r"\{.*\}")` 取 JSON 仍成立。
- 命令：`cd backend; uv run --frozen pytest`（必须全绿）、`python scripts/check_docs.py`。
- 前端本轮不改，不需要跑 `vue-tsc` / `npm test`。

## 后续（本规格不做，但要在 status 里留名）

1. **前端「已核对」标记**：需要决定核对结论怎么随 assistant 消息持久化
   （新增迁移，或复用现有消息字段），再在消息上渲染；`TurnResult` 与本规格的
   `facts` 已经为它准备好数据来源。
2. **声明覆盖面的扩展**：本轮只服务开发任务；将来「网页 / 桌面操作」类结论要核对时，
   沿用同一张台账与同一套 `claim` 协议扩展，不新造体系。
