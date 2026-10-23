# R9-H4 A 报告：要求理解、限制保存、继承与解除

> 基线：`67fa88c6`（r9h4/main 起点：冻结契约 + 迁移 65/66 身份 birth 生命周期）
> 分支：`r9h4/a`；worktree：`D:\qio-dev\qio-r9h4-a`
> 本轮**未触发完整 CI / 全量矩阵**（累计 2/2 已用尽）；只做文件级定向测试。
> 模型调用全部 fake/mock；未 push、未 merge main、未改历史迁移、未删除/skip/xfail 既有测试。
> 未改 engine.py/app.py/tools/effects.py/schema（B/Lead 负责；接线需求见 §7）。

## 0. 结论摘要

| 项 | 结果 |
| --- | --- |
| 操作名/对象 kind 统一 | `requirement_limits.LIMIT_OPERATIONS` 增加 `read`（保留 egress 兼容）；kind 已有 path/path_prefix/directory/program/service；`resolution.py` 的解析白名单改为**直接引用同一来源**，模型接受与保存层不再不一致 |
| 父 unresolved 已有 known limits | amend 子要求**继承 known**（apply_resolution 与直接 add_correction 都覆盖）；状态保持 unresolved（部分已知≠完整），恢复失败不会清空 |
| removed_limits 精确解除 | 新增 `remove_limits_precise`：可只解除部分对象/部分操作；解除 B write 不清 A、不误删 B delete；无法精确表达时抛 `LimitRemovalAmbiguous` → 保留父 known + unresolved，不猜测 |
| 恢复失败/空结果 | invalid/空 limits 不再清空父 known；setter `limits=None` 保留已知限制；逐条容错保留可靠条目 |
| 重启/后台 | 结构化限制与 unresolved 状态持久化，新 ledger 实例读回一致；new/amend/withdraw 均保持 |
| 提示词 | 只改要求理解/限制表达/removed_limits 输出契约；规范操作与 kind 单一来源；不引入关键词 |
| 定向测试 | A 新文件 **22 passed**；相关既有回归 **197 passed**；另有 1 个基线迁移断言红（`test_r9h3_state_migration` 断言 64，基线已是 66，Lead 迁移范围，A 未改） |
| 跨轮适配 | 更新旧 A 文件 `tests/test_r9h2_chain_recovery.py` 中 read 被当非法操作的旧断言（H4 规范 read/program），保留其它断言 |

## 1. 交付文件

| 文件 | 状态 | 内容 |
| --- | --- | --- |
| `backend/src/agent/security/requirement_limits.py` | 修改 | 规范操作增加 read；`LimitRemovalAmbiguous`；`remove_limits_precise`；merge_limits 改为精确移除；`known_limits_from_requirement` |
| `backend/src/agent/security/requirements.py` | 修改 | `_normalize_limits_tolerant`；`_child_structured_limits`；add_correction 继承 known；apply_resolution amend/new 容错与精确解除、保留 known |
| `backend/src/agent/security/resolution.py` | 修改 | 白名单直接引用 requirement_limits（read/program/path_prefix）；允许 objects-only 条目；kind 默认 path |
| `backend/src/agent/prompts.py` | 修改（解析段） | 要求理解/限制输出契约：规范操作/kind、read/program、removed_limits 精确范围、不得借 amend 清空 |
| `backend/tests/test_r9h4_limits_semantics.py` | 新增 | 22 用例：真实 ledger/apply_resolution/merge/extract |
| `backend/tests/test_r9h2_chain_recovery.py` | 跨轮适配 | read/program 从"非法示例"改为"规范接受"，保留其它断言 |
| `docs/r9h4-reports/A.md` | 新增 | 本报告 |

未改：`security/engine.py`、`recheck.py`、`app.py`、`tools/*`、`security/effects.py`、schema/迁移。

## 2. 统一操作名与对象 kind（契约 §2）

