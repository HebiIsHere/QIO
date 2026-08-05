# 端到端测试清单生成器
## 使用
1. 编辑 cases.js（新增用例：在对应模块末尾追加对象，编号取该模块下一个序号）
2. node scripts/e2e-checklist/build_checklist.mjs   # 生成 docs/e2e-test-checklist.xlsx
3. python scripts/e2e-checklist/post_process.py      # 补齐冻结窗格与筛选
