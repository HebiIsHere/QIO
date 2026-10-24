# R9-H10 合并与验收材料

> 基线 r9h9/main fec13810（代码 1f460c67）；修复分支 r9h10/main；根因与生命周期 docs/r9h10-root-causes.md；状态 docs/status.md P85。
> 完整 CI 本轮不新增（历史 2/2 已用尽）；最终代码同一 SHA 做 Linux py311/py312/Windows 定向 CI。

## 1. 基线与提交

| 项 | 值 |
| --- | --- |
| 基线 HEAD | fec13810（代码 1f460c67） |
| 修复分支 | r9h10/main |
| 最终代码 SHA | 0a0df3a1f84eec1f0182885efa2af563382a07f4 |
| 最终 HEAD | 见最终报告（其后仅 docs/evidence） |

## 2. 修复

- 问题一：normalize/serialize/deserialize 纯数据化；绑定只在要求保存/恢复显式创建，记录 id/version/status/path/identity/birth/source；历史无绑定不自动补建。
- 问题二：program_name 文件操作使用 binding.path/identity；write 限制阻止修改程序文件但不阻止执行；read/delete/execute 分别核对。
- 问题三：路径限制使用路径 + 文件身份/生命周期；硬链接读取/执行命中；同内容复制件不合并。
- 问题四：原绑定路径对象身份变化进入绑定失效拒绝/恢复，不自动绑定新对象；旧对象硬链接仍保护；普通批准不能解除。

## 3. 证据

- 基线 fec13810 跑 tests/test_r9h10_regressions.py：7 项中 6 失败（四项问题覆盖）。
- 修复后 H10：7 passed。
- H9/H8/H7/H6/H5/H4 核心：217 passed。
- 既有宽回归：首次运行出现 test_r9h4_identity_lifecycle 单项环境性失败，同套件立即复跑 24 passed；完整宽回归复跑 732 passed；最终定向 CI 全绿。
- 真实工具/台账/引擎/审批；模型 fake/mock；批量回收 fake 移动，非真实系统回收站。

## 4. CI

- 完整 CI 未新增（历史 2/2：39930482370、39958699070 仅基线证据）。
- 最终代码定向 CI：40106770972（backend-py311）success、40107072141（backend-py312）success、40107409191（backend-windows）success。

## 5. 边界

- 绑定创建依赖保存时平台程序查找和文件身份能力；无绑定历史按未绑定恢复处理。
- 同 size/mtime 伪造、身份不可读等按明确状态处理。
- 执行前核对到系统调用仍有竞态；真实回收站/安装版/真实模型/完整矩阵未验证。

## 6. 合并准备

- 未合并 main、未发布安装包；最终定向 CI 成功后仅 docs/evidence 更新（[skip ci]）。
