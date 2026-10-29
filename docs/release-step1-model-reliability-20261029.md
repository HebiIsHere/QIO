# 发布前第一轮模型可靠性修复（2026-10-29）

- 基线：`fix/settings-visual-20261028` @ `3fefb59e55059a50a0d8006ac56f453a6b5f74b7`
- 工作分支：`fix/release-step1-model-reliability-20261028`
- 范围：只处理发布前第一轮指定的四项问题；不合并 main、不打标签、不发布。
- 未改动历史迁移、权限规则、用量归属；测试全部使用 fake provider，不联网。

## 四项修复

### A. 同一凭据下不同模型的连接不能相互关闭

- 原触发条件：`_remember_adapter` 在建同一 `key_id` 的另一个模型 adapter 时，会淘汰并关闭该凭据全部其它缓存项；后台三类派生任务分别使用不同模型时，先建的连接可能在请求前已被关闭。
- 修复：缓存按槽位 `(key_id, model)` 管理；只有同一槽位换配置才顶替旧 adapter，被顶替者进入有界暂留名单，运行期不关闭；缓存与暂留均有上限，超限只松开引用；`aclose()` 统一关闭缓存与暂留中的 adapter。
- 验证：`backend/tests/test_release_step1_a.py`（三模型共存、同配置复用、轮换不中断旧工作、有界缓存与真实回收、退出清理、用量归属），并改写 `test_adapter_lifecycle.py` 中锁死旧缺陷的那条断言。

### B. 并行后台工作必须分别持有自己的模型配置

- 原触发条件：`use_adapters` 把按 kind 的 adapter 映射写入实例属性 `_task_adapters`；两个后台派生工作交错时互相覆盖，后续阶段可能读到别人的配置、空配置或残留的旧配置。
- 修复：改为任务上下文隔离（`contextvars.ContextVar`），`drain_derived_tasks` 在入口快照本次配置并显式传给两批 `_drain_batch`；`_drain_batch` 不再读取实例可变状态。显式 `adapters=` 与 `UsageUnavailable` 如实失败语义不变。
- 后续修复（B 剩余问题）：`build_derived_adapters` 在**第一次异步等待之前**同步解析并冻结本批三项目的与不可用结果，再逐个建连接；用户在建第一种连接期间修改用途设置，只影响下一批，不再出现同批 summary 旧配置、knowledge/entities 新配置的混用。
- 验证：`backend/tests/test_release_step1_b.py`（asyncio.Event 明确同步点交错、单工作三阶段一致、取消/失败收尾不污染、无实例残留；新增「暂停第一种连接构建 → 修改三项用途设置 → 放行」的确定性测试，同时覆盖旧目标与旧不可用结果、下一批全部新配置），并回归既有派生测试。

### C. 模型目录与真实调用对连接可用性的判断一致

- 原触发条件：目录侧 `model_usable`/`connection_unusable_reason` 只检查模型行验证失败，不检查连接级 `verify_state` 与密钥是否可读；未验证连接在目录里可选，真实调用以 `credential_unverified`/`secret_missing` 拒绝。
- 修复：连接级判断与 `credential_problem` 逐项同序（状态 → 启用 → 验证状态 → 预算 → 密钥可读）；新增 `REASON_CREDENTIAL_UNVERIFIED` / `REASON_SECRET_MISSING` 并映射到运行时 reason_code；`legacy` 历史连接与「连接已验证、模型未单独验证」保持可用；目录只读本地事实，不联网、不发验证请求。
- 验证：`backend/tests/test_release_step1_c.py`（状态矩阵一致性、未验证反例、历史兼容、密钥不可读、目录不联网、响应/日志不含密钥原文）。

### D. 「日常对话」一次选择整体保存，并显示真实生效目标

- 原触发条件：前端把主对话 / 通用对话合并行拆成两次顺序保存，第二次失败会留下半个配置；合并行显示 `main-chat` 自己的 `resolved`，而真实运行规则是 `general-chat` 显式优先，可能显示一个、调用另一个。
- 修复：后端新增 `POST /api/models/usages`，在一个 SQLite transaction 内整批写入，任一项失败全部回滚；旧 `POST /api/models/usage` 保留。`GET /api/models/catalog` 用与运行时同一个 `resolve_shared_target` 计算 `effective`，前端合并行与「当前实际使用」优先展示它；恢复跟随默认通过同一批量入口一次清两个 key。
- 验证：`backend/tests/test_release_step1_d.py`（批量成功/失败回滚、旧接口兼容、effective 与真实主目标一致、不一致配置取 general-chat、不可用原因）、`frontend/src/components/models/__tests__/ReleaseStep1D.test.ts`（只发一次批量请求、失败不 reload、合并行与只读区使用 effective）。

## 本地验证

- 后端定向：四项新增测试与受影响的 adapter/派生/目录/用途/路由既有测试全部通过。
- 前端：`npx vue-tsc --noEmit`、`npm run build` 通过；定向 vitest（模型组件 + 设置页）通过；全量 `npm test` 只有一个在基线上同样失败的 Windows 环境用例（同一基线提交的 Linux CI 运行通过），不属于本轮改动。
- 文档：`python scripts/check_docs.py` 与 `--selftest` 通过。
- 完整后端 `uv run --frozen pytest` 在上一候选提交（四项修复）上通过（exit 0，无失败用例）；B 剩余问题为后续定向修复，按用户要求只跑定向测试、未重跑全量或 CI。

## 范围外记录（本轮不处理）

1. 模型级验证成功不会提升连接级 `verify_state`：目录会如实显示连接未验证（与真实调用一致），但用户期望可能需要产品决策；改动会触及模型验证语义，本轮不做。
2. 目录 GET 会按连接读本地 keyring 判断密钥可读性（不联网、不发起验证）；读次数随连接/用途数量增长，性能优化不在本轮范围。
3. adapter 暂留名单为有界强引用（应用退出统一关闭）；更激进的回收需要弱引用方案，当前取舍已在代码注释说明。
4. `frontend/src/views/__tests__/i2v2-a-intent-flow.test.ts` 的「页面卸载后不再继续轮询」用例在本机（Windows）基线与修复分支均失败；同一基线提交的 Linux CI 通过，判断为环境相关既有问题，本轮不修改该测试或互动页实现。
