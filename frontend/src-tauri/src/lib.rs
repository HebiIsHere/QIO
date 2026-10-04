//! QIO 桌面外壳的共享库：只承载「两个二进制必须用同一份实现」的逻辑。
//!
//! 为什么要有它（2026-10-03 A 项）：sidecar 的所有权判定（读 lease、验进程身份、
//! 只按 pid 结束）同时被两个二进制使用 ——
//!
//! * qio（src/main.rs，外壳）：启动时写本实例的 sidecar.lease.<pid>.json（外加一份旧版
//!   单文件名兼容镜像）、退出时只删自己那一份；
//! * qio-uninstall-helper（src/bin/qio-uninstall-helper.rs，卸载器调用的帮助程序）：
//!   扫安装目录下**所有**实例的记录、逐个验身份、只收本安装实例自己的进程。
//!
//! 这两处一旦漂移，后果就是「卸载杀错实例」或「该杀的没杀掉」。所以是 lib 化共享，
//! **不是**两份复制粘贴（Cargo.toml 里 lib 名 = qio_core，避开主程序 bin 名 qio）。
//! 主程序仍把 Tauri 应用放在 bin 里（不是 Tauri v2 常见的 lib 布局），这里不碰 Tauri。

pub mod ownership;
