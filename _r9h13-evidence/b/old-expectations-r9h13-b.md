# R9-H13 B 引起的旧期望修订清单（提交给 Lead 修订 H11/H12 测试文件）

原则：不删除用例、不 skip/xfail、不放宽产品断言；保留原输入与复现步骤，只把
期望改成 H13 契约 §3.B 的正确语义。以下 4 条在 `r9h13/b` 修复 B3 后变红
（完整输出 `neighbor-red-after-b3-expected.txt`），修复前在基线 e68fb9b7 全绿。

命令：`cd D:\qio-dev\qio-r9h13-b\backend; .\.venv\Scripts\python.exe -m pytest tests/test_r9h11_object_state.py tests/test_r9h12_object_state.py -q -p no:cacheprovider`

依据（H13 契约 §3.B3）：禁止绑定到 A，同名入口当前解析到独立真实对象 B（identity
可读且与绑定不同、入口路径≠绑定路径）→ 按对象事实判可靠不同对象并继续；只有
“同名入口仍解析到绑定路径但身份/生命周期变化”或“解析失败/身份不可读”才
INVALID/失败关闭。

---

## 1. `tests/test_r9h11_object_state.py::test_binding_entry_name_same_object_differs_is_invalid`（约 :1087）

* 旧期望：`state is INVALID`、`basis == "entry_name_same_object_differs"`、
  `"绑定" in detail`；
* 新期望：`state is UNRELATED`、`basis == "entry_resolves_to_other_object"`、
  `validate_unrelated_decision(decision) == ()`，facts 中 `entry_name` 为同名、
  `identity_key.equal is False`、`entry_path.equal is False`（解析路径≠绑定路径）；
* 原输入不变：`_binding_item("runner", _binding(program))` +
  `_candidate("program_name", "runner")` + resolver 返回另一个真实程序 other；
* 建议同时把函数名改为 `test_binding_entry_name_same_resolves_other_object_is_unrelated`，
  或在 docstring 写“R9-H13 §3.B3 修订（原输入/步骤保留）”。

## 2. `tests/test_r9h11_object_state.py::test_effect_entry_name_same_source_dominates_path_unrelated`（约 :1192）

* 旧期望：`state is INVALID`、`basis == "entry_name_same_object_differs"`、
  `"绑定" in detail`；
* 新期望：`state is UNRELATED`、`basis == "entry_resolves_to_other_object"`、
  `validate_unrelated_decision(decision) == ()`；
* 依据：路径候选（current 绝对路径）与绑定不同对象；真实入口名 "runner" 当前
  解析到 current（identity 可读且不同、入口路径≠绑定路径）——两条依据都指向
  “可靠不同对象”，同来源名不再单独构成 INVALID；
* 原输入不变：bound A + PATH→current + effect.requested_program="runner"；
* 建议保留函数名或改名为 `test_effect_same_entry_resolves_other_object_allowed`，
  docstring 标注 H13 §3.B3。

## 3. `tests/test_r9h11_object_state.py::test_effect_entry_name_from_execution_program`（约 :1209）

* 旧期望：`state is INVALID`、`basis == "entry_name_same_object_differs"`；
* 新期望：`state is UNRELATED`、`basis == "entry_resolves_to_other_object"`；
* 依据同 2（本用例入口名来自 `detail.execution.program`，解析到 current）；
* 原输入不变。

## 4. `tests/test_r9h12_object_state.py::test_merge_invalid_dominates_path_unrelated`（约 :770）

* 旧期望：`state is INVALID`、`basis == "entry_name_same_object_differs"`；
* 新期望：`state is UNRELATED`、`basis == "different_object_identity"`
  （路径候选与入口解析对象都与绑定不同对象；merge 两者同为 UNRELATED，
  primary 路径依据胜出），并 `validate_unrelated_decision(decision) == ()`；
* 依据：H13 §3.B3——同名入口解析到独立对象不是 INVALID；该用例不再存在
  “INVALID 压过 UNRELATED”的事实前提；
* 原输入不变：bound runner + PATH→current + candidate=unrelated 绝对路径 +
  effect requested "runner"；
* 建议改名 `test_merge_unrelated_path_and_entry_resolves_other_object`，
  docstring 标注 H13 §3.B3。

---

## 待 B1 实现后追加：H12 UNRELATED 白名单冻结列表

`tests/test_r9h12_object_state.py::test_unrelated_registry_matches_frozen_whitelist`
在 B1 新增 basis 后会红。将提供精确 diff（新常量项），由 Lead 修订。