单一来源 `requirement_limits`：
```python
LIMIT_OPERATIONS = {read, write, create, delete, execute, install, system_change, egress}
LIMIT_OBJECT_KINDS = {path, path_prefix, directory, program, service}
```

* `resolution.ALLOWED_LIMIT_OPERATIONS / LIMIT_OBJECT_KINDS` 直接引用上述集合；模型输出被解析接受后，`apply_resolution` → `normalize_limits` → 持久化使用同一白名单；
* 旧宽白名单（shell/internal_data/tool_register/subagent/material/record/other）不再作为限制操作/kind 接受；egress 仅保留兼容既有条目；
* 字符串对象/缺省 kind 统一为 `path`（不再默认 other）；
* 限制条目允许 operations-only、objects-only 或两者（objects-only 表示该对象上所有操作；B 消费时需按此语义）；
* 测试覆盖 read + program 从提示词/解析（`parse_resolution_outcome/validate_resolution`）到保存、重启读回、链恢复提取。

## 3. 父 unresolved 的 known limits 继承（契约 §4）

### 3.1 新增读取

`known_limits_from_requirement(requirement)`：
* structured → 完整已知集合（可能空=明确无限制）；
* legacy/unresolved → 返回已保存的**部分** known（tuple，可能空），调用方不得当完整；
* 列 NULL/JSON 非法 → None（未知）。

### 3.2 amend 继承

`RequirementLedger._child_structured_limits`：
* parent structured：精确 merge added/removed，child structured；
* parent unresolved/legacy + known：**先继承 known**，再做精确 add/remove；child state 保持 unresolved/legacy；
* parent 没有 known 列：只能保存本次可靠新增（若有），state unresolved；
* removed 无法精确表达：child_limits=父 known、state unresolved、source 追加 `removed_ambiguous`；
* `apply_resolution` amend 与直接 `add_correction` 都走这套语义；`AppliedResolution.limits_state/structured_limits` 返回真实结果。

验证：unresolved + known(B write) 连续 amend 两次仍继承 B；append 新 read 限制后 known 为 B+read；直接 add_correction 同样继承；重启后保持。

### 3.3 失败/空结果

* 模型 limits 非法：逐条容错，合法条目保留，非法条目丢弃，state unresolved，父 known 保留；
* 空 limits/removed 的空 amend：继承父 known，不清空；
* `set_structured_limits(limits=None, state=unresolved)`：保留已有 known（H2 语义保持）；
* invalid limits 的 new：合法条目仍保存，state unresolved（不移交"确认无限制"）。

## 4. removed_limits 精确解除（契约 §4）

条目语义（后端结构化集合运算，不做文字判断）：
* removed 有 operations 无 objects：解除这些操作在**所有对象**上的限制；
* removed 有 objects 无 operations：解除这些对象上**所有操作**的限制；
* 两者都有：只解除交集；例如：
  - 父 `{ops:[write,delete], objects:[B]}` + 父 `{ops:[write], objects:[A]}`，removed `{ops:[write], objects:[B]}` → 保留 A write、B delete，移除 B write；
  - 父 `{ops:[write], objects:[A,B]}`，removed `{ops:[write], objects:[B]}` → 保留 A write；
  - 父 `{ops:[], objects:[B]}`（B 上所有操作），removed `{ops:[], objects:[B]}` → 整条移除；
* 无法精确表达剩余集合时抛 `LimitRemovalAmbiguous`（例如从"所有操作"中只解除 write；从"所有对象"中只解除 B；removed 指向父限制没有的对象/操作），caller 保留父 known 并标 unresolved，禁止猜测/静默忽略/清空全部；
* 可精确 target read/program（测试覆盖 execute+program 解除）。

## 5. 逐条容错（契约 §2）

`_normalize_limits_tolerant`：对模型 limits 数组逐条 normalize；坏条目跳过并记录 invalid，合法条目保留；`apply_resolution`：
* new：保存合法部分；有非法条目 → state unresolved（不写"明确无限制"）；
* amend：父 known + 合法新增/合法解除；非法条目 → state unresolved，父 known 不清空；removed 不合法时无法核对的部分不猜测；
* source 追加 `invalid_limits/removed_invalid` 等后缀便于审计。

