// 端到端测试清单生成器
import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";
import { CASES } from "./cases.js";

const HEADERS = [
  "用例编号", "模块", "测试标题", "优先级", "前置条件",
  "测试步骤", "预期结果", "实际结果（填写区）", "结论", "缺陷编号", "执行人", "执行日期", "备注",
];
const COL_W = {
  A: 12, B: 11, C: 26, D: 9, E: 28, F: 52, G: 44, H: 44, I: 10, J: 11, K: 10, L: 12, M: 24,
};
const MODULES = ["系统与服务", "凭据体系", "对话与主循环", "记忆域", "知识域", "图导航", "工具机制", "审批流", "前端交互", "桌面打包"];

const wb = Workbook.create();
const sheet = wb.worksheets.add("端到端测试清单");
wb.worksheets.add("使用说明");

// ---- 标题区 ----
sheet.getRange("A1:M1").values = [["Smart Agent 端到端测试清单"]];
sheet.getRange("A1:M1").merge?.();
sheet.getRange("A1").format.font = { bold: true, size: 16, color: "#FFFFFF" };
sheet.getRange("A1").format.fill = "#1c2c55";
sheet.getRange("A1").format.rowHeight = 30;

sheet.getRange("A2:M2").values = [[
  `项目：Smart Agent · 版本 v0.7 · 用例总数 ${CASES.length} · 生成日期 2026-08-03`,
  "", "", "", "", "", "", "", "", "", "", "", "",
]];
sheet.getRange("A2").format.font = { italic: true, size: 10, color: "#8fa8e0" };
sheet.getRange("A2").format.rowHeight = 20;

// ---- 表头 ----
const headerRange = sheet.getRange("A3:M3");
headerRange.values = [HEADERS];
headerRange.format.fill = "#2a3f7e";
headerRange.format.font = { bold: true, color: "#FFFFFF", size: 11 };
headerRange.format.borders = { preset: "all", style: "thin", color: "#3a5a9e" };
headerRange.format.rowHeight = 34;
headerRange.format.wrapText = true;
headerRange.format.hAlign = "center";
sheet.freezePanes.freezeRows(3);
try { sheet.freezePanes = { freezeRows: 3 }; } catch {}

// ---- 数据 ----
const rows = CASES.map((c) => [
  c.id, c.mod, c.title, c.pri, c.pre, c.steps, c.expect, c.actual,
  "", "", "", "", "",
]);
const dataStart = 4;
const dataRange = sheet.getRange(`A${dataStart}:M${dataStart + CASES.length - 1}`);
dataRange.values = rows;

// 数据区样式：wrap + 高行 + 对齐
dataRange.format.wrapText = true;
dataRange.format.valign = "top";
dataRange.format.rowHeight = 150;
dataRange.format.font = { size: 10 };
dataRange.format.borders = { preset: "all", style: "thin", color: "#22305a" };
// 优先级/结论居中
sheet.getRange(`D${dataStart}:D${dataStart + CASES.length - 1}`).format.hAlign = "center";
sheet.getRange(`I${dataStart}:I${dataStart + CASES.length - 1}`).format.hAlign = "center";
// 模块列按模块底色区分（浅色）
let prev = "";
let colorIdx = 0;
const MOD_COLORS = ["#16224a", "#182550"];
CASES.forEach((c, i) => {
  const r = dataStart + i;
  if (c.mod !== prev) { colorIdx = (colorIdx + 1) % MOD_COLORS.length; prev = c.mod; }
  sheet.getRange(`A${r}:M${r}`).format.fill = MOD_COLORS[colorIdx];
  sheet.getRange(`B${r}`).format.fill = "#22305a";
});
// 表头行冻结后，表头样式已设置；设置列宽
for (const [col, w] of Object.entries(COL_W)) {
  sheet.getRange(`${col}3:${col}${dataStart + CASES.length - 1}`).format.columnWidth = w;
}

