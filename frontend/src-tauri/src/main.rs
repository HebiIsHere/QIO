//! QIO desktop shell.
//!
//! Architecture: this Rust process is a thin shell. It launches the Python
//! backend (uvicorn + SSE) as a child process and renders the Vue UI in a
//! WebView. The frontend talks to the backend over localhost HTTP/SSE.
//!
//! - Debug builds: spawn the Python backend directly from the repo layout.
//! - Release builds: spawn the bundled sidecar (qio-backend).
//!
//! 本机 API 的身份与地址（见 backend/src/agent/api/auth.py）：
//! * 端口每次启动随机挑一个空闲端口，前端不再假设 8734（避免端口冲突与误连他人）；
//! * 后端自己生成 256-bit 会话令牌并写到一个用户私有的临时文件（不进命令行、
//!   不进日志），本壳只把文件路径读出来交给自己的 WebView；
//! * 前端通过 `qio_backend_info` 命令拿到 {port, token}，其它进程/网页拿不到。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{Manager, RunEvent};
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;

/// 后端连接信息：只在壳与自己的 WebView 之间传递。
#[derive(Clone)]
struct BackendState {
    port: u16,
    token_path: PathBuf,
}

#[derive(Serialize)]
struct BackendInfo {
    port: u16,
    token: String,
}

fn pick_free_port() -> u16 {
    // 让操作系统分配一个空闲端口；释放后立刻传给后端，冲突窗口极小。
    TcpListener::bind("127.0.0.1:0")
        .and_then(|listener| listener.local_addr())
        .map(|addr| addr.port())
        .unwrap_or(8734)
}

fn session_token_path() -> PathBuf {
    let mut path = std::env::temp_dir();
    path.push(format!("qio-session-{}.token", std::process::id()));
    path
}

/// 用户数据目录：与后端 Python 的默认值保持一致（`%APPDATA%\\qio`）。
///
/// 壳必须和后端算同一个目录，否则「内置模型复制到哪」和「后端去哪找模型」会对不上。
/// 环境变量 `QIO_DATA_DIR` 仍然优先（与后端一致，便于测试与隔离实例）。
fn data_dir() -> PathBuf {
    if let Ok(explicit) = std::env::var("QIO_DATA_DIR") {
        if !explicit.trim().is_empty() {
            return PathBuf::from(explicit);
        }
    }
    let base = std::env::var("APPDATA").unwrap_or_else(|_| {
        std::env::var("HOME").unwrap_or_else(|_| ".".to_string())
    });
    PathBuf::from(base).join("qio")
}

/// 内置模型的「内容指纹」：清单里每个文件的体积 + sha256。
///
/// 用它判断要不要重新复制：用户目录里的 `.ready` 与当前内置内容一致就直接跳过，
/// 升级换了模型（哈希变了）才重写一次。
fn model_fingerprint(manifest: &serde_json::Value) -> String {
    let mut parts: Vec<String> = Vec::new();
    if let Some(files) = manifest.get("files").and_then(|v| v.as_array()) {
        for entry in files {
            let name = entry.get("file").and_then(|v| v.as_str()).unwrap_or("");
            let bytes = entry.get("bytes").and_then(|v| v.as_u64()).unwrap_or(0);
            let sha = entry.get("sha256").and_then(|v| v.as_str()).unwrap_or("");
            if !name.is_empty() {
                parts.push(format!("{name}:{bytes}:{sha}"));
            }
        }
    }
    parts.push(format!(
        "default:{}",
        manifest.get("default").and_then(|v| v.as_str()).unwrap_or("")
    ));
    parts.join("|")
}

#[derive(Debug, PartialEq, Eq)]
enum SyncOutcome {
    /// 用户目录里已经是同一份内容（跳过复制）
    AlreadyPresent,
    /// 复制完成（首次安装或内置模型换版）
    Copied,
}

