# 独立验收（E）复核记录 — 2026-10-09

工作目录：D:\qio-dev\qio-final（分支 fix/interactive-final-closure）
本次独立验收只创建 `scripts/final-verify-subagent/` 下的探针与证据；**没有修改任何产品代码**，
也**没有执行任何 git 提交**（说明：Lead 在 21:53 的提交 0fa94a4 里把本目录的探针/证据一并提交了，
那不是我执行的动作）。

## 0. 验收期间候选 SHA 发生了移动（重要）

| 时间 | SHA | 事件 |
| --- | --- | --- |
| 21:29 | e7f5802 | 任务给出的候选 SHA（我的 M1/16/17/12 探针就是针对它跑的） |
| 21:48 | ef6198a | [final-Lead] 只改 docs（门禁数字） |
| 21:53 | 0fa94a4 | [final-Lead] 改 `frontend/src/stores/interactive.ts`（+75 行）修复「12 的第二个反例」 |

- e7f5802..0fa94a4 的产品代码差异只在 `frontend/src/stores/interactive.ts`；backend 无差异
  （`git diff --stat e7f5802..HEAD -- backend` 为空）。
- 因此：后端 16 / 17 的结论对两个 SHA 都成立；前端 01 / 12 需要在两个 SHA 上分别看。

## 1. 反例结果

### 17 去重身份（探针 `probe17_dedup.py`，14 项断言）
- 指纹层：A、B 同内容不同身份时 A→C 与 B→C 的 content_fingerprint 不同（65fb8737… vs 5a5fe8f4…）。
- 真提交：A→C 换成 B→C → status=succeeded，表达式含 link_added + link_removed。
- 真正撤回再加回（同 id 结构同正文、无中间提交）→ status=empty，delivered=false，成功提交数与基准行未变。
- 撤回再加回但换 link id（同端点同正文）→ 仍判 empty（重复规则没被废掉）。
- 有序组 [A,B]→[B,A] 来回调整后回到基准 → empty；真正提交顺序变化 → succeeded + order_changed；
  相对新基准再改回也是 succeeded。
- 结论：**成立**（攻击未找到反例）。

### 16 审批材料复核（探针 `probe16_approval.py`，16 项断言）
- 真实 PUT /state 改材料正文（未提交）→ 该预览立刻 needs_update；approve → ok=false / reason=needs_update，
  细节说明「材料发生了变化」。
- 绕过保存钩子**直接写库**（跳过保存时标记，仍是 pending）→ approve 仍被拒（证明审批时确实重新核算）。
- 只改 x/y/w/h（真实接口 + 直接写库两条路径）→ 不失效，approve ok=true。
- 材料被标记删除 → approve 被拒。
- 结论：**成立**。

### 01 / M1 草稿乱序（探针 `probe-01-m1.probe.test.ts`，2 项）
- 尖锐构造：GET 载荷**带该键旧正文**、GET 开始后无新输入，唯一保命条件是
  `savedNow > readSaved`（GET 在飞期间草稿 PUT 成功）。
- 结果：乱序 GET 返回后 drafts 仍是新稿、状态 saved。
- 变异验证：把 store 的该条件改成 `false` 后，尖锐用例变红（收到服务器旧正文），
  对照组（memoryIsNewer 路径）仍绿；随后 `git checkout --` 还原，工作区干净。
- 结论：**成立**（用例确实在测这件事）。

### 12 清除事实（探针 `probe-12-clear.probe.test.ts`、`probe-12b-version-guard.probe.test.ts`）
- 在 **e7f5802**（原候选）：
  - 本机 v1 存在 → 存储写满 → 新稿与 cleared 都写失败 → 服务器清除成功（PUT 不含该键）→ 点重试
    （CardDraftHint 真实路径 `retryDraftSave(key)`）→ 磁盘仍是 kind=draft 的 v1 → 重开 store 后
    `drafts["card:c1"]="旧稿-v1"`。**旧稿复活，条目 12 被证伪。**
  - 根因：`clearDraft` 在本机 cleared 写失败时把键从 pendingRemovals 删除（旧代码 900-905 行），
    而 retry 只在 `pendingRemovals.has(key) && removalState==="error"` 时才补写 cleared —— 该分支不可达。
- 在 **0fa94a4**（Lead 的修复）：
  - probe-12 原反例已修：重试后磁盘为 cleared、重开无旧稿 → 原反例关闭。
  - **但 probe-12b 找到新反例**：本页面清除写失败后，另一个页面写了更新的一版草稿（v2）；
    本页面重试 → 观测 `{"version":null,"kind":null,"text":null}`：
    更新记录先被 cleared 覆盖、随后被网络确认流程删除，**另一页面的新输入被销毁**。
    这违反契约 M2「不得为清除旧稿误删后来输入的更新版本」与 §12.2 的版本守卫。
  - 根因：新代码在 retry（1270 行）与 flush 确认路径用 `ensureCardLocalClear` 补写，
    它只判断「磁盘上是不是 cleared」而**不校验记录版本**（原 `removeCardLocalDraft(cardId, expectVersion)`
    的版本守卫被绕过）。
- 结论：**在 e7f5802 被证伪；在 0fa94a4 原反例已修但仍有新反例（多页面版本守卫缺口）→ 12 仍不能算完成。**

## 2. 门禁实跑（本轮实际输出）

| 检查 | e7f5802 | 0fa94a4（当前 tip） |
| --- | --- | --- |
| `cd frontend; npx vue-tsc --noEmit` | exit 0，无输出 | exit 0，无输出 |
| `npx vitest run` | 168 文件 / 1549 用例 全通过（183s） | 168 文件 / 1549 用例 全通过（107s） |
| `cd backend; .venv\Scripts\python.exe -m pytest -q` | exit 0（与 tip 相同代码） | exit 0；`--co` 收集 2158 条 |
| `python scripts/check_docs.py` | exit 0（34 个里程碑条目） | exit 0（34 个里程碑条目） |

说明：pytest 因 pyproject 的 `addopts=-q` 叠加命令行 `-q` 变成 `-qq`，不打印最终计数行；
用 `-o addopts="" --co -q` 得到收集数 2158，全量运行退出码 0。
证据文件：`evidence/gate-*.txt`。

## 3. QIO_DATA_DIR 隔离
- 起任何服务/脚本前显式 `$env:QIO_DATA_DIR=<临时目录>`；探针脚本内部也再次覆盖。
- 只跑了 pytest/vitest，没有起真实服务；未触碰 D:\QIO-data。

## 4. 证据文件
- `probe-01-m1.probe.test.ts`、`probe-12-clear.probe.test.ts`、`probe-12b-version-guard.probe.test.ts`
- `vitest.probe.config.mjs`（root=frontend，绝对 include，未改 frontend 任何文件）
- `probe16_approval.py`、`probe17_dedup.py`
- `evidence/gate-vue-tsc.txt`、`gate-vitest-full.txt`、`gate-pytest-full.txt`、`gate-pytest-full2.txt`、
  `gate-check-docs.txt`、`gate-vue-tsc-0fa94a4.txt`、`gate-vitest-0fa94a4.txt`
