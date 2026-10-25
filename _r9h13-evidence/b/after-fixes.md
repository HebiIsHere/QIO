# R9-H13 B：对象判定过严（B1/B2/B3）修复与证据

分支：`r9h13/b`（worktree `D:\qio-dev\qio-r9h13-b`），基线 `e68fb9b7`（r9h12/main）。
本轮只改：`backend/src/agent/security/object_state.py`、
`backend/tests/test_r9h13_b_object_state.py`、`_r9h13-evidence/b/**`。

## 提交

| SHA | 内容 |
| --- | --- |
| 46a06afd | B1/B2/B3 真实流程红用例 + 基线证据（5 red / 4 guard；见 baseline-facts.md） |
| 9ec0e3fa | B3 同名入口解析到独立真实对象时按对象事实继续（失败关闭保留） |
| 84e22387 | B2 消费保存时路径对象身份（限制路径消失不再持续拒绝无关对象，旧别名仍保护） |
| 3ebaa602 | B1 程序概念限制不阻断普通文件读取（新 registered basis） |
| （本文件所在提交） | 最终证据（after 日志、邻居结果、旧期望清单） |

## B1 未绑定局部程序读取限制不得拒绝普通文件读取

* 根因（基线实测，`baseline-facts.md`）：真实保存“禁止读取局部程序 localtool”
  （局部程序不在 PATH）后，条目只有 `binding_state=unresolved` /
  `binding_failure=lookup_failed`，没有候选/身份；`_unbound_path_decision` 对
  **所有**候选（含普通 report.txt 的文件路径读取）返回 UNKNOWN
  `binding_missing_lookup_unresolved`；引擎聚合 UNKNOWN → DENY，
  read_only/restricted/unrestricted 三种模式都拒绝普通读取（decision_code=
  object_state_unconfirmed）。
* 修改（object_state.py，Lead 2026-10-25 决策）：
  1. 注册新 UNRELATED basis `program_concept_file_read_out_of_scope`（白名单校验
     要求带足 limit_kind/has_saved_candidates=False/read_path_scope/operation/
     candidate/candidate_path/resolution 事实）；
  2. 仅当“limit kind=program_name + 无有效绑定 + 无任何 binding_candidates +
     operation=read + candidate_kind=path”时适用：当前环境解析限制名称**只作
     候选事实**——解析成功且本次候选身份可读且相同 → 仍 UNBOUND
     （binding_missing_same_object，需专用澄清/绑定）；解析到不同对象
     （identity 可读且不同 / 生命周期不同）或解析失败 → UNRELATED；
  3. 执行/写/入口名候选、有绑定/有候选的历史数据、显式 program 路径限制
     （P3）全部保持原失败关闭语义。
* 残留边界（契约要求如实注明）：保存时从未识别出对象、且程序本体后来只以
  普通路径被读取时，无法证明是否为该程序（需澄清绑定后才能按对象身份保护）。
  这不是“未知直接允许”：只有“程序概念限制 + 无任何保存对象/候选 + 普通文件路径
  读取”这一范围事实成立时才继续。

## B2 限制对象删除后不得持续拒绝无关对象

* 根因（基线实测）：path 限制 A 的保存结构不带 identity/birth_key；A 存在时读 B
  可靠无关（path_different_object），A 删除后 `_path_core` 因限制路径身份不可读
  返回 UNKNOWN `path_limit_target_unreadable`，B 被永久拒绝；旧对象硬链接别名
  也无法按保存身份命中。
* 修改（object_state.py）：
  1. 新增 `_saved_object_facts`：读取限制条目上的保存时对象事实
     `identity`/`birth_key`/`exists`/`observed_at`/`source`（纯数据，不查当前
     路径/不查程序）；
  2. `_path_core` 在“候选存在”后优先用保存身份比较：身份相同 → MATCH
     `path_hardlink_same_object`（旧对象硬链接/别名仍保护）；identity 可读且不同
     → UNRELATED `path_different_object`；同 identity 不同 birth → UNRELATED
     `path_different_lifecycle`；候选身份不可读/生命周期不可读 → UNKNOWN；
  3. 历史数据缺保存身份时保持原 UNKNOWN 失败关闭（不猜路径/名称）。
  该 facts 已贯通 path / path_prefix / directory / 显式 program 名称入口分支。
