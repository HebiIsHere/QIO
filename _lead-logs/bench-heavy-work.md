# WS3 §3 后端耗时计算实测（`_lead-logs/bench-heavy-work.md`）

任务：`task-9`（契约 `_JANK-RACE-CONTRACT.md` WS3）。测量目标：把「启动模型加载 /
缺失向量补算 / 话题预测 / 记忆检索 / 索引更新 / 后台整理」各自的耗时，以及**期间**
健康探测、取消请求、事件流的响应情况测出来；先测量、再按实际调用链把同步重活移出
事件循环。

## 0. 环境与命令原文

- 机器：本机 Windows，`os.cpu_count() = 16`；ONNX Runtime CPU（`CPUExecutionProvider`），
  模型是仓库里真实的那一份：`frontend/src-tauri/resources/models/bge-small-zh-v1.5`
  （fp32，身份 `onnx:bge-small-zh-v1.5:fp32:69a0b846f4f1`）。
- 沙箱注记：本会话里 `uv run` 的缓存在沙箱外不可写，pytest 的临时目录要放在仓库外
  （否则 0o700 目录连创建者都读不了）。因此命令用 venv 里的解释器直接跑：

```powershell
$shim = "C:\Users\zxy\Documents\Front agent\.dsh-pytest-shim"
$env:PYTHONPATH = $shim            # 垫片：只放宽 pytest 临时目录的 mkdir 权限位
$env:TEMP = "$shim\tmp"; $env:TMP = $env:TEMP
$env:QIO_BENCH = "1"               # 打开真实测量（默认跳过，回归用例不受影响）
cd "C:\Users\zxy\Documents\Front agent\qio-wt-fixes\backend"
.\.venv\Scripts\python.exe -m pytest tests/test_interactive_during_heavy_work.py -q -s -k bench
```

测量脚本与用例都在 `backend/tests/test_interactive_during_heavy_work.py`（`QIO_BENCH=1`
时才跑；`-k bench` 选中它们）。数据规模：**8 个话题 + 24 条真实记忆索引行**（封块 →
摘要 → `memory_index` → 向量，全部走真实链路）。

## 1. 各重活耗时（真实模型 + 真实数据库）

`wall` = 该操作自身耗时；`loop_gap` = 同一操作**跑在事件循环上**时，事件循环心跳测到的
最大停顿（= 期间真实 HTTP 请求要等的时间）。

| 操作 | 修复前 wall | 修复前 loop_gap | 修复后（事件循环路径） |
|---|---|---|---|
| 启动模型加载（`AppContext` 构造） | 394–472 ms | 同值（启动期） | 不变（启动期一次性，见 §5） |
| 话题预测 · 冷启动（8 个话题向量缺失） | 41–79 ms | **41.8–79.1 ms** | 推理进执行器；见 §3 |
| 话题预测 · 热（向量都在） | 3.1 ms | 3.2 ms | 0.6–0.8 ms（含嵌入复用） |
| 记忆检索（`build_injection`，24 条索引） | 2.1–8.3 ms | 2.1–8.3 ms | 同左（搬进执行器后不占循环） |
| 索引更新 · 单条增量（`_upsert_selector`） | 2.2–8.2 ms | 2.3–8.3 ms | 同左（未搬，见 §5） |
| 索引更新 · 全量重建（`_refresh_selector`） | 0.4 ms | 0.5 ms | 同左（启动期调用） |
| 后台整理（`maintenance.run_once`） | **460.2 / 478.4 ms**（冷会话一次 4618.9） | **463.7 / 481.8 ms**（冷会话一次 4628.5） | **已修（task-12）：wall 481.7 ms，loop_gap 17.3 ms** |

冷/热两档差得很开：**冷启动那一次是几十毫秒级的同步阻塞**，热路径只有毫秒级。

> 勘误（验证方复跑发现，已修）：本节「后台整理」原写作 `0.0 ms`，是 bench 工具缺陷 ——
> 那一行把**协程函数** `MaintenanceScheduler.run_once` 传给了只做同步调用的
> `_measure_sync_on_loop`，于是只创建了协程对象、**维护根本没执行**（并留下
> `RuntimeWarning: coroutine ... was never awaited`）。修法：新增
> `_measure_async_on_loop`（真的 `await` 一轮，与生产里 `_tick` 的调用方式一致），并在
> `_measure_sync_on_loop` 里加防呆断言（拿到协程就报错，不再把「创建协程的耗时」当结果）。
> 修正后的真实数字见上表与 §5.1。

