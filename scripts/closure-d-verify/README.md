# closure-D 独立验收装置（R1–R6 反例与分层证据）

这是**独立验收 D** 的装置：冻结六条「正确行为」反例，在基线与候选 SHA 上用同一份探针复跑。
它**不改任何产品代码**，只新增测试与脚本。

## 1. 一条命令复跑

```powershell
# 仓库根目录；会自建独立临时 QIO_DATA_DIR（tmp\run-<时间戳>），不会连到用户真实数据目录
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/closure-d-verify/run-all.ps1 -Phase candidate
# 无 Chrome 时可跳过第⑤/⑥层
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/closure-d-verify/run-all.ps1 -Phase candidate -SkipBrowser
```

退出码 0 = 全部步骤通过。基线（修复前）**预期非 0**：第①②⑤层会红，第③④层应绿（见 `REPORT-phase1.md`）。

前置：`frontend/node_modules` 已装；`backend/.venv` 需有 pytest（`uv sync --frozen --extra dev`，只动 .venv）；
端口 8734 / 5199 必须空闲（脚本会先检查，占用即报错退出，不杀别人的进程）。

## 2. 分层覆盖

| 层 | 文件 | 覆盖 |
| --- | --- | --- |
| ① 状态/单元 | `frontend/src/stores/__tests__/closure-d-r1-r4.test.ts`、`closure-d-r5-r6.test.ts` | R1–R6 的 store 层正确行为（受控替身模拟乱序/失败回执，已标注） |
| ② 真实组件 DOM | `frontend/src/components/interactive/__tests__/closure-d-impact-dialog.test.ts` | R1/R5/R6 的**用户可见结果**（真实 ImpactConfirmDialog + jsdom） |
| ③ API/数据库 | `backend/tests/test_closure_d_acceptance.py` | 真实临时 sqlite + 完整 ASGI 路由：R3 两版落库、R5 门/stale_check 不落库、R6 真暂停 |
| ④ 真实 HTTP | `closure-d-api-journey.py` | 真实前后端进程 + 真实库：20 条检查逐条 PASS/FAIL |
| ⑤ 真浏览器 | `closure-d-browser-probe.mjs` | 真实 Chrome（CDP）：真实点选卡片→完成编辑→影响确认框→取消；截图证据 |
| ⑥ 关闭重开 | 同上（B2 段） | **结束浏览器进程**后端同一用户目录由**新进程**打开，本机数据仍在 |

## 3. 六条反例（正确行为断言）

| # | 一句话 |
| --- | --- |
| R1 | 取消影响确认后，板面必须回到服务器已保存内容（基线：仍显示被取消的新正文） |
| R2 | 迟到的 GET 不许把已成功保存的新正式板面换回旧正文 |
| R3 | 保住第二版正文，也必须保住第一版保存成功的版本事实（第二次 PUT 带服务器最新 seq） |
| R4 | 本机清除依据写失败 + 服务器清除已确认时，重试不许把最新旧稿写成 `kind=cleared` |
| R5 | 服务端兜底确认必须拿到有效 checkId（确认后不许重复同一条说明） |
| R6 | 服务端说明里列出的任务不许被客户端漏掉 |

## 4. 模拟与真实（如实标注）

- **模拟**：①②层的网络回执/失败/乱序都是受控替身（`vi.mock`），不是真实网络；断言的形状与
  `services/interactive.ts` 解出的真实 payload 一致（FastAPI 的 `{detail:{...}}`）。
- **真实**：③层是真实 sqlite 文件 + 真实路由；④层是真实 HTTP + 真实进程；⑤⑥层是真实 Chrome 进程。
- **已知环境现象**（不算缺陷）：验收环境 webfont 经 worktree 外 junction 被 Vite 403 拒绝，
  截图用系统回落字体；探针已按已知现象排除该项请求。

## 5. 上一轮 20 项核心回归的抽检入口（阶段 3 用）

不改这些既有文件，直接按需复跑并记录真实数字：

```powershell
cd frontend
npx vitest run src/stores/__tests__/final-lead-m1.test.ts `
  src/stores/__tests__/final-b2-sendIdentity.test.ts `
  src/interactive/__tests__/final-a2-clear-protection.test.ts `
  src/interactive/__tests__/final-a2-reopen-cleared.test.ts `
  src/interactive/__tests__/final-a2-long-draft-local-copy.test.ts `
  src/components/interactive/__tests__/final-a3-07-draft-clear-binding.test.ts `
  src/interactive/__tests__/final-d-overlay-geometry.test.ts `
  src/interactive/__tests__/final-d-space-reset.test.ts
cd ..\backend
.venv/Scripts/python.exe -m pytest -q tests/test_final_c_impact.py tests/test_final_c_drafts.py `
  tests/test_final_c_dedup.py tests/test_final_c_preview_apply.py tests/test_final_c_approval.py
```

对照重点：草稿冲突、发送归属/互换、清除版本守卫、长稿不截短（20000/20001/20008）、
审批材料失效、关系去重、成组、定位、失焦复位；聊天发送不产生 submissions、保存/恢复不自动发送/提交；
未勾选注释在前后态与删除路径不可见。