// 数据验证
const lastRow = dataStart + CASES.length - 1;
sheet.getRange(`D${dataStart}:D${lastRow}`).dataValidation = {
  allowBlank: true,
  list: { inCellDropDown: true, source: ["P0", "P1", "P2"] },
};
sheet.getRange(`I${dataStart}:I${lastRow}`).dataValidation = {
  allowBlank: true,
  list: { inCellDropDown: true, source: ["通过", "失败", "阻塞", "跳过"] },
};
sheet.getRange(`L${dataStart}:L${lastRow}`).dataValidation = {
  rule: { type: "date", operator: "greaterThanOrEqualTo", formula1: "=DATE(2026,1,1)" },
  errorAlert: { style: "stop", title: "无效日期", message: "请输入有效日期（yyyy-mm-dd）" },
};
sheet.getRange(`L${dataStart}:L${lastRow}`).format.numberFormat = "yyyy-mm-dd";

// 自动筛选
sheet.getRange(`A3:M${lastRow}`).autoFilter = true;

// ---- 使用说明 sheet ----
const guide = wb.worksheets.getItem("使用说明");
const guideRows = [
  ["Smart Agent 端到端测试清单 · 使用说明", "", "", ""],
  ["", "", "", ""],
  ["一、编号规则", "", "", ""],
  ["1. 编号格式：模块前缀-三位序号，如 SYS-001、CRED-001、TURN-001。", "", "", ""],
  ["2. 模块前缀对照：SYS=系统与服务，CRED=凭据体系，TURN=对话与主循环，MEM=记忆域，KNOW=知识域，GRAPH=图导航，TOOL=工具机制，APPROVAL=审批流，UI=前端交互，PACK=桌面打包。", "", "", ""],
  ["3. 前缀用于用例检索与缺陷关联；序号不跨模块连排。", "", "", ""],
  ["", "", "", ""],
  ["二、新增用例方法", "", "", ""],
  ["1. 在对应模块的用例行之后追加新行（建议保留空行便于插入）。", "", "", ""],
  ["2. 用例编号取该模块当前最大序号 + 1；不得复用已删除编号。", "", "", ""],
  ["3. 表格已开启自动筛选、冻结表头、换行显示与下拉验证，新增行会自动继承样式；如样式未自动扩展，用格式刷复制相邻行。", "", "", ""],
  ["4. 填写的关键约束：实际结果必须完整写入单个单元格（自动换行），不拆分到多个格子；结论列使用下拉（通过/失败/阻塞/跳过）；失败用例必须填写缺陷编号。", "", "", ""],
  ["5. 本清单每轮回归执行一次；执行前在说明页记录环境版本与日期。", "", "", ""],
  ["", "", "", ""],
  ["三、执行环境（本轮）", "", "", ""],
  ["后端：python scripts/e2e_up.py（uvicorn :8734）", "", "", ""],
  ["前端：vite dev（:5199），浏览器访问 http://127.0.0.1:5199", "", "", ""],
  ["桌面：npm run tauri dev（需 Rust 工具链）", "", "", ""],
  ["依赖：已配置 main-loop 凭据（真实 Key）；种子数据可选（scripts 下 e2e 脚本）", "", "", ""],
  ["", "", "", ""],
  ["四、优先级定义", "", "", ""],
  ["P0：核心链路（对话、工具、星球导航、审批、打包），每次回归必须全部执行。", "", "", ""],
  ["P1：重要功能，发布前必须执行。", "", "", ""],
  ["P2：边界与增强，按版本节奏选择性执行。", "", "", ""],
];
guide.getRange("A1:D22").values = guideRows;
guide.getRange("A1:D1").format.font = { bold: true, size: 14, color: "#FFFFFF" };
guide.getRange("A1:D1").format.fill = "#1c2c55";
guide.getRange("A1:D1").format.rowHeight = 26;
guide.getRange("A3:A3").format.font = { bold: true, size: 12 };
guide.getRange("A8:A8").format.font = { bold: true, size: 12 };
guide.getRange("A14:A14").format.font = { bold: true, size: 12 };
guide.getRange("A20:A20").format.font = { bold: true, size: 12 };
guide.getRange("A1:D22").format.wrapText = true;
guide.getRange("A1:D22").format.valign = "top";
guide.getRange("B4:B13").format.columnWidth = 100;
guide.getRange("A1:A22").format.columnWidth = 26;
guide.getRange("B1:D22").format.columnWidth = 100;

// ---- 导出 ----
const outDir = "C:/Users/zxy/Documents/Front agent/smart-agent/docs";
await fs.mkdir(outDir, { recursive: true });
const output = await SpreadsheetFile.exportXlsx(wb);
await output.save(`${outDir}/e2e-test-checklist.xlsx`);
console.log("saved", outDir, "rows:", CASES.length);