## 2. 真实 uvicorn + 真实 HTTP：慢重活期间的响应情况

服务端用真实 `create_app` + uvicorn（`127.0.0.1:8731`），重活 = 6 次冷启动话题预测
（≈230 ms 窗口，故意拉宽以便三个探针都落在窗口内）。三种请求**并发**发出，测端到端
耗时（ms）：

| 请求 | 基线 | 重活跑在事件循环上（修复前） | 重活搬到执行器（修复后） |
|---|---|---|---|
| `GET /api/health` | 3–32 ms | **228.7 ms** | **6.7 ms** |
| `POST /api/turns/cancel` | 3–15 ms | **228.0 ms** | **5.0 ms** |
| `GET /api/events`（连接建立/响应头） | 2–5 ms | **227.9 ms** | **5.0 ms** |

（基线一列的前几个请求含首次连接开销，稳定值 3–10 ms；同一次运行内比较才有效。）

结论：修复前，一次几十到几百毫秒的同步重活会把**健康探测、取消、事件流全部按在门外**
（用户看到的就是「点了停止没反应、界面没响应」）；搬到有上限的执行器之后，三者都在
个位数毫秒内返回。

## 3. 残余停顿：为什么不是 0

执行器只能搬走**不在事件循环上**的那部分工作；Python 线程之间还有 GIL，ONNX Runtime /
tokenizers / numpy 里不释放 GIL 的片段仍会短暂占住解释器。单独探针实测（9 条文本一批）：

| 场景 | wall | 事件循环停顿 |
|---|---|---|
| `embed_texts(1 条)`（热） | 0.3–0.4 ms | 0.4–0.5 ms |
| `embed_texts(9 条)` 在**线程**里 | 16.8 ms | **11.8 ms**（GIL 片段） |
| 冷启动话题预测（三段） | 20–60 ms | 修复后 21–35 ms |

所以修复后的诚实说法是：**把阻塞从「整个推理时长」降到「推理里不释放 GIL 的那一小段」**
（实测冷启动 79 ms → 21–35 ms；热路径 3 ms → ~1 ms；真实 HTTP 探针 228 ms → 5–7 ms），
而不是「完全不阻塞」。要继续降只能换进程池（IPC 成本 + 共享连接不可传），本轮不做。

## 4. 修复内容（按实际调用链）

1. **`agent/services/heavy.py`（新增）**：有并发上限的执行器（`ThreadPoolExecutor`，
   默认 2 个线程）+ `run()` / `shutdown()`。约定：只搬**纯计算**，输入读取与结果提交
   有明确归属；**不**把 `AppContext` / predictor / selector / 整套装配塞进线程。
2. **`services/predict.py`**：话题预判拆成三段 —— `plan_prediction`（只读数据库，事件
   循环一侧）/ `compute_embedding`（纯 ONNX 推理）/ `finish_prediction`（排序 + 冷启动
   向量写回，`commit=False` 时不写）。`predict()` 保留为同步入口（语义不变）。
3. **`services/turn_orchestrator.py`**：`build_context` 的预判与检索都经执行器；检索
   （`build_injection`，只读、无事务）整段搬走；**取消检查在提交之前**（`ctx.cancelled`
   → 不写回、不发切换建议、不把检索结果写进本轮上下文）。取消检查点补
   `bindings.mark_status(..., "cancelled")`（装配期间取消也要记终态）。
4. **`services/app.py`**：`AppContext.heavy` + 关闭时 `shutdown`；`refresh_topic_vector_offloaded`
   （封块后刷新话题向量：嵌入进线程、写回留在循环、取消后不写回）。
5. **`selector/onnx.py`**：嵌入复用缓存（键 =「输入 + 模型身份」，上限 256 条，换模型 /
   换精度不命中）；向量矩阵与向量集合加一把**只保护缓存快照**的锁（重活进线程后
   search 与 upsert 真的会并发，不加锁会出现矩阵/keys 错位或脏标记被覆盖）；
   `topic_vectors()` 批量读、`save_topic_vector(s)` 写已算好的向量（不再重新推理）。

