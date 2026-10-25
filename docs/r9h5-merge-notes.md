# R9-H5 合并与验收材料（六项审批授权补修）

> 基线：r9h4/main = d2403425490fdb82555cf66bac48d708d29e8f6c（上轮定向代码 76e6248b）
> 修复分支：r9h5/main；契约 docs/r9h5-contracts.md；状态 docs/status.md P80
> 完整 CI 本轮不新增（历史累计 2/2 已用尽）；只做同一代码 SHA 的 Linux py311/py312/Windows 定向 CI。

## 1. 基线与分支

| 项 | 值 |
| --- | --- |
| 基线 HEAD | d2403425（代码 76e6248b） |
| 修复分支 | r9h5/main |
| 子 worktree | r9h5/a、r9h5/b、r9h5/c、r9h5/d（最终由 Lead 集成） |
| 代码 SHA | b52715385c855aab6f29e8cb315f9c05193283ff（定向 CI 验证；其后仅 docs/evidence） |

## 2. 六项修复

1. 文件写入前置：fs_write/fs_patch 在模型/人工等待后以当前真实磁盘核对 identity/base content/absent；RecheckService 路径也走本地核对；变化 invalidated。
2. 脚本事实绑定：审查前 launch facts/spec_digest/argument_files 绑定；直接模型允许和人工恢复执行前比较当前事实；替换脚本不执行。
3. patch 读取：先 read effect 授权再读正文；拒绝不读/不补/不写；读写分别；spy 证明未读。
4. 批量审批：多子项 common_only 共同条件；逐项子项 effect 重核对；单项限制/内容只失效该项；结果 partial/count 保留。
5. 结构化决策：AuthDecision.decision_code 由引擎结构事实设置；operations 只认 code，不解析中文 reason；旧无 code 需重新确认。
6. 程序限制：只按平台规范化真实路径/链接目标匹配；删除 basename/去后缀/统一小写回退；不隐式解释裸名称。

## 3. 真实流程证据

- 基线 8f07f86f 跑 tests/test_r9h5_regressions.py + test_r9h5_regressions2.py：23 项中 10 失败（写入等待/新文件、patch 禁读三模式、脚本替换、批量、结构化决策、程序同名）。
- 修复后同一批：23 passed。
- 宽回归（见最终命令）：695 项中先前 2 个模式变化测试已修复并复跑；以最终记录为准。
- 模型全部 fake/mock；真实工具/台账/引擎/审批服务/执行恢复；批量回收使用临时 fake 移动，未验证真实系统回收站。

## 4. CI

- 完整 CI 未新增（历史 2/2：39930482370、39958699070 仅基线证据）。
- 最终代码定向 CI：40065856405（backend-py311）success、40066080217（backend-py312）success、40066324492（backend-windows）success。

## 5. 边界

- 执行前核对后到实际系统调用之间仍有竞态；不新增沙箱/文件代理/全盘扫描。
- 真实系统回收站、安装版、真实模型、完整矩阵未验证。
- 程序名称限制没有隐式回退；若需要按名称限制必须新增明确结构化类型（本轮未新增词表）。
- Linux/Windows 链接别名以平台权限为准；无法创建链接时测试如实跳过该分支。
- 旧审批没有 decision_code 时不再按中文 reason 放行，需要重新确认。

## 6. 合并准备

- 未合并 main、未发布安装包；最终定向 CI 成功后仅 docs/evidence 更新（[skip ci]）。
