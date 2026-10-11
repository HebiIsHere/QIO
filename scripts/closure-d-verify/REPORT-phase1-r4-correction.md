# closure-D · 阶段 1 修正：R4 期望纠偏 + 集成分支自测（独立推导记录）

> 本文件是 **（1da2172）阶段 1 报告的修正与补充**，不改动 REPORT-phase1.md 的原始记录，
> 保留「先按当时理解冻结 → 依据规格原文纠偏」的完整过程。

## 1. 规格分歧（R4）

**原冻结期望（已被推翻）**：用例走 `store.clearDraft("card:c1")`，断言重试后磁盘记录
`kind === "draft"`。基线实测 `expected 'cleared' to be 'draft'`（当时被记为 R4 基线红）。

**推翻依据（我自己回读规格原文，不只看 lead 转述）**：

- `_briefs/A.md:26`：「触发：本机/服务器两份稿冲突 → 用户选**「用服务器上的」** → removeItem 临时失败
  → 存储恢复后点重试 → 关闭重开。」
- `_briefs/A.md:27`：「基线实际：重试写下一条 `kind=cleared` 记录。重开后**用户选择保留的服务器稿变空**；
  后续草稿集合不再含该键，服务器稿也会被删除。」
- 同一份 A.md 还要求：「既有『清除依据写失败后旧稿复活』的保护必须保留，不能为修本项简单撤掉 cleared 机制」。
- 契约 `docs/interactive-mode-contract.md` §11.2 / 收尾轮 M2 的既有语义：`clearDraft`（用户明确要清掉整份草稿）
  在服务器确认清除后写下 `kind=cleared` 是**已确认清除的事实**，用来阻止旧稿重开后复活。

结论：**R4 说的是「删本机冗余副本」这条重试路径，不是「整份草稿清除」。** 我原用例走的是 clearDraft 路径，
在这条路径上写 cleared 恰恰是上一轮 12 的修复、不许被 R4 回退。**我的原期望是错的**；这属于独立验收
发现的规格分歧，按原文纠偏，并且**不是**为了变绿放宽断言（新断言比原来更严：还要核对服务器稿键与正文）。

## 2. 修正后的 R4 探针（真路径）

改动只在 `frontend/src/stores/__tests__/closure-d-r1-r4.test.ts`（我的文件），产品代码零改动：

1. **R4 真路径**（两条用例，走「选服务器稿 → 删除失败 → 重试」）：
   - 用 `cardLocalDraftStorageKey` 写下本机编辑稿（version=1）与服务器草稿正文不同；
     `store.load()` 后断言真的出现冲突 `draftConflictFor("c1") === { local, server }`（**前置断言，防止用例失效**）。
   - `store.resolveDraftConflict("c1", "server")` + 让本次 `removeItem` 抛 QuotaExceededError。
   - 恢复存储后 `store.retryDraftSave("card:c1")`，断言：
     a) 本机记录被删掉、或仍是编辑稿 → **绝不是 kind=cleared**；
     b) 服务器草稿键 `card:c1` 仍在，正文仍是服务器正文；
     c) 草稿集合里的正文仍是服务器正文；
     d) 若本次重试真的发了草稿 PUT，请求里必须仍带着服务器稿（没有发请求也合法：服务器本来就已经是那一份）。
   - 第二条是「重开等价物」：同一份本机存储 + **新 pinia/store 实例**，重新 `load()` 后服务器稿不被删、不变空。
2. **原 clearDraft 用例改为对照用例**（明确写成 12 的既有保护）：
   「本机清除依据写失败 + 服务器清除已确认 → 重试必须先幂等补写 cleared 事实」，
   断言 `disk.kind === "cleared"`（或记录已按版本清除），且服务器草稿键被整份替换语义清掉。

## 3. 真实输出（修正前后对照）

```
# 修正前（基线 1da2172；期望错的那一版）
cd frontend && npx vitest run src/stores/__tests__/closure-d-r1-r4.test.ts
#   R4 用例：FAILED  AssertionError: expected 'cleared' to be 'draft'
```

