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
use std::path::PathBuf;
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
) -> Result<CommandChild, String> {
    let port = port.to_string();
    let token_path = token_path.to_string_lossy().to_string();
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
            let child = backend_launch(app.handle(), port, &token_path)?;
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
