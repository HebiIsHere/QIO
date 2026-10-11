# closure-D 独立验收 · 阶段 1：基线失败证据（冻结反例）

- 角色：独立验收 D（不参与任何开发、不改产品代码）
- worktree：`qio-cl-d`　分支：`wt/closure-d-accept`
- 基线 SHA：`1da2172c6876c98f9b1076b4477dc75e7fe37638`
- 一句结论：**R1–R6 六条反例在基线上逐条失败**，失败都是真实断言失败（没有跳过断言、没有弱化断言）；
  服务端协议层（第③/④层）在基线上是绿的，说明这些缺陷在**前端丢掉服务端事实**这一侧，而不是服务端没做。

## 1. 反例冻结清单（触发 → 应有结果 → 基线实测 → 证据层）

| # | 触发步骤 | 应有结果 | 基线实测（真实失败原文） | 证据层 |
| --- | --- | --- | --- | --- |
| R1 | 改卡片正文→服务端门 409 要求影响确认→点确认框「取消」 | 板面回到**服务器已保存**内容；取消不删草稿候选；dirty 如实 | 会话层：`expected [ '被取消掉的新正文' ] to equal [ '服务器原文' ]`；真实浏览器（第⑤层）：取消后界面仍显示 `浏览器里改成的正文-R1`，而服务器仍是 `材料一` | ①状态 + ②DOM + ⑤真浏览器 |
| R2 | 保存先成功（seq 前进），更早发起的 GET 后返回旧正文 | 已保存成功的新正式板面不被迟到的旧读取回退 | `expected [ '旧正文' ] to equal [ '已保存成功的新正文' ]` | ①状态 |
| R3 | 第一版保存回执在飞时出现第二版；第一版回执带着新 seq 落地 | 第二版仍必须能继续保存（下一次 PUT 携带服务器最新 seq） | `expected 3 to be 4`（第二次 PUT 的 state.seq 仍是旧值；真实服务端会按旧基准判定） | ①状态 + ④真实 HTTP（服务端对照） |
| R4 | 本机 cleared 依据写失败 → 服务器清除已确认 → 点重试 | 不许把最新旧稿写成 `kind=cleared`；旧稿在有真实编辑内容时不被静默清除 | `expected 'cleared' to be 'draft'`（重试后磁盘记录变成 cleared） | ①状态 |
| R5 | 服务端门 409 兜底（客户端没预料到）→ 用户点「继续」 | 必须先重新预判拿有效 checkId，确认时带上；之后保存成功、同一条说明不再出现 | `expected undefined to be 'chk_fresh'`（第二次 PUT 的 confirm 为空）；真实浏览器：确认框仍在 | ①状态 + ②DOM |
| R6 | 服务端说明里列出 A 与 B，客户端只知道 A | 待确认说明必须列出**服务端列出的全部**任务（漏掉 B = 用户看不到 B 会被暂停） | `expected [ 'A' ] to equal [ 'A', 'B' ]`；DOM 层 `expected 1 to be 2` | ①状态 + ②DOM |

## 2. 实跑命令与真实输出数字

```
# 第①层（状态/单元）+ 第②层（真实组件 DOM）：基线
cd frontend
npx vitest run src/stores/__tests__/closure-d-r1-r4.test.ts \
                 src/stores/__tests__/closure-d-r5-r6.test.ts \
                 src/components/interactive/__tests__/closure-d-impact-dialog.test.ts
# => Test Files 3 failed (3) | Tests 9 failed (9)
#    逐条失败原文见 scripts/closure-d-verify/evidence/phase1-all.json
```

```
# 第③层（真实临时 sqlite + 完整 ASGI 路由）
cd backend
.venv/Scripts/python.exe -m pytest -q tests/test_closure_d_acceptance.py
# => 3 passed（基线上服务端行为就是正确的：R3 两版落库、R5 门/stale_check 不落库、R6 受影响任务暂停且保留进度）
```