## 5. 没搬的东西（有数字支撑）

- **索引更新（`_upsert_selector`）**：单条 2–8 ms，不值得为它把 `memory_lifecycle`
  的同步回调改成 async（跨模块改动 + 提交顺序风险）；全量重建 0.4 ms 且只在启动跑。
- **启动模型加载 394–472 ms**：发生在 `AppContext` 构造期（uvicorn 起来之前），
  不阻塞任何交互请求；如实记录，不做改动。
- **记忆检索**：整段搬进执行器（不是逐段拆），因为这条路径**只读**（context/retrieval/
  selector/bm25 里没有 INSERT/UPDATE，静态核查过），没有提交顺序问题。

### 5.1 后台整理：从「整段占住事件循环」到「只占 17ms」（task-12 已修）

**修正测量后新暴露的阻塞源**：`await MaintenanceScheduler.run_once()` 实测 **460.2 / 478.4 ms**，
期间事件循环停顿 **463.7 / 481.8 ms** —— 整段占住事件循环。`_tick` 只在
`ctx.turns.active is None` 时调用它，所以它不抢正在跑的一轮，但会在**空闲期**把健康探测 /
取消 / 事件流 / 新一轮开头一起按住约 0.5 s。

**子步骤归属**（每个子步骤用干净的 ctx 单独量；同一进程内 apples-to-apples）：

| 子步骤 | wall | loop_gap | 处置 |
|---|---|---|---|
| `prune_tool_outputs` | 0.0–0.1 ms | 0.2–0.3 ms | 留在循环侧（本身就是毫秒级 DB 写 = 提交动作） |
| `prune_tool_records` | 0.0 ms | 0.1 ms | 同上 |
| `scan_contradictions` | 0.5–2.3 ms | 0.7–2.4 ms | 拆开：读库 / 纯计算（词元命中）/ 提交 |
| `run_dreaming` | 0.0–0.1 ms | 0.1–0.3 ms | 无可搬的纯计算（读库+拼 prompt 毫秒级；模型调用是 `await`，本来就不阻塞，也不能进线程） |
| **`mine_tool_candidates`** | **524.5–571.4 ms** | **16.6–21.4 ms（修复后）** | **拆开：聚类整段进执行器** |
| 一整轮 `run_once` | **481.7 ms** | **17.3 ms** | 见下 |

**同一段聚类的对照**（240 条消息，同一进程、先清嵌入缓存，唯一变量是执行位置）：

| 聚类 `cluster_texts` | wall | loop_gap |
|---|---|---|
| 跑在事件循环上（修复前的样子） | 503.2 ms | **503.4 ms** |
| 搬到执行器（修复后） | 520.9 ms | **17.1 ms** |

也就是：**工作量没变（wall 基本不变），阻塞从 503 ms 降到 17 ms**，剩下的 17 ms 是执行器
搬不走的 GIL 片段（同 §3）。

**真实 uvicorn + 真实 HTTP**（维护整轮 561.0 ms，三个探针并发打）：

| 请求 | 维护期间 |
|---|---|
| `GET /api/health` | **11.6 ms** |
| `POST /api/turns/cancel` | **7.7 ms** |
| `GET /api/events`（连接建立） | **6.0 ms** |

**修法**（`services/maintenance.py`，语义逐字保留）：

- `plan_contradiction_scan` / `find_contradiction_hits` / `commit_contradiction_scan`
- `plan_tool_candidates` / `cluster_texts` / `commit_tool_candidates`
- `mine_tool_candidates` / `scan_contradictions` 保留原签名，内部按「读取 → 执行器纯计算 →
  循环侧提交」组合；`cluster_texts` 返回**下标**，提交侧再取原文，保证聚类顺序、
  `representative = max(texts, key=len)`、审批载荷与旧实现逐字一致。
- **提交前校验代次**：代次 = 该步开始时 `ctx.turns.active` 的引用；计算期间用户开始了
  新一轮（引用变了）就丢弃这次结果 —— 不写库、不落审批、也不再花一次模型调用。
  清理范围 / 判定阈值 / dreaming 条件 / `_running` 单飞 / 「只在无主 turn 时跑」全部未动。
- 模型调用（`await`）始终留在循环侧，没有进线程。

