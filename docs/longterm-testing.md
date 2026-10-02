# 长期测试体系（护栏清单）

这份文档回答三个问题：**哪些护栏在守什么、怎么跑、坏了会怎样**。
它不是功能说明，也不是进度表（进度见 `docs/status.md`）；它是「改坏了会被谁拦住」的清单。

## 0. 两条总原则

1. **护栏必须有红绿对照证据**：一个从没红过的测试，和没有测试是两件事。每新增一条护栏，
   都要在临时副本上把它改坏、确认它会红，再恢复确认它会绿（做法见 §7）。
2. **标定值写在测试里，出处写在评测数据里**：阈值/标定值只能由 `backend/evals/` 下的
   真实评测给出；改标定值必须在提交里写清依据，而不是把断言改松。

## 1. 话题判定标定回归

| 项 | 内容 |
| --- | --- |
| 守什么 | 「延续 / 切换 / 新建」的判定不被悄悄改坏（尤其别退回单阈值 0.7 那套） |
| 在哪 | `backend/tests/test_topic_decision_eval.py` |
| 数据 | `backend/evals/topic_threshold/cases.jsonl` + 真实 ONNX 余弦快照 `scores_onnx.json` |
| 怎么跑 | `cd backend && uv run --frozen pytest tests/test_topic_decision_eval.py -q` |
| 重新记录 | `QIO_MODEL_DIR=<模型目录> uv run --frozen python evals/topic_threshold_curve.py --record` |
| 坏了会怎样 | 整体 acc / 真新话题召回 / 延续召回 / 假新话题率跌破标定下限，或多轮类别跌破逐类下限 |

判定走的是**生产代码**（`TopicPredictor._rank` + `affinity.classify`）与生产默认参数
（`backend/src/agent/services/params.py` 的 `TOPIC`），所以断言的就是产品行为。

**多轮上下文**：`multi_turn_*` 类别的用例带 `previous_exchanges`，评测把历史 user 轮折进
当前话题的指纹文本再冷启动话题向量（生产里话题向量就是话题指纹文本的 embedding，
指纹里的 summary 随对话累积；这里用历史轮次文本近似，不做真摘要）。快照同时记录
`scores_without_context`，测试据此断言「上下文真的改变了分数」，并要求至少有一条用例的
**判定**依赖上下文。注意：折历史是**近似**，不是生产摘要的等价物 —— 见 §8 的已知边界。

## 2. 分段边界决策守卫

| 项 | 内容 |
| --- | --- |
| 守什么 | 「说不准就不切」这条纪律：规则臂不误切、acc 不退化、语义切分不被悄悄打开 |
| 在哪 | `backend/tests/test_boundary_semantic_eval.py` |
| 数据 | `backend/evals/boundary_semantic/cases.jsonl`（真实 ONNX 余弦快照 `scores_onnx.json`） |
| 怎么跑 | `cd backend && uv run --frozen pytest tests/test_boundary_semantic_eval.py -q` |
| 看曲线 | `uv run --frozen python evals/boundary_semantic/run_boundary_arms.py` |
| 坏了会怎样 | 误切数 > 0、规则臂标定值漂移、`DEFAULT_BOUNDARY_MODE` 不再是 `shadow`、
| | 或者生产策略里出现语义/向量输入（`embedding` / `cosine` / `similarity`） |

当前结论（记录在测试与 `backend/evals/boundary_semantic/result_onnx.json`）：规则臂误切 0、
漏切 8；语义臂最好一档只多覆盖一个用例，而且阈值从 0.35 挪到 0.45 误切就从 0 涨到 6。
所以语义切分**保持 shadow**，生产策略不接收语义输入 —— 这条由测试守卫，不靠自觉。

## 3. 每模块独立进程导入冒烟

| 项 | 内容 |
| --- | --- |
| 守什么 | 「谁先被导入决定成败」的循环导入（历史环：`agent.tools.registry → agent.core.loop → registry`） |
| 在哪 | `backend/tests/test_import_smoke.py` |
| 怎么跑 | `cd backend && uv run --frozen pytest tests/test_import_smoke.py -q` |
| 坏了会怎样 | 某个模块无法作为**第一个** import 成立；清单缩水也会失败（保证覆盖整棵树） |