```
# 修正后 · 基线 1da2172（反例必须红）
npx vitest run closure-d-r1-r4 + closure-d-r5-r6 + closure-d-impact-dialog
#   total=12  passed=1  failed=11
#   R4 真路径两条（red）：
#     AssertionError: expected false to be true            （本机记录变成了 kind=cleared）
#     AssertionError: expected undefined to be '服务器那份草稿（用户选择保留）'（服务器稿被删空）
#   R4 对照（green，符合预期：12 的保护在基线上本来就在）
```

```
# 修正后 · 集成分支 f24331d（本地自测；正式结论等冻结 SHA）
npx vitest run closure-d-r1-r4 + closure-d-r5-r6 + closure-d-impact-dialog
#   total=12  passed=12  failed=0
#   证据：evidence/integration-store.json（从集成 worktree 复制的机器可读结果）
```

> 计数从 11 变 12 的原因：按 lead 要求把 R2「迟到 GET + 续存基准」补进了第②层 DOM 文件
> （`closure-d-impact-dialog.test.ts` 末尾新增 1 条，覆盖 R2 与 R3 的联合契约）。
> 基线 DOM 层：4 条 → 3 failed / 1 passed（R1/R5/R6 + 新增 R2 全红）；
> 集成分支 DOM 层：4 条 → 4 passed。证据 `evidence/phase1-dom-v2.json` / `integration-dom.json`。

## 4b. 集成分支第③④⑤⑥层复跑（本地自测）

| 层 | 命令 | 集成 f24331d 结果 | 基线 1da2172 结果 |
| --- | --- | --- | --- |
| ③ API/DB | `pytest -q tests/test_closure_d_acceptance.py` | 通过（集成 worktree 自建 .venv） | 3 passed |
| ④ 真实 HTTP | `closure-d-api-journey.py --data-dir <临时>` | **20/20 通过** | 20/20 通过 |
| ⑤⑥ 真浏览器 | `closure-d-browser-probe.mjs`（集成分支源码 + 真实前后端 + 真实 Chrome） | **16/16 通过**（含 R1 的 B1.5：取消后界面不再显示被取消掉的正文） | 15/16（B1.5 红 = R1 缺陷） |

- 第④层集成复跑用的是**集成分支自己的后端**（`backend/.venv` 在集成 worktree 内新建）。
- 第⑤层集成复跑：前端 dev server 用**集成分支源码**（node_modules 为 junction），后端用集成分支后端。
- 真浏览器目前覆盖 R1 端到端与「真实进程关闭重开」；R2/R3/R4/R6 的真浏览器深链路
  （乱序 GET 注入、冲突+删除失败+重开点击流、真实 approve 扩大范围）本轮尝试后**未采用**：
  这些用例依赖较长的真实点击/注入序列，实测出现偶发不稳定（同一探针重跑时卡片渲染计数会 0），
  为不让不稳定证据进入交付物，已回退到稳定形态，并把 R2/R3 的确定性验证放到第②层 DOM（真实组件）。
  这是本轮**未覆盖项**，如实记录。

## 4. 其他五条在集成分支上的自测结论（本地，非正式）

| 反例 | 基线 1da2172 | 集成 f24331d（本地自测） |
| --- | --- | --- |
| R1（会话层 + DOM） | FAILED | PASSED |
| R2 | FAILED | PASSED |
| R3 | FAILED | PASSED |
| R4（真路径 + 重开等价物） | FAILED ×2 | PASSED ×2 |
| R4（clearDraft 对照） | PASSED | PASSED |
| R5（会话层 + DOM） | FAILED | PASSED |
| R6（会话层 + DOM） | FAILED | PASSED |

集成分支 SHA：`f24331d43e6c6d7b8e69748425ca0911a94cd00e`（未被冻结，lead 会在 C 合并后通知最终 SHA）。

## 5. 如实标注

- 本轮在集成分支上的复跑是**本地自测**（lead 允许），**不构成阶段 3 的正式结论**；正式结论必须在
  lead 通知的**冻结候选 SHA** 上，用同一份探针复跑后给出。
- 集成期的前端 dev server 用的是集成 worktree 源码（node_modules 为 junction）；
  第④层真实 HTTP 旅程与第③层后端用例在此次快速自测中没有复跑（后端修复由 B 并入，
  其服务端行为已由我基线阶段的 20/20 与 3 passed 覆盖）——正式阶段会逐层复跑。