* 依赖（阻塞项）：保存结构由 A 的 requirement_limits 保存流程落库（Lead 已收
  精确需求：identity/birth_key/exists/observed_at/source，首次形成条目时采集、
  已存在不覆盖、normalize/serialize/deserialize/merge/remove 原样保留）。
  本分支用**构造的保存事实**经真实引擎入口注入验证（monkeypatch
  `_requirement_for` / `_requirement_binding_state` 只补保存字段，判定逻辑真实），
  真实保存端到端待 A 合入 r9h13/main 后复验。

## B3 同名入口解析到独立对象时按对象事实判断

* 根因（基线实测）：`_name_branch` 在“已绑定 + 入口名相同”时只比较绑定身份，
  不一致就 INVALID `entry_name_same_object_differs`，没有核对入口当前解析到的
  真实对象（identity 可读且不同、入口路径≠绑定路径）。B 绝对路径本已允许，
  同名入口却被判绑定失效。
* 修改（object_state.py）：
  1. 同名入口 + 解析对象 identity 可读且与绑定不同 + 解析路径≠绑定路径 →
     UNRELATED `entry_resolves_to_other_object`（扩展该 basis 的 condition 文本，
     required_facts/required_equal 不变），facts 带 entry_name/entry_path/
     identity 比较；
  2. 同名入口仍解析到绑定路径而对象已替换 → INVALID `bound_path_object_replaced`；
  3. 同 identity 不同生命周期 → INVALID `lifecycle_changed_same_identity`；
  4. 解析失败/身份不可读 → INVALID（原失败关闭保留）。

## 测试与命令

```
cd D:\qio-dev\qio-r9h13-b\backend
& .\.venv\Scripts\python.exe -m pytest tests/test_r9h13_b_object_state.py -q -p no:cacheprovider
   -> 10 passed（final-b-tests.txt）

& .\.venv\Scripts\python.exe -m pytest tests/test_r9h11_object_state.py tests/test_r9h12_object_state.py tests/test_r9h12_cross_operation.py -q -p no:cacheprovider
   -> 5 failed, 157 passed（final-required-neighbor.txt；5 条均为待 Lead 修订的旧期望，
      清单与精确 diff 见 old-expectations-r9h13-b.md）

& .\.venv\Scripts\python.exe -m pytest tests/test_r9h11_cross_operation.py tests/test_r9h12_cross_operation.py tests/test_r9h11_regressions.py tests/test_r9h12_regressions.py -q -p no:cacheprovider
   -> 65 passed（cross-regression-final.txt）

& .\.venv\Scripts\python.exe -m pytest tests/test_r9h5_regressions2.py tests/test_r9h6_regressions.py tests/test_r9h7_regressions.py tests/test_r9h8_regressions.py tests/test_r9h9_regressions.py tests/test_r9h10_regressions.py tests/test_r9h4_engine_binding.py tests/test_r9h4_limits_semantics.py -q -p no:cacheprovider
   -> 112 passed（older-neighbor-after-all.txt）
```

## 真实 / 模拟边界

* 真实：AuthorizationEngine + security/object_state 五态判定、fs_read/fs_write/
  run_program 真实工具、真实 SQLite 要求台账与真实保存流程
  （record_user_message + set_structured_limits）、真实临时文件、真实硬链接
  （os.link 后 samefile 断言）、真实 PATH 查找（shutil.which）、真实程序启动
  （系统 shell 副本 + marker + 启动计数）。模型一律 fake（不调用模型）。
* 构造：B2 的保存层字段（identity/birth_key/exists/observed_at/source）目前生产
  保存代码尚未写入，用 monkeypatch 引擎取要求入口注入（只补保存数据，不改判定）；
  B1 的“保存时从未识别对象”场景使用真实保存数据（无候选）。身份不可读边界
  注入未使用（本轮不需要）。

## 旧期望修订（Lead 负责改 H11/H12 测试文件）

清单与精确 diff：`old-expectations-r9h13-b.md`；原始失败输出：
`neighbor-red-after-b3-expected.txt`（B3 4 条）、`final-required-neighbor.txt`
（+ B1 白名单 1 条）。本分支未修改任何 H11/H12 测试文件。

## 已知限制 / 未完成

* B2 真实保存端到端依赖 A 的 requirement_limits 变更（Lead 已协调）；合入前
  本分支只验证了 object_state 消费侧与真实引擎链路。
* B1 残留边界见上（未识别程序文件本体被普通路径读取无法保护，需澄清绑定）。
* 5 条旧期望（4 条 B3 + 1 条 B1 白名单）待 Lead 在 r9h13/main 修订。
* 未跑全量 pytest / CI（按契约只跑受影响与邻近回归）；未 push、未合并 main。
