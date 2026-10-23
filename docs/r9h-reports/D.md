# R9-H D 交付报告：固定查询兼容性 / 真实流程回归 / CI 排查

- 分支：r9h/d-verify（worktree D:/qio-dev/qio-r9h-d，基线 39c0f48e = r9g/main + R9-H 契约与预接线）
- 独占文件：backend/src/agent/security/fixed_queries.py（修改）、backend/tests/test_r9h_fixed_queries_platform.py（新增）、
  scripts/r9h_ci_audit.py（新增）、docs/r9h-reports/D.md（本报告）、证据 _r9h-evidence/
- 纪律：未改其他人文件、未 push/merge、未触发任何全量 pytest/完整 CI；模型零调用（纯规则测试）。

## 1. 真实 CI 日志核对（上一轮失败项 → 当前代码状态）

数据来源：gh run view --json / --log-failed（真实日志已落盘证据 _r9h-evidence/run2-fixed-query-failure.log）。

| run | SHA | 结论 | 失败 job | 失败项（backend） | 根因 | 当前状态 |
| --- | --- | --- | --- | --- | --- | --- |
| 39899916527 | 4ad0d9e6 | failure | backend (py3.11/py3.12/windows-latest)、install e2e | 迁移补偿链（test_fu_w3_b01_schema / test_irm_b_migration_39 / test_p1_b_migration / test_p2_d / test_p3_d 等）、test_authorization_engine 关键词禁止 3 项、test_dev_workflow_integration、test_tool_restore、test_r8_cancel_target_verify、test_r9_acceptance_e2e | 补偿 58 只抄 56+57，存量库补偿链不完整；模型调用入口清单缺 review/resolution；取消计数未计入要求解析调用；install e2e FIFO 被要求解析吃掉 | 已在 c085b745 修复（迁移完整链/清单/取消计数/假厂商自动响应）；后续 run 2 未再出现 |
| 39903261628 | c085b745 | failure | backend (py3.11/py3.12/windows-latest) | test_r9_acceptance_e2e::test_restricted_write_pends_then_approval_executes；test_r9g_fixed_queries::test_node_npm_version_forms（仅 Linux 两平台）；test_tool_restore::test_restore_agent_tool_after_restart；test_interactive_during_heavy_work::test_health_probe_stays_responsive_while_slow_prediction_runs（355ms>300ms） | ① 旧 R9 验收审批执行链路绑定（已修）；② Linux 上 /usr/local/bin/npm 是脚本入口符号链接，真实目标 npm-cli.js 与入口名不同族（当时精确白名单与同族放宽都不足）；③ 自建工具重启恢复审批绑定（已修）；④ 健康探测 355ms 属 runner 调度地板（R9-F W4 已有压力对照诊断，非固定查询） | ①③ 已由 b0eb34b3 修复，且最终树定向 CI（39905686592 / 39905806975 / 39905693250）三文件通过；② b0eb34b3 只补了 /usr/local/bin 精确目录，/opt/<...>/bin 布局与"真实工具流程传入 resolve 后脚本目标"仍未覆盖 → 本轮修复；④ 保持已知问题（测量窗口问题，见 R9-F W4 报告），不归固定查询 |
| 39905686592 / 39905806975 / 39905693250 | b0eb34b3 | success | — | 定向三文件（test_r9_acceptance_e2e / test_r9g_fixed_queries / test_tool_restore） | — | 证据见 docs/r9g-merge-notes.md；本轮未重复派发 |

- 本轮未触发全量/完整 CI（纪律）；下方 §6 给出建议的定向 rerun 范围与命令（由 Lead 派发）。
- 说明："六个定向测试文件 100 passed / 1 failed" 复核：按计数可对齐为
  test_r9g_fixed_queries(21) + test_r9g_deletion(22) + test_r9g_cmd_binding(16) + test_r9g_resolution(21) +
  test_r9g_semantics(17) + test_r9_acceptance_e2e(4) = 101；唯一失败即 test_node_npm_version_forms。
  本机（Windows）修复前该组合全部通过（Windows npm 是真实 .cmd，不以符号链接进入），故用平台规则测试复现 Linux 失败。

## 2. 复现与根因（区分程序身份 / 符号链接 / 安装位置 / 测试假设）

真实 CI 失败事实（py3.12，run 39903261628）：

    FixedQueryFacts(kind='program', program_path='/usr/local/bin/npm',
                    program_name='npm', argv=('--version',), cwd=None, shell=False) → None

复现与区分（证据 _r9h-evidence/d-ci-audit.json、d-platform-red.log）：

