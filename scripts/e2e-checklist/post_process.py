"""清单后处理：补齐冻结窗格与自动筛选（artifact-tool 导出缺失）。
用法：node scripts/e2e-checklist/build_checklist.mjs 后运行本脚本。"""
from pathlib import Path

import openpyxl

# 清单就在 <检出>\docs\ 下：按脚本位置推导，检出换盘不用改。
p = Path(__file__).resolve().parents[2] / "docs" / "e2e-test-checklist.xlsx"
wb = openpyxl.load_workbook(p)
ws = wb["端到端测试清单"]
ws.freeze_panes = "A4"
ws.auto_filter.ref = f"A3:M{ws.max_row}"
wb.save(p)
print("ok")
