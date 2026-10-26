# 第三方组件与分发合规

QIO 的安装包与前端产物里**分发**三类开源代码与随包分发的资产：

| 类别 | 来源 | 数量口径 |
| --- | --- | --- |
| 后端 Python 依赖 | `backend/uv.lock` | 锁文件全部条目 |
| 前端 npm 包 | `frontend/package-lock.json` | 锁文件全部条目 |
| Rust crate | `frontend/src-tauri/Cargo.lock` | 锁文件全部条目 |
| 随包资产 | —— | 内置 CPython 运行时及其发行来源 `python-build-standalone`、内置嵌入模型 `bge-small-zh-v1.5`、内置 Noto Serif SC 字体；逐条列在 `THIRD-PARTY-NOTICES.md` 的 bundled 条目里 |

MIT / BSD / ISC / OFL / PSF / MPL 这类许可证要求「随二进制复制版权声明与（部分要求）许可证全文」。
所以「仓库里没有一份完整声明」不是文档欠债，而是**分发时的合规缺口**。这份文档说明声明在哪、
怎么保证它不随依赖漂移，以及**还有哪些边界没有覆盖**。

## 一、声明在哪（一个来源，四个出口）

唯一来源是仓库根目录与 `licenses/`：

| 位置 | 内容 | 谁生成 |
| --- | --- | --- |
| `LICENSE` | QIO 自身的授权（MIT） | 人 |
| `THIRD-PARTY-NOTICES.md` | 完整声明：组件清单 + 每个许可证全文 | `scripts/licenses/build_third_party.py` |
| `licenses/third-party.json` | 机器可读清单（含每条的版权行 / 出处） | 同上 |
| `licenses/texts/*.txt` | 各许可证全文 | 同上（首次联网落库后随仓库走） |
| `licenses/overrides.json` | `manual` 人工确认；`fetched` 联网取回的元数据 | 人 / 生成器 |
| `licenses/README.md` | 目录说明与常用命令 | 人 |

出口（用户实际能拿到声明的地方）：

1. **应用内**：设置 → 关于，内联 `LICENSE` 与 `THIRD-PARTY-NOTICES.md` **原文**，离线可读；
2. **前端产物**：这两份文本在构建期内联进 `dist/`，而 `dist` 被 Tauri 打包进 `QIO.exe`；
3. **安装目录**：内置模型目录带 `LICENSE-BAAI-bge-small-zh-v1.5.txt` + `NOTICE.txt`；
   内置 Python 运行时带 `LICENSE-CPython.txt` + `NOTICE.txt`（由 `scripts/build_runtime.ps1` 放置，缺许可证直接构建失败）；
4. **源码仓库**：`LICENSE` 与 `THIRD-PARTY-NOTICES.md`。

## 二、覆盖范围（过覆盖是有意的）

- 声明覆盖**三套锁文件的全部条目**，包括只在 Linux / macOS / Android / WASM 上用到的 crate 与依赖；
  QIO 目前只发 Windows 安装包，多列不构成问题，**漏列才是问题**。
- 锁文件是全平台的，而开发机只能装出本机平台那一份：其余条目由 `--refresh` 从
  crates.io / PyPI 取一次元数据（含 `.crate` / 发行包里的许可证文件与真实版权行），
  结果落在 `licenses/overrides.json` 的 `fetched` 段。**CI 与用户侧都不需要联网。**
- 不随包分发的构建期工具**不列入**声明：uv、CPython 工具链、Rust/Node 工具链、PyInstaller、
  NSIS、Playwright、Docker。它们只是构建机器上的工具，不进用户产物。
- WebView2 是 Windows 的系统组件（由微软分发与更新），QIO 不附带它。

## 三、生成与校验

改了依赖就重新生成并提交生成物（这一步漏了会让 CI 变红，而不是悄悄漏声明）：

    python scripts/licenses/build_third_party.py

没有改依赖/锁文件时不需要重新生成：生成物（`THIRD-PARTY-NOTICES.md`、`licenses/third-party.json`）与锁文件保持同步即可。
只改声明里的**文字概括**时，改文档（本文件等），不要为了「让文字对上」去动生成物。

只在**维护者机器**上跑、需要联网（补本机装不出来的平台依赖的元数据；结果进 `overrides.json`）：

    python scripts/licenses/build_third_party.py --refresh

离线覆盖率校验（stdlib，CI 的 docs 任务里跑；少一个组件就失败）：

    python scripts/licenses/check_third_party.py
    python scripts/licenses/check_third_party.py --selftest

