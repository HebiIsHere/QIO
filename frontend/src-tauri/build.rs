//! Tauri 构建脚本。
//!
//! 为什么除了 `tauri_build::build()` 还要补占位文件：
//! `tauri.conf.json` 的 `bundle.resources` 里列了两项**构建产物**：
//!   * `resources/python-runtime`     —— 安装包自带的 Python 运行时（scripts/build_runtime.ps1 生成）
//!   * `resources/qio-uninstall-helper.exe` —— 卸载帮助程序（scripts/build_uninstall_helper.ps1 生成）
//! 它们都不入库（.gitignore 里的 `frontend/src-tauri/resources/`）。而 tauri-build 在**编译期**
//! 就会校验每个资源路径存在 —— 干净检出上直接失败：
//!
//!     resource path `resources/qio-uninstall-helper.exe` doesn't exist
//!     error: failed to run custom build command for `qio`
//!
//! 2026-10-03 实测：CI 的 rust 任务与 install e2e 任务都因此变红（cargo check / cargo build 101）。
//! 所以这里先补**占位**（目录 / 空文件），让编译期校验过得去；真正的产物由上面两个脚本在
//! `tauri build` 之前写进去，覆盖占位。`scripts/build_installer.ps1` 还会在打包前硬校验
//! 「这两个是真产物、不是占位」，避免把占位打进安装包。

use std::path::Path;

/// 资源目录（与 build.rs 同级的 src-tauri/resources）。
fn resources_dir() -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("resources")
}

fn ensure_dir(path: &Path) {
    if !path.exists() {
        // 失败不 panic：让 tauri-build 给出它自己的报错（信息更完整）。
        let _ = std::fs::create_dir_all(path);
    }
}

fn ensure_file(path: &Path) {
    if !path.exists() {
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let _ = std::fs::write(path, b"");
    }
}

fn ensure_build_placeholders() {
    let resources = resources_dir();
    // 自带 Python 运行时是个目录：存在即可通过校验（真产物由 build_runtime.ps1 铺进去）。
    ensure_dir(&resources.join("python-runtime"));
    // 卸载帮助程序是单个可执行文件：空文件占位。
    ensure_file(&resources.join("qio-uninstall-helper.exe"));
}

fn main() {
    ensure_build_placeholders();
    tauri_build::build()
}
