# 第八轮状态恢复收尾 · 独立验收装置（子智能体 D）

本目录只放**独立命名的验收脚本与证据**；前端反例在
frontend/src/acceptance-state-recovery/，后端反例在
backend/tests/test_state_recovery_d_acceptance.py。不改任何产品代码。

## 第一步（已完成）：基线红

- evidence/baseline-red-summary.md：命令、逐条失败断言、基线已通过项、分层标注。
- evidence/baseline-b3245e5-frontend-red.txt / baseline-b3245e5-backend-red.txt：原始输出。

## 第二步（进行中）：E1 截图断言矩阵 + E2 真实关闭重开旅程

- E1：截图前先**实际断言**主题（亮/暗）、测试卡片与关系、预定面板状态、目标失败及原因/重试入口；
  重新加载后再次验证主题；同一次页面状态内完成截图与测量；条件不满足即失败。
  矩阵 1440x900 / 1024x768 / 800x600 / 480x600 x 亮/暗 x 正常/双面板共存/保存失败/提交失败。
- E2：草稿冲突 → 选服务器稿 → 本机删除失败 → 重试 → **关闭浏览器进程** → 同一持久目录重开 →
  实际编辑入口恢复；并覆盖 R2/R3/R5 的迟到读取、连续编辑、兜底确认与 F1/F3/N1/N3/N4/N5 的关键决定路径。

脚本与证据将放在本目录下（scripts/state-recovery-verify/**），可复用 scripts/closure-c-verify/cdp.mjs 的 CDP 装置，
但脚本独立命名。所有服务使用独立临时 QIO_DATA_DIR；浏览器实例串行、用完关闭。
