"""把 e2e 结果回填到 docs/e2e-test-checklist.xlsx（实际结果 + 结论 + 备注）。"""
import json
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[2]
XLSX = ROOT / "docs" / "e2e-test-checklist.xlsx"
API_RES = json.loads((Path(__file__).parent / "results_api.json").read_text(encoding="utf-8"))
UI_RES = json.loads((Path(__file__).parent / "results_ui.json").read_text(encoding="utf-8"))

# 环境前置缺失（非产品缺陷）→ 阻塞
PRECOND_BLOCKED = {"TURN-001", "TURN-002", "TURN-003", "MEM-001", "MEM-003", "CRED-002"}
# MEM-001 无对话数据同样源于无凭据 → 阻塞
PRECOND_BLOCKED.add("MEM-001")
PRECOND_KEYS = ["前置缺失：无活动凭据", "无活动凭据", "重启后旧凭据因持久化缺陷修复已丢失"]

# 缺陷编号（本次发现的真实缺陷）
DEFECTS = {
    "UI-001": "DEF-001",
}

def _sanitize(text):
    """去除 Excel 非法控制字符。"""
    return "".join(ch for ch in (text or "") if ch >= " " or ch in "\n\t\r")

def classify(entry):
    detail = (entry.get("actual") or "") + (entry.get("detail") or "")
    if not entry["passed"]:
        if any(k in detail for k in PRECOND_KEYS):
            return "阻塞", f"环境前置缺失：{detail[:100]}"
        return "失败", detail[:200]
    return "通过", detail[:200]

wb = openpyxl.load_workbook(XLSX)
ws = wb["端到端测试清单"]
by_id = {}
for entry in API_RES + UI_RES:
    by_id[entry["id"]] = entry

for row in range(4, ws.max_row + 1):
    case_id = ws.cell(row, 1).value
    if not case_id or case_id not in by_id:
        continue
    entry = by_id[case_id]
    conclusion, note = classify(entry)
    ws.cell(row, 8).value = _sanitize(note)  # 实际结果
    ws.cell(row, 9).value = conclusion    # 结论
    ws.cell(row, 11).value = "e2e 自动化"
    if case_id in DEFECTS:
        ws.cell(row, 10).value = DEFECTS[case_id]
        ws.cell(row, 13).value = f"缺陷 {DEFECTS[case_id]}：{note[:80]}"
wb.save(XLSX)
print("checklist updated")