**没搬走的**：`prune_*`（毫秒级 DB 写）与 `run_dreaming` 的模型调用（`await`，本来就不占
循环）。维护一轮的 wall 仍然是 ~0.5 s（**工作量没变**），这是后台任务该有的代价；变的是
它不再把这 0.5 s 压在事件循环上。

## 6. 测不出来的部分（NOT RUN / NOT VERIFIED）

- **后台整理的 LLM 步骤**：本机没有可用的模型凭据，`dreaming_candidates: 0` /
  `tool_candidates: 1` 说明模型驱动的那部分没有真正产出；**整理里 LLM 步骤的耗时 NOT MEASURED**
  （非 LLM 部分已逐步测到，见 §5.1）。
- **模型加载对「首个交互请求」的影响**：加载发生在应用构造期，本机无法构造「服务已
  就绪但模型仍在加载」的窗口。**NOT RUN**。
- **真实多实例并发共享数据目录**：见 §7，**NOT REPRODUCED**（本机不启动第二个安装实例）。
- **掉电 / 强杀下的原子发布**：不适用（本任务不改模型同步实现）。

## 7. §7 跨进程共享资源核查（只读核查，未复现）

### A. 模型文件同步 + `.ready`（`frontend/src-tauri/src/main.rs:110–154`）

静态证据：

- 目标目录 = 用户数据目录 `data_dir()/models/bge-small-zh-v1.5`（`main.rs:196–197`），
  **多实例共享同一数据目录时是同一个目录**。
- 复制是 `std::fs::copy(source, target.join(name))` **直接写目标文件**（`main.rs:142`），
  **没有**「临时文件 + 原子发布」；`.ready` 用 `std::fs::write` 直接写（`main.rs:151`），
  也没有临时文件 + rename。
- 跳过条件 = `.ready` 内容等于指纹 **且** 清单里每个文件 `exists()`（`main.rs:126–133`）——
  只查**存在**，不查大小/哈希。
- 读侧：`selector/onnx.py:91` 只判 `path.exists()`，加载失败（截断/损坏）→ 会话为空 →
  `available()=False` → 静默退回 BM25。

风险（**未复现**，需要条件）：两个实例（或一个实例正在复制、另一个刚启动）共享数据目录时，
并发 `std::fs::copy` 会同时写同一个目标文件（没有锁）；先完成的一方写 `.ready` 之后，
另一方仍在复制 —— 下一次启动会因为「`.ready` 匹配 + 文件都存在」而**跳过复制**，于是可能
长期使用一个内容不完整的模型文件（表现为语义检索静默失效）。另一个更窄的洞：文件被删后
重复制途中被强杀，会留下「`.ready` 匹配 + 文件存在但内容不完整」的状态，同样跳过。

复现所需条件：同一数据目录下两个外壳实例**同时首次启动**（或启动瞬间删掉目标文件让两边
都进复制路径），且复制过程足够长（90 MB 模型文件，本机可复现窗口）。本机没有安装态外壳，
**NOT REPRODUCED**；修复属于外壳侧（WS2），不在本任务写范围。

### B. 同一安装目录重复启动的所有权记录（`ownership.rs` / `main.rs`）

静态证据：

- lease 路径固定为 `<install_dir>\sidecar.lease.json`（`ownership.rs:167`），**同一安装目录
  只有一个文件、不按进程区分**；写入是「临时文件 + `std::fs::rename`」原子替换
  （`ownership.rs:205–226`，临时名带 pid，两个进程的临时文件不会互相覆盖），但**最终路径
  相同 → 后写覆盖先写**。
- 退出路径按自己记下的路径删 lease（`main.rs:1114–1119`）：**先启动的实例退出时会删掉
  后启动实例的记录**，后启动实例从此在卸载器眼里「没有所有权记录」。
- 卸载/关闭按 lease 里记录的 pid 收进程（`ownership.rs::close_installation`），只会看到
  最新那一份记录 → 更早实例的后端不在它的清单里。

判断：这是**明确冲突**（同一安装目录的第二个实例与第一个实例互相覆盖/删除记录），不是
「合法多实例互不阻断」。按契约「沿用既有实例语义、不擅自改成全局单实例」，修复应放在
外壳侧（WS2）：例如 lease 里保留多个实例（列表/按 pid 命名）或写入前检查既有 lease 是否
指向存活进程。**本机未复现**（没有安装态外壳、debug 构建根本不写 lease，见 `main.rs:908–912`），
**NOT REPRODUCED + 静态证据**。