校验器检查七件事：锁文件组件是否全部在清单里、有没有 `UNKNOWN` 许可证、
每个被引用的许可证是否有全文、声明正文里是否出现每个组件、正文里是否真的嵌入了全文、
每条是否至少有版权行或上游出处、仓库是否有 `LICENSE` 与 `licenses/README.md`。
`--selftest` 用合成工作区逐条证明这些检查**既会绿也会红**。

## 四、已知边界（不粉饰）

- **安装向导没有许可证页**：Tauri v2 的 bundle 配置里没有 `licenseFile` 字段
  （可在 `frontend/src-tauri/gen/schemas/desktop-schema.json` 里核对：检索不到 license 相关字段），
  所以 NSIS 安装器不会额外弹一页许可证；文本由应用内「关于」分区与安装目录里的资源提供。
- **上游确实没有版权行的条目**：声明里单列一节给出上游仓库地址（例如 `objc2` 的 `.crate` 里
  一份许可证文件都没有，只有 SPDX 标识）。这类条目按「有出处」而不是「有版权行」处理，并在声明里写明。
- **声明的文本来源**：标准许可证全文来自 SPDX `license-list-data`；`PSF-2.0` 直接用随包 CPython
  自带的那份 `LICENSE.txt`。校验器只保证「有这个许可证的全文」与「组件没漏」，
  **不逐字节比对上游**（那需要把每个上游包的许可证都存下来，收益与成本不成比例）。
- **更新通道经过第三方加速镜像**（`gh.llkk.cc` / `ghproxy.net` / `ghfast.top`）：
  产物内容由 minisign 验签，但代理能看到请求元数据。这是隐私面的事实，不影响许可合规；写在这里是为了不假装它不存在。
- **生成输入的 sha256 与换行符**：生成器按锁文件**原始字节**计算输入 sha256 并写在声明开头。仓库统一 LF 存储（`.gitattributes`），但 Windows 检出可能是 CRLF；此时在本机重跑生成器会出现「只有『生成输入』三行 sha256 不同、组件与许可证完全没变」的差异 —— 那是换行符差异，**不要提交**。确实要重新生成时，先按 LF 规范化（或 `git config core.autocrlf false` 后重新检出），确认 `git diff` 只有真实语义变化再提交。
- **许可合规 ≠ 安全边界**：工具子进程不是安全沙箱，这条在 `docs/security/tool-execution-isolation.md` 里另说。

## 五、QIO 自身的授权

以仓库根目录 `LICENSE` 为准：**MIT**（Copyright (c) 2026 左修源、尚雅淇）。
MIT 只要求保留版权声明与许可声明，不要求下游开源 —— 这与 QIO 分发的第三方许可证并不冲突，但**不能概括成「依赖全是宽松许可、没有 copyleft」**。按实际清单（逐条以 `licenses/third-party.json` 与 `THIRD-PARTY-NOTICES.md` 为准；许可证组数与逐条归属用 `python scripts/licenses/check_third_party.py` 查，不写死在文档里）：

- 绝大多数是 MIT / Apache-2.0 / BSD-2/3-Clause / ISC / Zlib / Unlicense / CC0-1.0 / MIT-0 / 0BSD / OFL-1.1 / PSF-2.0 / Unicode-3.0 / BlueOak-1.0.0 / CDLA-Permissive-2.0 / Apache-2.0 WITH LLVM-exception 这类宽松许可（含 `AND` / `OR` 组合）；
- **存在弱 copyleft：MPL-2.0**（含 `MPL-2.0 AND MIT` 组合）。MPL 是**文件级** copyleft：对 MPL 覆盖的文件要求保留声明、并可按 MPL 获取对应源码；它不要求把整个 QIO 产物开源，也不构成链接传染；
- 少数条目把 **LGPL-2.1-or-later 与 MIT/Apache-2.0 并列为可选项**（`MIT OR Apache-2.0 OR LGPL-2.1-or-later`）；本项目按 MIT/Apache-2.0 使用，没有「只给 LGPL」的组件；
- 没有发现 GPL / AGPL 这类强 copyleft 的单项许可。

上面的划分是「清单里出现了哪些许可证表达式」的事实概括；具体分发义务（版权行、许可证全文、出处）以 `THIRD-PARTY-NOTICES.md` 的逐条条目与文末全文为准。

要换成别的许可（Apache-2.0 / 专有 等）：替换 `LICENSE` 正文，并同步 `README.md`、
`frontend/src/views/SettingsView.vue` 的「关于 → 授权」文案与本文件；
`frontend/src/views/__tests__/SettingsView.test.ts` 里有针对这段文案的断言，也要一起改。
