# 端到端测试清单生成器
## 使用
1. 编辑 cases.js（新增用例：在对应模块末尾追加对象，编号取该模块下一个序号）
2. node scripts/e2e-checklist/build_checklist.mjs   # 生成 docs/e2e-test-checklist.xlsx
3. python scripts/e2e-checklist/post_process.py      # 补齐冻结窗格与筛选

## 记忆功能验证（run_memory_tests.py）
- 运行：`python scripts/e2e-checklist/run_memory_tests.py`（需后端 8734 运行、active main-loop 凭据、模型文件可选）
- 通道 A（确定性）：短期记忆注入 / 话题预判 / 封块分档 / 话题工具 / 封块提炼链 / 向量后端
- 通道 B（真实模型行为）：对话连续性引用（重试一次）/ switch_topic、create_topic 自主调用（观察项）/ 跨轮次记忆引用
- 输出 results_memory.json；测试话题「记忆验证-」前缀，跑完自动清理并恢复锚点