/// 把内置模型同步到用户数据目录：幂等、可重复调用。
///
/// 只复制清单里列出的文件，再加上 tokenizer 与许可证/声明；写入 `.ready` 标记
/// 记下内容指纹，避免每次启动都重算 90MB 的哈希。
fn sync_model_dir(bundled: &Path, target: &Path) -> Result<SyncOutcome, String> {
    let manifest_path = bundled.join("model_manifest.json");
    let raw = std::fs::read_to_string(&manifest_path)
        .map_err(|e| format!("读不到清单 {}：{e}", manifest_path.display()))?;
    let manifest: serde_json::Value =
        serde_json::from_str(&raw).map_err(|e| format!("清单不是合法 JSON：{e}"))?;
    let fingerprint = model_fingerprint(&manifest);
    if fingerprint.is_empty() {
        return Err("清单里没有可用的文件条目".to_string());
    }

    let ready_path = target.join(".ready");
    let ready_matches = std::fs::read_to_string(&ready_path)
        .map(|content| content.trim() == fingerprint)
        .unwrap_or(false);
    let files = model_files(&manifest);
    let all_present = files.iter().all(|name| target.join(name).exists());
    if ready_matches && all_present {
        return Ok(SyncOutcome::AlreadyPresent);
    }

    std::fs::create_dir_all(target)
        .map_err(|e| format!("建目录 {} 失败：{e}", target.display()))?;
    for name in files {
        let source = bundled.join(&name);
        if !source.exists() {
            return Err(format!("内置资源里缺少 {name}：{}", source.display()));
        }
        std::fs::copy(&source, target.join(&name))
            .map_err(|e| format!("复制 {name} 失败：{e}"))?;
    }
    for name in optional_files() {
        let source = bundled.join(name);
        if source.exists() {
            let _ = std::fs::copy(&source, target.join(name));
        }
    }
    std::fs::write(&ready_path, &fingerprint)
        .map_err(|e| format!("写标记文件失败：{e}"))?;
    Ok(SyncOutcome::Copied)
}

/// 需要一起复制的文件名：清单里的模型档 + tokenizer + 许可证/声明。
fn model_files(manifest: &serde_json::Value) -> Vec<String> {
    let mut names: Vec<String> = Vec::new();
    if let Some(files) = manifest.get("files").and_then(|v| v.as_array()) {
        for entry in files {
            if let Some(name) = entry.get("file").and_then(|v| v.as_str()) {
                names.push(name.to_string());
            }
        }
    }
    names.push("tokenizer.json".to_string());
    names.push("model_manifest.json".to_string());
    names
}

/// 可选文件：许可证与声明。有就一起带上；万一打包时漏了，
/// 也不该因此把「语义检索」整个关掉（那是分发包的义务，不是运行前提）。
fn optional_files() -> [&'static str; 2] {
    ["LICENSE-BAAI-bge-small-zh-v1.5.txt", "NOTICE.txt"]
}

/// 打包后的模型准备：把内置模型复制到用户数据目录，返回要交给后端的 `models` 目录。
///
/// 失败时返回 None（**不阻塞启动**）：后端会退回关键词检索，日志里能看到原因。
fn prepare_models(app: &tauri::AppHandle) -> Option<PathBuf> {
    let bundled_root = match app.path().resource_dir() {
        Ok(dir) => dir.join("models"),
        Err(err) => {
            eprintln!("[qio] 找不到资源目录，内置模型不可用：{err}");
            return None;
        }
    };
    let bundled = bundled_root.join("bge-small-zh-v1.5");
    if !bundled.join("model_manifest.json").exists() {
        eprintln!(
            "[qio] 安装包里没有内置模型（{}）：语义检索将退回关键词检索",
            bundled.display()
        );
        return None;
    }
    let models_dir = data_dir().join("models");
    let target = models_dir.join("bge-small-zh-v1.5");
    match sync_model_dir(&bundled, &target) {
        Ok(SyncOutcome::AlreadyPresent) => {
            eprintln!("[qio] 内置模型已就位：{}", target.display());
        }
        Ok(SyncOutcome::Copied) => {
            eprintln!("[qio] 内置模型已复制到：{}", target.display());
        }
        Err(err) => {
            eprintln!("[qio] 内置模型准备失败（语义检索退回关键词检索）：{err}");
            return None;
        }
    }
    Some(models_dir)
}