它给 `src/agent` 下每个模块各起一个新解释器，只 import 它自己。整套测试一起跑时导入顺序
恰好成立，所以只有这种「独立进程 + 全量模块」的冒烟才能抓住这类缺陷。

## 4. 发布闸门自检与历史

| 项 | 内容 |
| --- | --- |
| 守什么 | 闸门本身没坏（该红的会红）+ 每次判定的结果有历史可比 |
| 在哪 | `scripts/release_gate.py`（`--selftest` / `--history`），回归测试 `backend/tests/test_release_gate_history.py` |
| 怎么跑 | `python scripts/release_gate.py --selftest`；真判定 `python scripts/release_gate.py` |
| 看历史 | `python scripts/release_gate.py --history` |
| 历史文件 | `docs/releases/release-history.jsonl`（可用 `--history-file` 改到别处） |
| 坏了会怎样 | 自检会指出哪类缺陷没被抓到；历史比较会指出这次比上次多了/少了哪些通过项 |

历史放在 `docs/releases/` 而不是 `dist/`：发布记录要跟版本说明一起进仓库、随 PR 评审，
`dist/` 会被清掉。历史写不进去时闸门只打警告，判定不受影响 —— 判定只能由真实产物决定。

## 5. 执行链护栏（上一阶段建立，保持有效）

| 项 | 内容 |
| --- | --- |
| 守什么 | 工具 worker 的协议、退出码语义、取消/超时清理、输出上限、冻结产物能跑 |
| 在哪 | `backend/tests/test_sandbox_worker.py`、`backend/tests/test_tool_worker.py`、`backend/tests/test_sandbox_executor.py`、`scripts/frozen_worker_smoke.py` |
| 怎么跑 | `cd backend && uv run --frozen pytest tests/test_sandbox_worker.py tests/test_tool_worker.py -q`；
| | 冻结产物冒烟 `python scripts/frozen_worker_smoke.py <qio-backend.exe>`（源码模式加 `--script`） |
| 坏了会怎样 | 协议/清理/上限的断言失败；冻结冒烟三项里对应项 FAIL |

CI 上真跑的步骤见 `.github/workflows/ci.yml`：Linux 真 worker 冒烟（源码模式）、Linux fake worker
协议用例、Windows 后端测试、Windows 冻结冒烟（真 onefile 产物 + 包内 worker 源码断言）、
发布闸门自检。

## 6. 新增护栏时怎么写

1. 先有**可复现的失败**（评测数据或最小复现），再写断言；不要为了绿而写测试。
2. 标定值（阈值、下限）必须来自真实评测，并在测试里写清记录时间与出处；
3. 用「退化会红」的措辞：断言失败信息里要告诉后来者**该做什么**（重跑哪个评测、重记哪组值）。
4. 不许用 skip / xfail / 放宽断言 / 平台特判换绿灯；评测数据只增不减（缩水也要红）。

## 7. 红绿对照怎么做（护栏有效性验证）

在**临时副本**上把被守的东西改坏，跑一次确认它会红，再恢复确认它会绿。
恢复用 `git checkout -- <文件>`，并确认 `git status --short` 里不再有它。
已经做过两次对照（话题阈值改回 0.7 → acc 0.847 跌破下限、多轮 switch 类别 0/1；
去掉否定句护栏 → 误切 0 变 3），结论：这两套护栏都抓得住退化。

## 8. 已知边界（诚实清单）

* 话题/边界评测用的是**记录的余弦快照**，不加载模型 —— 它守的是「判定逻辑 + 参数」，
  不是「模型文件没被换掉」。模型身份在快照的 `model` 字段里（identity 哈希）。
* 多轮上下文用**历史轮次文本**近似生产里的话题摘要；近似可能让话题向量比真实摘要更
  泛化，从而**高估**上下文的作用。当前语料里 14 条多轮用例的分数都随上下文变化，
  但只有 1 条的**判定**依赖上下文 —— 不要把它读成「多轮已经完全被覆盖」。
* 发布闸门只做**离线可判定**的部分；安装 → 首启 → 工具全链 → 卸载仍必须真机执行，
  闸门输出里明确写着这一点，不要把它读成「发布已验证」。
* 边界语料的 `new_goal` 类别当前 0/4（规则臂漏切），是**已知漏切**，不是误切；
  测试钉住的是「误切 0」这条纪律与整体标定值，漏切数据变化会体现在标定值里。