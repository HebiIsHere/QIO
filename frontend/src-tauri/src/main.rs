//! QIO desktop shell.
//!
//! Architecture: this Rust process is a thin shell. It launches the Python
//! backend (uvicorn + SSE) as a child process and renders the Vue UI in a
//! WebView. The frontend talks to the backend over localhost HTTP/SSE.
//!
//! - Debug builds: spawn the Python backend directly from the repo layout.
//! - Release builds: spawn the bundled sidecar (qio-backend).

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use tauri::RunEvent;
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;

fn backend_launch(app: &tauri::AppHandle) -> Result<CommandChild, String> {
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
                "8734",
                "--log-level",
                "warning",
            ])
            .current_dir(backend_dir.clone())
            .env("PYTHONPATH", backend_dir.join("src"))
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
            let child = backend_launch(app.handle())?;
            *backend_for_setup.lock().unwrap() = Some(child);
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(move |_app, event| match event {
            RunEvent::Exit | RunEvent::ExitRequested { .. } => {
                if let Some(child) = backend.lock().unwrap().take() {
                    let _ = child.kill();
                }
            }
            _ => {}
        });
}