## 6. 提示词（prompts.py 解析段）

* 要求解析 system：规范操作白名单（含 read）、kind 白名单（含 path_prefix/program）、对象-only 语义；removed_limits 可只写具体 operations/objects，不得扩大到整条限制，无法精确确定时保留原限制并在 reason 说明；
* 历史单句/链恢复 system：操作/kind 同步 canonical；read/program 可作为限制；条目 operations/objects 至少一个；不引入关键词/句式/全局语言风格改动；
* 测试静态检查提示词不含旧宽白名单词（material/record/shell/internal_data/tool_register/subagent）。

## 7. 引擎/执行层接线需求（A 不改 engine）

A 只保证解析/保存/继承/解除语义；以下**必须由 B/Lead 在 engine、recheck、effects/工具层消费**，否则会出现"保存接受但执行不生效"的缺口：

1. `engine._LIMIT_OPERATION_NAMES` 增加 `EffectKind.READ -> "read"`（当前只映射 write/create/delete/execute/install/system_change/egress/INTERNAL_DATA→write）；
2. program 对象匹配：`_limit_conflict` 需要把 `program` kind 与 effect 的 program target/argv 程序身份做结构化核对；path/path_prefix/directory 继续按规范化路径；相对路径限制按 `workspace_root`（detail cwd 明确时用 cwd）规范化；
3. `Effect.detail.limit_impacts` 多个实际影响（每项 operation + objects）逐项与限制核对；
4. `EffectKind.INTERNAL_DATA` 不再固定 write，应按 detail 的 `internal_operation`（read/write/create/delete/execute）或明确事实；无法确定保留不确定（B/Lead 共享文件 effects.py 只能 Lead 改）；
5. engine/recheck 在 legacy/unresolved 时使用 `requirement.structured_limits`（known 部分）而不是只看 state（A 已提供 `known_limits_from_requirement` 可读取，但 B 也可直接读属性）；
6. `merge_limits` 现在会对无法精确解除抛 `LimitRemovalAmbiguous`（ValueError 子类）；外部调用方需捕获并保留原限制+unresolved（当前仓库内仅 requirements.py 调用，已处理）；
7. objects-only 限制语义 = 该对象上所有操作；对象列表为空限制语义 = 所有对象（既有），engine 匹配需保持；
8. 契约 §2 "限制只取操作绑定要求与有效继承链"：engine/recheck 仍需使用绑定要求（A 未改 binding 逻辑）。

## 8. 测试与验证

### 8.1 命令与结果

```powershell
cd D:/qio-dev/qio-r9h4-a/backend
uv sync --frozen --extra dev

# A 新测试
uv run --frozen python -m pytest tests/test_r9h4_limits_semantics.py -W ignore
# => 22 passed in 6.63s

# 相关既有定向回归（要求/解析/绑定/链恢复/引擎/H3 限制）
uv run --frozen python -m pytest tests/test_r9h4_limits_semantics.py tests/test_r9h_limits.py tests/test_r9h2_chain_recovery.py tests/test_r9g_resolution.py tests/test_r9h_binding_flow.py tests/test_r9f_requirements.py tests/test_r9h2_parse_state.py tests/test_r9h2_real_flow.py tests/test_r9h3_limits_priority.py tests/test_authorization_engine.py tests/test_r9g_acceptance_e2e.py -W ignore
# => 197 passed in 99.29s

# 编译
uv run --frozen python -m py_compile src/agent/security/requirements.py src/agent/security/requirement_limits.py src/agent/security/resolution.py src/agent/prompts.py tests/test_r9h4_limits_semantics.py
# => OK
```

### 8.2 基线既有红

