# licenses/ —— 第三方组件声明

这个目录是 QIO 的**第三方开源组件声明**，它回答两个可判定的问题：

1. 我们随安装包/前端产物分发了哪些第三方组件，各自的许可证与版权行是什么；
2. 这份声明有没有随着依赖更新而漏掉组件（由覆盖率校验回答）。

## 文件

| 文件 | 内容 | 谁生成 |
| --- | --- | --- |
| `third-party.json` | 机器可读清单：组件（生态/名称/版本/许可证/版权行/出处） | generator |
| `overrides.json` | `manual` = 人工确认过的条目；`fetched` = `--refresh` 联网写入 | 人 / generator |
| `texts/<SPDX>.txt` | 各许可证全文（MIT / Apache-2.0 / OFL-1.1 / PSF-2.0 …） | generator |
| `../THIRD-PARTY-NOTICES.md` | 人类可读且自包含的完整声明（组件表 + 全文） | generator |

## 覆盖范围（不要缩水）

- `backend/uv.lock` 的全部依赖；
- `frontend/package-lock.json` 的全部条目；
- `frontend/src-tauri/Cargo.lock` 的全部 crate；
- 外加随包分发的资产：内置 CPython 运行时、`python-build-standalone`、
  `bge-small-zh-v1.5` 嵌入模型、Noto Serif SC 字体。

**过覆盖是有意的**：锁文件是全平台的（Linux / macOS / Android / WASM 的依赖也列在里面），
而 QIO 目前只发 Windows 安装包。宁可多列不可漏列 —— 漏列才是合规问题。
构建期工具（uv、Rust/Node 工具链、PyInstaller、NSIS、Playwright、Docker）不随包分发，
因此不在声明里，见 `docs/compliance.md`。

## 常用命令

重新生成（改了依赖就重跑，并提交生成物）：

    python scripts/licenses/build_third_party.py

联网补元数据（本机装不出来的平台依赖；结果写进 `overrides.json` 的 `fetched` 段，
之后普通运行不再需要网络）：

    python scripts/licenses/build_third_party.py --refresh

校验覆盖率（离线、CI 会跑；少一个组件就红）：

    python scripts/licenses/check_third_party.py
    python scripts/licenses/check_third_party.py --selftest

## 上游取不到信息时怎么办

`"license": "UNKNOWN"` 会让校验直接失败，这是有意的：认不出来就必须人工确认，
而不是让它悄悄过去。确认后写进 `overrides.json` 的 `manual` 段，键为
`<eco>:<name>@<version>`，可填 `license` / `copyright` / `attribution` / `note`。
`manual` 段脚本永不覆盖；`fetched` 段每次 `--refresh` 都会重写。

版权行确实不存在的上游包（许可证正文本身不含版权行）允许只留 `attribution`
（上游仓库地址），声明里会单列一节说明出处。