```
# 第④层（真实前后端进程 + 真实 HTTP + 真实库）
# 服务必须先用独立临时 QIO_DATA_DIR 启动（见下）
backend/.venv/Scripts/python.exe scripts/closure-d-verify/closure-d-api-journey.py \
    --base http://127.0.0.1:8734 --data-dir <临时目录>
# => 20/20 通过（含 R3.1–R3.5 两版落库回读、R5.1–R5.8 门与 stale_check 不落库、R6.3/R6.4 真暂停）
```

```
# 第⑤层（真浏览器 CDP）+ 第⑥层（真实进程关闭重开）
# 前端 dev server 5199 + 后端 8734（独立临时 QIO_DATA_DIR）都起好后：
node scripts/closure-d-verify/closure-d-browser-probe.mjs \
  --app http://127.0.0.1:5199 --api http://127.0.0.1:8734 \
  --profile <临时 Chrome 用户目录> --out scripts/closure-d-verify/evidence
# => 15/16 通过，唯一 FAIL 就是 R1（B1.5：取消后界面仍显示被取消掉的正文）
#    B2.1 真实关闭浏览器进程 + 同一用户目录重开：本机数据仍在
```

## 3. 环境与安全（逐条核对）

- 本次所有服务都用**独立临时 QIO_DATA_DIR**：`scripts/closure-d-verify/tmp/phase1-http-data`；
  第④层脚本第一步就断言 `<data-dir>/app.db` 存在并直接读该文件核对落库，**不会连到用户真实数据目录**。
- 模型一律未使用（本轮不涉及模型调用）；没有真实 Key、没有联网依赖。
- 依赖：frontend/node_modules 为既有 junction；backend/.venv 原本缺 pytest，
  只在本 worktree 内执行了 `uv sync --frozen --extra dev`（只动 gitignore 的 .venv，未改受版本管理的文件）。
- 已知环境现象（如实标注，不算本轮缺陷）：验收环境 webfont 走 worktree 外 junction，
  Vite 对 `/@fs/**@fontsource**` 返回 403 → 截图使用系统回落字体，几何不受影响；浏览器探针已按已知现象排除该项。

## 4. 逐条基线证据文件

| 文件 | 内容 |
| --- | --- |
| `evidence/phase1-all.json` | ①②层 9 条用例的机器可读结果（含失败原文与行号） |
| `evidence/phase1-store.json` | 第①层 6 条（R1–R6）单独结果 |
| `evidence/phase1-backend.txt` | 第③层 pytest 输出 |
| `evidence/phase1-http.txt` | 第④层真实 HTTP 旅程逐条 PASS/FAIL |
| `evidence/phase1-browser.txt` | 第⑤/⑥层真浏览器逐条 PASS/FAIL |
| `evidence/closure-d-b0-app.png` | 真实应用在真浏览器里的截图（板面 + 卡片） |
| `evidence/closure-d-b1-impact-dialog.png` | 真实影响确认框出现时的截图（R1 触发点） |
| `evidence/closure-d-b2-reopened.png` | 关闭浏览器进程后同一用户目录重开的截图 |

## 5. 未完成 / 未验证（如实）

- **六条反例的修复尚未验证**：本轮只冻结并对基线取证；候选 SHA 固定后需在同一份探针上复跑（阶段 3）。
- 第⑤层尚未覆盖：R2（迟到读取）、R3（第二次保存）、R4（重试）、R5（确认后说明消失）的**真浏览器**端到端操作；
  这几条目前由①②④层覆盖（真浏览器目前覆盖 R1 与「关闭重开」）。
- 第⑥层目前验证的是「真实浏览器进程关闭重开 → 本机数据仍在」（同一用户目录、新进程）。
  针对 R4 的「重开后本机记录不复活」目前是第①层（jsdom 存储）证据，**不是**真实进程关闭重开证据。
- 上一轮 20 项核心回归未逐条重跑（阶段 3 抽检计划见 `README.md`）。