/// 等后端把令牌写出来（后端启动要几秒）。只在后台线程里阻塞。
fn read_token_file(path: &PathBuf, timeout: Duration) -> Option<String> {
    let deadline = Instant::now() + timeout;
    loop {
        if let Ok(raw) = std::fs::read_to_string(path) {
            let token = raw.trim().to_string();
            if !token.is_empty() {
                return Some(token);
            }
        }
        if Instant::now() >= deadline {
            return None;
        }
        std::thread::sleep(Duration::from_millis(120));
    }
}

/// 前端启动时调用：拿到本实例的后端端口与一次性会话令牌。
#[tauri::command]
async fn qio_backend_info(state: tauri::State<'_, Arc<BackendState>>) -> Result<BackendInfo, String> {
    let info = state.inner().clone();
    let path = info.token_path.clone();
    let token = tauri::async_runtime::spawn_blocking(move || {
        read_token_file(&path, Duration::from_secs(30))
    })
    .await
    .map_err(|err| format!("token read task failed: {err}"))?
    .ok_or_else(|| "backend did not publish a session token".to_string())?;
    Ok(BackendInfo { port: info.port, token })
}

fn backend_launch(
    app: &tauri::AppHandle,
    port: u16,
    token_path: &PathBuf,
    models_dir: Option<PathBuf>,
) -> Result<CommandChild, String> {
    let port = port.to_string();
    let token_path = token_path.to_string_lossy().to_string();
    let user_data_dir = data_dir().to_string_lossy().to_string();
    let models_env = models_dir.map(|dir| dir.to_string_lossy().to_string());
    if cfg!(debug_assertions) {
        // Repo layout: frontend/src-tauri -> ../../backend
        let backend_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..")
            .join("backend");
        let python = std::env::var("PYTHON").unwrap_or_else(|_| "python".to_string());
        let (mut rx, child) = app
            .shell()
            .command(python)
            .args([
                "-m",
                "uvicorn",
                "agent.main:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                port.as_str(),
                "--log-level",
                "warning",
            ])
            .current_dir(backend_dir.clone())
            .env("PYTHONPATH", backend_dir.join("src"))
            .env("QIO_PORT", port.clone())
            .env("QIO_SESSION_TOKEN_FILE", token_path.clone())
            // 数据目录显式传入：壳与后端必须算同一个目录（内置模型就放在它下面）
            .env("QIO_DATA_DIR", user_data_dir.clone())
            .envs(models_env.clone().map(|dir| ("QIO_MODELS_DIR", dir)))
            .spawn()
            .map_err(|e| format!("failed to start backend: {e}"))?;
        tauri::async_runtime::spawn(async move {
            while rx.recv().await.is_some() {}
        });
        Ok(child)
    } else {
        let (mut rx, child) = app
            .shell()
            .sidecar("qio-backend")
            .map_err(|e| format!("sidecar setup failed: {e}"))?
            .env("QIO_HOST", "127.0.0.1")
            .env("QIO_PORT", port.clone())
            .env("QIO_SESSION_TOKEN_FILE", token_path.clone())
            .env("QIO_DATA_DIR", user_data_dir.clone())
            .envs(models_env.clone().map(|dir| ("QIO_MODELS_DIR", dir)))
            .spawn()
            .map_err(|e| format!("sidecar spawn failed: {e}"))?;
        tauri::async_runtime::spawn(async move {
            while rx.recv().await.is_some() {}
        });
        Ok(child)
    }
}