## 8. 「修复前会红」证据（受控回归）

`backend/tests/test_interactive_during_heavy_work.py` 的默认用例（慢嵌入 / 慢召回替身，
不依赖真实模型）在**未应用本次实现**的代码上跑（把 `services/`、`selector/` 的改动
`git stash` 掉、保留测试文件）：

| 用例 | 修复前 | 修复后 |
|---|---|---|
| 慢话题预判期间事件循环停顿 | **275 ms**（断言 <100 红） | ~10 ms |
| 慢召回期间事件循环停顿 | **446 ms**（断言 <250 红） | ~150 ms（整轮；检索段本身 <10 ms） |
| 取消请求被慢推理挡住 | 红（回调等推理跑完才执行） | 绿（<100 ms） |
| 取消后过时预判不得提交 | 红（向量已写回） | 绿（不写回） |
| 取消后缓存与数据库一致 | 红 | 绿（无话题向量、用户消息仍在） |
| 取消后不发切换建议 | 红 | 绿 |
| 同一输入 + 模型身份复用嵌入 | 红（每次重新推理） | 绿（只推理一次） |
| 正常一轮仍提交话题向量 | 绿（防止「过度修复」） | 绿 |

### 8.1 task-12：后台整理的「修复前会红」

把 `backend/src/agent/services/maintenance.py` 的改动 `git stash` 掉、保留测试文件再跑
（其余改动不动）：

| 用例 | 修复前 | 修复后 |
|---|---|---|
| 维护期间事件循环停顿 | **2493 ms**（断言 <100 红） | 绿（<100 ms） |
| 维护期间取消请求被挡住 | **2437 ms**（断言 <100 红） | 绿（<100 ms） |
| 计算期间开始新一轮 → 过时结果不落地 | 红（`find_contradiction_hits` 不存在 = 没有代次校验） | 绿（矛盾不下调、候选不落） |
| 维护照旧落地（矛盾下调 + 工具候选） | 绿（防止「为了数字改语义」） | 绿 |

（修复前的 2493 ms 是「12 条消息 × 50 ms 慢嵌入」的替身规模；真实模型规模见 §5.1 的
503 ms / 5.4 s。）

实现改动清单见 §4；另外两处顺带修好的真实缺口：

- **取消落在上下文装配期间时绑定不记终态**：装配现在会把推理交给工作线程，
  「取消正好发生在装配期间」变得常见；`_execute_turn` 的取消检查点补上
  `bindings.mark_status(..., "cancelled")`（否则 `TurnBinding.status` 停在 None）。
  `tests/test_cancel_and_rapid_turns.py` 抓到了这条。
- **老后端 / 替身没有批量写接口**：`finish_prediction` 在没有 `save_topic_vectors`
  时退回逐条 `update_topic_vector`（`tests/test_predictor.py` 的冷启动用例抓到了这条）。

## 9. 仍未做 / 未验证（汇总）

- **后台整理整段占住事件循环**：已修（task-12），见 §5.1；wall 仍是 ~0.5 s（工作量没变），
  loop_gap 503 ms → **17 ms**，真实 HTTP 探针 6–12 ms。
- 后台整理里 LLM 步骤的耗时：**NOT MEASURED**（本机无凭据，那几步没有真正产出）。
- 服务「已就绪但模型仍在加载」的窗口：**NOT RUN**（加载发生在应用构造期，构造不出该窗口）。
- 真实多实例共享数据目录（§7 A/B）：**NOT REPRODUCED**，只有静态证据与复现条件。
- 残余 GIL 停顿（§3）：已测量并如实记录（整理修复后剩下的 17 ms 就是它）；要再降需要进程池，
  本轮不做。
- 轮末与后台派生工作仍可能在事件循环上占住 ~150 ms（慢召回整轮用例测到）：这是
  **另一条**路径（派生任务 / 索引与落库），不在「预判 + 检索 + 整理」范围内，未改动。
- bench 工具自身的一处缺陷（协程函数被当同步调用，导致维护一轮被记成 0.0 ms）已修，
  并加了防呆断言；勘误写在 §1 的表下。