1. 程序身份：请求身份是"npm"，入口文件名也是"npm"；但符号链接的真实目标是
   /usr/local/lib/node_modules/npm/bin/npm-cli.js —— 真实文件名（npm-cli.js）与请求身份不同族。
   旧逻辑只允许"真实文件名同族"（python3 -> python3.11），对脚本入口不成立。
2. 符号链接：分类器对 program_path 做 realpath 后比较真实文件名；npm/pnpm/yarn 在 POSIX
   都是"入口 -> 脚本"的符号链接，必须按"系统安装目录 + 同族脚本目标"识别，不能按二进制同名规则。
3. 安装位置：b0eb34b3 的精确白名单只有 /usr/bin、/usr/local/bin、/bin、…、/opt/homebrew/bin、
   /opt/local/bin，不含 /opt/<...>/bin；而 Linux 上 node 常见于
   /opt/hostedtoolcache/node/20.x/x64/bin/npm、/opt/node/bin/npm → 入口目录不被信任 → 判定 None。
4. 测试假设 vs 真实流程：单测把 shutil.which() 返回的入口路径直接塞进 facts；
   而真实工具（cmd_tools → launch.build_spec_facts）传的是 resolve 之后的真实目标
   （program_path=npm-cli.js、program_name=npm-cli.js）—— 入口名已丢失。这意味着即使入口目录
   被信任，真实流程也可能因为"族从未知脚本名推导"而拿不到固定查询（此前六文件测试未覆盖该路径，
   属于本轮"真实流程回归"补测发现的第二种子问题）。

## 3. 修复（backend/src/agent/security/fixed_queries.py，仅本文件）

原则不变：只看真实程序身份 + 精确 argv 结构，不搜索任何文字/程序名整放行；解析不确定 → None。

1. 系统安装目录规则（替换精确白名单）：
   - _POSIX_SYSTEM_BIN_DIRS（/bin、/usr/bin、/usr/local/bin、/sbin、/usr/sbin、/opt/homebrew/bin、
     /opt/local/bin、/snap/bin）
   - _is_posix_system_bin_dir()：精确名单 + /opt/<至少一个子目录>/bin 结构
     （/opt/hostedtoolcache/node/20.19.0/x64/bin、/opt/node/bin 通过；/opt、/opt/evil、/opt/evil/lib、
     /tmp/bin、/home/u/bin、工作区 bin 拒绝）。
   - _under_posix_system_roots()：真实目标必须位于 /usr、/bin、/sbin、/lib、/lib64、/opt、/snap、
     /System、/Library、/var/lib 之内；/tmp、/var/tmp、/home、/workspace、相对路径一律拒绝。
2. 同族脚本入口放宽：
   - _SCRIPT_ENTRY_FAMILIES（node/npm/pnpm/yarn/pip/pip3/uv/bun/deno/corepack），
     _family_from_script_target("npm-cli.js") == "npm"、("pnpm.cjs") == "pnpm"；
     其它脚本（evil.js、python/site.py、tool.js）不识别。
   - _posix_script_entry_allowed(entry, real, family)：入口目录必须是系统 bin 目录 且
     真实目标在系统根内 且 目标是同族脚本 —— 三条同时成立才放宽。
3. 真实流程（resolve 后目标）：
   - _resolved_script_family(real_path)：仅当真实目标位于 POSIX 系统安装根内，才按同族脚本名
     推导程序族；工作区/用户/临时目录里的同名脚本一律不识别（不因脚本名增权）。
   - classify_fixed_query 在入口名族未知时回退到该函数（Windows 路径不满足 POSIX 绝对路径，
     行为不变；Windows npm.cmd 仍走同名归一化）。
4. 身份规则重构为纯函数 _identity_rule_problem(facts, raw_path, real_text)（无文件系统访问），
   文件存在性/可执行位仍在 _identity_problem；这样 POSIX 规则可在 Windows 上用真实生产代码
   字符串验证（环境假设见测试文件头）。
5. 负向保证（未放宽）：工作区/cwd/临时目录同名替身、任意可写目录、任意脚本、/opt 下任意程序
   （未知程序族、非族名脚本）、shell=True、解释器脚本、未知 argv 全部仍为 None。
   同名替身/内容替换负向用例保留在 test_r9g_fixed_queries.py 并全部通过。

残余风险（如实）：放行前提是"系统安装根不可被普通用户写入"（/usr、/opt 等通常 root-owned）；
若攻击者已能写 /usr 或 /opt，属于已提权场景（任何 PATH 程序都会受影响），不在本规则能力内。
未新增沙箱/隔离/受控代理，未新增关键词判断。

## 4. 测试与结果（全部定向，未跑全量）

~~~powershell
cd D:/qio-dev/qio-r9h-d/backend

