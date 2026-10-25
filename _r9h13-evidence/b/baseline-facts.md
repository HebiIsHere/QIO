# R9-H13 B：基线复现（e68fb9b7，分支 r9h13/b）

命令（离线，模型 fake；真实引擎/工具/SQLite/临时文件/硬链接/PATH/程序启动）：

```
cd D:\qio-dev\qio-r9h13-b\backend
& .\.venv\Scripts\python.exe -m pytest tests/test_r9h13_b_object_state.py -q -p no:cacheprovider
```

基线结果（完整输出见 baseline-r9h13-b-tests.txt）：

```
5 failed, 4 passed
FAILED test_b1_unbound_local_program_limit_allows_plain_file_in_all_modes
FAILED test_b1_unbound_program_limit_keeps_non_read_operations_fail_closed
       （前半段“普通读取按 B1 范围规则继续”在基线红；修复后该用例继续校验写操作失败关闭）
FAILED test_b2_deleted_limit_target_does_not_block_unrelated_file
FAILED test_b2_deleted_target_old_hardlink_alias_still_blocked
FAILED test_b3_same_name_entry_resolving_to_independent_program_continues
```

B1 范围规则按 Lead 2026-10-25 决策实现：program_name 概念限制 + 无有效绑定 +
无任何保存候选 + operation=read + 候选为具体文件路径 → UNRELATED（新注册 basis
`program_concept_file_read_out_of_scope`）；当前环境名称解析只作候选事实，
解析到本次同一对象时仍 UNBOUND。

## B1：未绑定的局部程序读取限制拒绝普通文件读取

真实保存流程（record_user_message + set_structured_limits；局部程序真实存在于
workspace/tools 下、保存时 PATH 不含该目录）落库的结构化限制条目：

```json
[{"operations": ["read"],
  "objects": [{"kind": "program_name", "value": "localtool",
               "binding_state": "unresolved", "binding_failure": "lookup_failed"}],
  "reason": "禁止读取局部程序 localtool"}]
```

读取普通 `report.txt`（真实 FsReadTool + 真实引擎）：

| 模式 | ok | decision_code | object_decisions.state / basis |
| --- | --- | --- | --- |
| read_only | false | object_state_unconfirmed | unknown / binding_missing_lookup_unresolved ×2 |
| restricted | false | object_state_unconfirmed | unknown / binding_missing_lookup_unresolved ×2 |
| unrestricted | false | object_state_unconfirmed | unknown / binding_missing_lookup_unresolved ×2 |

根因：`_unbound_path_decision` 在“无保存候选 + 限制名称当前解析失败”时对所有
候选（包括普通文件路径读取）返回 UNKNOWN；引擎把 UNKNOWN 聚合为 DENY，三种模式
都无法继续。该路径没有对象事实可证明“report.txt 是那个局部程序”。

## B2：限制对象删除后持续拒绝无关对象

真实保存“禁止读取 A”（path 限制；生产保存结构当前**不带** identity/birth_key）：

1. A 存在时读 B：ok=true（不同身份 → path_different_object）——B 原本可读；
2. 删除 A 后读 B：ok=false，decision_code=object_state_unconfirmed，
   basis=path_limit_target_unreadable（“限制路径当前不存在……不能仅凭路径文本不同判无关”）；
3. A 的真实硬链接别名 alias 在 A 路径删除后读取：也被拒，但 basis 同样是
   path_limit_target_unreadable（原因：保存结构没有路径对象身份，无法按保存身份命中旧对象）。

根因：`_path_core` 在限制路径身份不可读（不存在）时一律 UNKNOWN，没有可消费的
“保存时对象身份”把“无关对象”与“旧对象别名”区分开。

## B3：同名入口解析到独立对象仍被判 INVALID

真实保存“禁止运行 runner”（保存时 PATH 解析到 A 并固化绑定），随后 PATH 改为
指向独立程序 B（两个独立真实可执行文件副本）：

| 入口 | 基线结果 | 说明 |
| --- | --- | --- |
| B 绝对路径 | ok=true，真实启动写 marker | 相邻正常放行（对象身份不同） |
| 同名入口 runner | ok=false，decision_code=program_binding_invalidated | `_name_branch` 在“绑定 + 入口名相同”时只要绑定身份不一致就 INVALID，未核对入口当前解析到的真实对象 |

## 邻居守卫（基线绿、修复后必须保持）

* B1 当前解析成功且就是本次文件（局部程序本体按路径读取）→ UNBOUND（需澄清），
  不得放行；
* B1 有保存候选（即使无 identity）→ 保持 UNKNOWN 失败关闭，新范围规则不适用；
* B1 执行/入口名候选 → 保持失败关闭（0 启动、无 marker）；
* B2 历史数据无保存身份 → A 删除后读 B 仍 UNKNOWN（不得猜无关）；
* B3 同名入口仍解析到绑定对象 → MATCH 拒绝、0 启动；
* B3 同名入口解析到绑定路径但对象身份已变 → INVALID 拒绝、0 启动。

## 真实 / 构造边界

* 真实：真实引擎/工具/SQLite 台账/临时文件/真实硬链接/真实 PATH 查找/真实
  程序启动（marker + 启动计数）。
* 构造：B2 的“保存时路径对象 identity/birth_key/exists”通过 monkeypatch
  `AuthorizationEngine._requirement_for` 注入（只补保存字段，不改判定逻辑）；
  生产保存结构由 A 的 requirement_limits 变更落库（Lead 已收需求）。B1 当前
  按 Lead 决策只用真实保存结构（无候选）即可复现与验收。