fn main() {
    let backend: Arc<Mutex<Option<CommandChild>>> = Arc::new(Mutex::new(None));
    let backend_for_setup = Arc::clone(&backend);

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(move |app| {
            let port = pick_free_port();
            let token_path = session_token_path();
            // 清掉上一轮的残留文件：令牌绝不跨进程生命周期复用。
            let _ = std::fs::remove_file(&token_path);
            app.manage(Arc::new(BackendState {
                port,
                token_path: token_path.clone(),
            }));
            // 内置模型：复制到用户数据目录，并把目录交给后端（失败不阻塞启动）
            let models_dir = prepare_models(app.handle());
            let child = backend_launch(app.handle(), port, &token_path, models_dir)?;
            *backend_for_setup.lock().unwrap() = Some(child);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![qio_backend_info])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(move |_app, event| match event {
            RunEvent::Exit | RunEvent::ExitRequested { .. } => {
                if let Some(child) = backend.lock().unwrap().take() {
                    let _ = child.kill();
                }
                let _ = std::fs::remove_file(session_token_path());
            }
            _ => {}
        });
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn temp_dir(name: &str) -> PathBuf {
        let mut dir = std::env::temp_dir();
        dir.push(format!("qio-model-sync-{}-{}", name, std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn write_bundle(bundled: &Path, sha: &str) -> String {
        fs::create_dir_all(bundled).unwrap();
        fs::write(bundled.join("model.onnx"), b"fake-onnx-bytes").unwrap();
        fs::write(bundled.join("tokenizer.json"), b"{}").unwrap();
        fs::write(bundled.join("NOTICE.txt"), b"notice").unwrap();
        let manifest = serde_json::json!({
            "name": "bge-small-zh-v1.5",
            "default": "model.onnx",
            "files": [
                {"file": "model.onnx", "precision": "fp32", "bytes": 15, "sha256": sha}
            ]
        });
        let fingerprint = model_fingerprint(&manifest);
        fs::write(
            bundled.join("model_manifest.json"),
            serde_json::to_string(&manifest).unwrap(),
        )
        .unwrap();
        fingerprint
    }

    #[test]
    fn copies_bundle_into_data_dir_and_skips_second_time() {
        let root = temp_dir("copy");
        let bundled = root.join("bundled");
        let target = root.join("data").join("models").join("bge-small-zh-v1.5");
        let fingerprint = write_bundle(&bundled, "abc123");

        assert_eq!(sync_model_dir(&bundled, &target).unwrap(), SyncOutcome::Copied);
        assert!(target.join("model.onnx").exists());
        assert!(target.join("tokenizer.json").exists());
        // 许可证/声明是可选的：这里没放，也必须复制成功（不该因为它禁用语义检索）
        assert!(target.join("model_manifest.json").exists());
        assert_eq!(
            fs::read_to_string(target.join(".ready")).unwrap().trim(),
            fingerprint
        );

        // 第二次：内容一致 → 跳过复制（幂等）
        assert_eq!(
            sync_model_dir(&bundled, &target).unwrap(),
            SyncOutcome::AlreadyPresent
        );
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn refreshes_when_the_bundled_model_changes() {
        let root = temp_dir("refresh");
        let bundled = root.join("bundled");
        let target = root.join("data").join("models").join("bge-small-zh-v1.5");
        write_bundle(&bundled, "old-hash");
        sync_model_dir(&bundled, &target).unwrap();

        // 升级换了模型（哈希变了）→ 必须重写
        write_bundle(&bundled, "new-hash");
        assert_eq!(sync_model_dir(&bundled, &target).unwrap(), SyncOutcome::Copied);
        assert_eq!(
            fs::read_to_string(target.join(".ready")).unwrap().trim(),
            model_fingerprint(
                &serde_json::from_str(&fs::read_to_string(bundled.join("model_manifest.json")).unwrap())
                    .unwrap()
            )
        );
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn missing_manifest_is_an_error_not_a_silent_success() {
        let root = temp_dir("missing");
        let bundled = root.join("bundled");
        fs::create_dir_all(&bundled).unwrap();
        let target = root.join("data").join("models").join("bge-small-zh-v1.5");

        assert!(sync_model_dir(&bundled, &target).is_err());
        assert!(!target.join(".ready").exists());
        let _ = fs::remove_dir_all(&root);
    }
}