# 修复前（红）：新增平台测试因缺少新规则函数 + /opt 规则不成立而失败
uv run --frozen pytest -q tests/test_r9h_fixed_queries_platform.py -p no:warnings
# → 13 failed（AttributeError: 生产代码尚无 _identity_rule_problem/_is_posix_system_bin_dir/...）
# 证据 _r9h-evidence/d-platform-red.log

# 修复后（绿）
uv run --frozen pytest -q tests/test_r9h_fixed_queries_platform.py -p no:warnings
# → 11 passed
uv run --frozen pytest -q tests/test_r9g_fixed_queries.py tests/test_r9h_fixed_queries_platform.py -p no:warnings
# → 32 passed（21 + 11）；证据 _r9h-evidence/d-fixed-queries-green.log

# 六个定向测试文件（与"100 passed / 1 failed"同一组）
uv run --frozen pytest -q tests/test_r9g_fixed_queries.py tests/test_r9g_deletion.py tests/test_r9g_cmd_binding.py tests/test_r9g_resolution.py tests/test_r9g_semantics.py tests/test_r9_acceptance_e2e.py -p no:warnings
# → 101 passed（本机 Windows；Linux 的 /opt 路径由平台规则测试与建议的定向 CI 覆盖）
# 证据 _r9h-evidence/d-six-targeted-run.log

# CI 复现/规则审计脚本
uv run --frozen python ../scripts/r9h_ci_audit.py
# → exit 0；all_ok=true；本机真实 python/node/npm/git 固定查询均判 version
# 证据 _r9h-evidence/d-ci-audit.json
~~~

补充回归：test_r9g_engine_core.py + test_r9g_fs_binding.py + 平台测试 → 2 项失败
（test_structured_limit_denies_matching_write_in_unrestricted、test_limit_matches_only_structured_objects）。
已用 git checkout 临时还原 fixed_queries.py 证明这两项在基线 39c0f48e 本来就红（与本次改动无关，属
结构化限制接线未完成的基线状态），已在 §6 报给 Lead；其余（fs_binding 7、平台 11）全绿。

## 5. 与契约偏差 / 接口

- 无契约接口变更：classify_fixed_query / FixedQueryFacts / fixed_query_facts_from_detail 签名未变；
  只新增内部规则函数（下划线开头）。
- 未改 engine.py/review.py/tools/app.py/requirements.py；未新增依赖；未新增迁移。
- 建议 Lead 复核的跨文件接缝：真实工具流程目前只传 resolve 后 program_path（入口名丢失）。
  本轮分类器已能在"真实目标位于系统根内"时按同族脚本识别；若后续要在链路上保留"用户请求的
  入口名"用于审计展示，可在 B 的 cmd_tools 里把 program_facts.requested 一并带出（非必需，不影响判定）。

## 6. 未完成 / 未验证 / 需 Lead 处理

1. Linux 真实 /opt 文件系统：本机 Windows 无法创建 /opt 树；已用生产纯规则函数以 POSIX 字符串
   验证，并在建议的定向 CI 中由 Linux runner 真实执行（见下）。属未实跑项。
2. 全量 pytest / 完整 CI：按纪律未触发（Lead 预算）。
3. 基线已知失败（与本轮无关，请 Lead 分派）：
   tests/test_r9g_engine_core.py::test_structured_limit_denies_matching_write_in_unrestricted、
   tests/test_r9g_engine_core.py::test_limit_matches_only_structured_objects —— 基线 39c0f48e 已红，
   疑为 R9-H 契约 §6 结构化限制读取预接线 + C 的 requirement_limits 未合入导致，属 C/Lead 范围。
4. 建议定向 rerun（由 Lead 派发，不跑全量）：
   ~~~powershell
   gh workflow run targeted-verify.yml -f ref=r9h/d-verify -f sha=<本分支完整40位SHA> -f targets=backend-py311 -f pytest_node_ids="tests/test_r9h_fixed_queries_platform.py tests/test_r9g_fixed_queries.py tests/test_r9g_deletion.py tests/test_r9g_cmd_binding.py tests/test_r9g_resolution.py tests/test_r9g_semantics.py tests/test_r9_acceptance_e2e.py"
   # 同样 targets=backend-py312、windows 各一次；只跑固定查询/被影响文件，不重跑整套
   ~~~
   预期：Linux py3.11/py3.12 上 /usr/local/bin 与（runner 若用 hostedtoolcache）/opt 入口均判 version；
   windows 同基线全绿。运行号由 Lead 派发后补记。
5. 未验证 runner 镜像的真实 node 安装形态（符号链接 vs 真实文件）差异：两种形态都已覆盖
   （符号链接 -> 同族脚本放宽；真实同名文件 -> 同名身份直接通过），以 CI 实测为准。
