# R9-H13 B 证据目录

* `baseline-facts.md`：基线 e68fb9b7 的 B1/B2/B3 真实复现事实、根因与范围规则。
* `baseline-r9h13-b-tests.txt`：基线运行 `tests/test_r9h13_b_object_state.py`
  的 pytest 输出（5 failed, 4 passed；迁移 WARNING 行已过滤）。
* `after-b3-tests.txt` / `after-b2-tests.txt` / `after-b1-tests.txt`：逐项修复后的
  中间运行输出（每步只剩后续子问题的预期红）。
* `after-fixes.md`：修复汇总（提交、根因、修改、真实/模拟边界、依赖与限制）。
* `final-b-tests.txt`：最终 `tests/test_r9h13_b_object_state.py` 结果（10 passed）。
* `final-required-neighbor.txt`：H11/H12 object_state + H12 cross_operation 结果
  （5 failed = 待 Lead 修订的旧期望，157 passed）。
* `neighbor-red-after-b3-expected.txt`：B3 引起的 4 条旧期望红（保留原输入证据）。
* `old-expectations-r9h13-b.md`：旧期望修订清单（旧→新+依据+精确 diff），交 Lead。
* `cross-regression-final.txt`：H11/H12 cross_operation + regressions（65 passed）。
* `older-neighbor-after-all.txt`：H4-H10 邻近回归（112 passed）。