`tests/test_r9h3_state_migration.py::test_versions_and_compensation_are_additive_only`：断言 `SCHEMA_VERSION == COMPENSATION_VERSION == 64`，基线 r9h4/main 已加入迁移 65/66，实际 66。**A 未改 schema/迁移/该测试**，属基线既有红（Lead 迁移范围）；本次定向集合未把它计入通过数。

### 8.3 A 新测试覆盖

1. read/program 规范化、serialize/deserialize、解析/校验、apply_resolution 保存、重启读回、链恢复提取；
2. unresolved 父 known(B) 连续 amend 两次仍继承；append read 后 B+read；直接 add_correction 继承；
3. 父 unresolved known 精确解除 B write 后保留 B delete、state unresolved；
4. 部分操作/部分对象解除（B write 不清 A、不误删 B delete；多对象条目拆分；全操作条目移除）；
5. 无法精确解除：从"所有操作"只解除 write、removed 指向不存在对象 → 保留 known + unresolved + source 标记；
6. 恢复失败/非法 limits/空 amend/new-with-invalid 不清 known；逐条容错保留可靠条目；
7. new/withdraw/重启保持 structured limits；setter limits=None 保留 known；
8. 提示词规范操作/kind 静态检查；解析器拒绝非规范操作/kind；
9. read/program removed 精确解除。

## 9. 与契约的偏差 / 边界

1. **egress 保留**：契约 §2 说本轮排除 egress 但保留既有；A 保留在 `LIMIT_OPERATIONS`（可解析/持久化），未删除旧数据兼容；新提示词标注"仅兼容旧条目"。
2. **objects-only 条目**：A 允许（=该对象上所有操作），因为 requirement_limits 原有语义与 H2 数据可能存在；引擎消费需按此解释（§7-7）。
3. **ambiguous removed 时同一 amend 的合法新增也暂不写**：保留父 known 最保守；报告/测试明确。若 Lead 要求"合法新增仍写 + unresolved"，需进一步契约冻结（A 未擅自扩大）。
4. **父 unresolved 的 known 继承后 state 仍 unresolved**：不得因继承 known 就标 structured（隐藏未知部分仍未知）；引擎按 unresolved 规则处理（B 负责）。
5. **merge_limits 行为变更**：未匹配/无法表达的 removed 由旧"静默忽略"改为抛 `LimitRemovalAmbiguous`；调用方必须保留原限制+unresolved。仓库内唯一调用方 requirements.py 已处理；外部脚本需适配。
6. **提示词 kind 默认 path**：模型省略 kind/字符串对象按 path 处理（结构化、不猜名称）；旧 other/material 等被拒绝。

## 10. 未完成 / 未验证

| 项 | 状态 |
| --- | --- |
| engine/recheck 消费 read/program/limit_impacts/internal_operation | **未做**（B/Lead 共享文件）；A 报告 §7 给接线需求；当前解析保存可用，但执行层可能尚未拦截 read/program 限制 |
| app.py 接线/真实 E2E | Lead/B（A 未改） |
| 完整 CI / 全量 pytest | **禁止**（累计 2/2 用尽）；未运行 |
| 真实模型 / 提示词真实效果 | 未运行（fake/mock） |
| 非 Windows / SQLite 并发 | 未验证 |
| 后台/重启真实进程端到端 | A 只验证同 DB 新 ledger 重启读回；真实后台进程由 B/Lead |
| 迁移 65/66 身份 birth 交互 | C/Lead 范围，A 未涉及 |

## 11. 复现命令

```powershell
cd D:/qio-dev/qio-r9h4-a/backend
uv run --frozen python -m pytest tests/test_r9h4_limits_semantics.py -W ignore
uv run --frozen python -m pytest tests/test_r9h_limits.py tests/test_r9h2_chain_recovery.py tests/test_r9g_resolution.py tests/test_r9h_binding_flow.py tests/test_r9f_requirements.py tests/test_r9h2_parse_state.py tests/test_r9h2_real_flow.py tests/test_r9h3_limits_priority.py tests/test_authorization_engine.py -W ignore
```
