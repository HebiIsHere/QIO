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
/// 解析 Windows 的「系统代理」设置（浏览器读的就是这一份）。
///
/// 为什么需要它：更新用的 HTTP 客户端只认环境变量，**不读** Windows 系统代理；而绝大多数
/// VPN（Clash / v2rayN / Shadowsocks / 脉动VPN 等）在"系统代理模式"下只写这份设置。
/// 读它 → 用户开了 VPN 就能自动跟上，关掉就自动回到直连，不需要任何配置。
///
/// 用 `reg query` 而不是引入注册表库：少一个依赖，输出格式稳定，解析失败就当"没有"。
/// 自动配置脚本（PAC）不在这里处理：无法在不解释脚本的前提下判断该走哪个代理，
/// 这种情况如实记为"未使用代理"，并写进日志。
fn system_proxy() -> Option<String> {
    #[cfg(not(windows))]
    {
        return None;
    }
    #[cfg(windows)]
    {
        const KEY: &str = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings";
        let query = |name: &str| -> Option<String> {
            let out = std::process::Command::new("reg")
                .args(["query", KEY, "/v", name])
                .output()
                .ok()?;
            let text = String::from_utf8_lossy(&out.stdout).to_string();
            let line = text.lines().find(|l| l.contains("REG_SZ") || l.contains("REG_DWORD"))?;
            let value = line.split_once("REG_SZ").map(|(_, v)| v)
                .or_else(|| line.split_once("REG_DWORD").map(|(_, v)| v))?;
            Some(value.trim().to_string())
        };
        let enabled = query("ProxyEnable")
            .map(|v| v.trim_start_matches("0x").trim() == "1" || v == "1")
            .unwrap_or(false);
        if !enabled {
            return None;
        }
        let raw = query("ProxyServer")?;
        pick_proxy_from_windows_value(&raw)
    }
}

/// Windows 的 ProxyServer 可能是 `host:port`，也可能是
/// `http=h:p;https=h:p;socks=h:p`。我们要发的是 HTTPS 请求，取值优先 https → http → 单个地址。
/// socks 需要客户端支持 socks，这里不当作可用（宁可直连，也不给一个用不了的值）。
fn pick_proxy_from_windows_value(raw: &str) -> Option<String> {
    let raw = raw.trim();
    if raw.is_empty() {
        return None;
    }
    let mut https = None;
    let mut http = None;
    let mut single = None;
    for part in raw.split(';') {
        let part = part.trim();
        if part.is_empty() {
            continue;
        }
        match part.split_once('=') {
            Some((scheme, value)) => match scheme.trim().to_ascii_lowercase().as_str() {
                "https" => https = Some(value.trim().to_string()),
                "http" => http = Some(value.trim().to_string()),
                "socks" | "socks5" => {}
                _ => {}
            },
            None => single = Some(part.to_string()),
        }
    }
    let chosen = https.or(http).or(single)?;
    if chosen.is_empty() {
        return None;
    }
    if chosen.contains("://") {
        Some(chosen)
    } else {
        Some(format!("http://{chosen}"))
    }
}

/// 用之前先确认这个代理地址真的有人监听（0.5 秒上限）。
///
/// 动机（2026-09-22 真实故障）：系统里留着一个指向"已经关掉的代理"的地址时，
/// 客户端不会自己发现，只会一直等 —— 表现就是"卡住"或"网络错误"。
/// 这里用一次极短的 TCP 连接判断可达性：不可达就不使用它，改为直连。
fn proxy_is_reachable(proxy: &str) -> bool {
    use std::net::{TcpStream, ToSocketAddrs};
    use std::time::Duration;

    let without_scheme = proxy
        .split_once("://")
        .map(|(_, rest)| rest)
        .unwrap_or(proxy)
        .trim_end_matches('/');
    let host_port = without_scheme.split('/').next().unwrap_or(without_scheme);
    if host_port.is_empty() || host_port.starts_with(':') {
        return false; // 空主机名不是地址（":80" 会被系统解析成任意地址，不能当可达）
    }
    let host_port = if host_port.contains(':') {
        host_port.to_string()
    } else if proxy.starts_with("https://") {
        format!("{host_port}:443")
    } else {
        format!("{host_port}:80")
    };
    let Ok(addrs) = host_port.to_socket_addrs() else {
        return false;
    };
    for addr in addrs {
        if TcpStream::connect_timeout(&addr, Duration::from_millis(500)).is_ok() {
            return true;
        }
    }
    false
}

/// 决定这次更新走哪个代理，并按需要写入/清除进程内的代理环境变量。
///
/// 顺序（从"最明确"到"最省事"）：
///   1. 环境变量 —— 用户或别的程序显式设置的；
///   2. `%APPDATA%\qio\updater-proxy.txt` —— 用户为 QIO 单独写的；
///   3. Windows 系统代理 —— VPN 开的"系统代理"就是这一份，零配置跟上；
///   4. 都不行 → 直连（TUN/全局模式的 VPN 本来就走这条）。
///
/// 前三种在采用之前都会做一次连通测试：不可达就不用（并清掉进程内的代理变量，避免
/// 一个失效的地址让更新一直等）。每次检查更新前都会重新调用，所以用户开关 VPN 后
/// 不需要重启 QIO。
fn apply_updater_proxy() -> Option<String> {
    let from_env = std::env::var("HTTPS_PROXY")
        .or_else(|_| std::env::var("https_proxy"))
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty());
    let from_file = std::fs::read_to_string(data_dir().join("updater-proxy.txt"))
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty());
    let from_system = system_proxy();

    let mut candidates: Vec<(&str, String)> = Vec::new();
    if let Some(v) = from_env {
        candidates.push(("环境变量", v));
    }
    if let Some(v) = from_file {
        candidates.push(("updater-proxy.txt", v));
    }
    if let Some(v) = from_system {
        candidates.push(("Windows 系统代理", v));
    }

    for (source, proxy) in candidates {
        if proxy_is_reachable(&proxy) {
            std::env::set_var("HTTPS_PROXY", &proxy);
            std::env::set_var("http_proxy", &proxy);
            std::env::set_var("HTTP_PROXY", &proxy);
            std::env::set_var("ALL_PROXY", &proxy);
            log::info!("[qio] 更新走代理（来源：{source}）：{proxy}");
            return Some(proxy);
        }
        log::warn!("[qio] 代理不可达，跳过（来源：{source}）：{proxy}");
    }

    // 一条都不可用：清掉进程内的代理变量，确保更新走直连而不是去连一个死地址。
    for name in ["HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"] {
        std::env::remove_var(name);
    }
    log::info!("[qio] 本次更新不使用代理（直连）");
    None
}

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

/// 更新前调用：把后端进程**整棵树**结束掉。
///
/// 为什么必须有这一步（2026-09-22 实测）：后端是 PyInstaller 单文件程序，它自己会再起一个
/// 子进程跑真正的服务。只杀父进程会留下子进程继续占着 `qio-backend.exe`，
/// 于是 NSIS 安装器报 `Can't write: ...\qio-backend.exe` 并中止安装 —— 用户看到的就是
/// 「安装失败」，实际是文件被残留进程锁住。`taskkill /T` 会连同子进程一起结束。
#[tauri::command]
fn qio_prepare_for_update(
    backend: tauri::State<'_, Arc<Mutex<Option<CommandChild>>>>,
) -> Result<bool, String> {
    let pid = {
        let guard = backend.lock().map_err(|e| e.to_string())?;
        guard.as_ref().map(|child| child.pid())
    };
    let Some(pid) = pid else { return Ok(false) };
    let pid_arg = pid.to_string();
    let status = std::process::Command::new("taskkill")
        .args(["/PID", pid_arg.as_str(), "/T", "/F"])
        .status();
    log::info!("[qio] 更新前结束后端进程树 pid={pid} status={status:?}");
    // 结束后端不影响返回值：拿不到 pid 或 taskkill 失败也要继续（安装器会自己再试一次）
    Ok(true)
}

/// 前端每次检查更新之前调用：按当前网络环境重新判断该不该走代理。
///
/// 为什么由前端触发而不是只在启动时判断一次：VPN 是随时开关的。启动时缓存住判断结果，
/// 用户在开会话期间开了 VPN（或关掉）就失效了。每次检查前重算一遍，用户就不用重启 QIO。
#[tauri::command]
fn qio_refresh_updater_proxy() -> Result<Option<String>, String> {
    Ok(apply_updater_proxy())
}

/// Windows Job Object：把后端放进「job 关闭即终止」的 job。
///
/// 这是孤儿进程问题的根治手段（2026-09-22 实测：壳退出只杀父进程，PyInstaller 的子进程
/// 会留下来锁住 `qio-backend.exe`，安装器因此报 `Can't write` 并中止）。
/// Job 句柄由壳持有：壳正常退出、被强杀、被安装器结束 —— 只要句柄随进程消失，
/// 系统就会连带终止 job 里的所有进程，包括那个子进程。
#[cfg(windows)]
mod backend_job {
    use std::ffi::c_void;
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows_sys::Win32::System::Threading::{OpenProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE};

    /// 持有 job 句柄（进程活着句柄就活着；句柄关闭 = 系统杀掉 job 内所有进程）
    pub struct BackendJob(HANDLE);

    // 句柄只在主线程创建、进程生命周期内一直存在
    unsafe impl Send for BackendJob {}
    unsafe impl Sync for BackendJob {}

    pub fn create() -> Option<BackendJob> {
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() {
                log::warn!("[qio] 建 Job Object 失败，退回 taskkill 兜底");
                return None;
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            ) == 0
            {
                log::warn!("[qio] 设置 Job Object 失败，退回 taskkill 兜底");
                CloseHandle(job);
                return None;
            }
            Some(BackendJob(job))
        }
    }

    impl BackendJob {
        /// 把某个 pid 放进 job。失败不是致命的：退出时仍有 taskkill 兜底。
        pub fn assign(&self, pid: u32) -> bool {
            unsafe {
                let process = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, pid);
                if process.is_null() {
                    log::warn!("[qio] OpenProcess({pid}) 失败，job 未生效");
                    return false;
                }
                let ok = AssignProcessToJobObject(self.0, process) != 0;
                CloseHandle(process);
                if !ok {
                    log::warn!("[qio] 把 pid {pid} 放进 job 失败（可能已在别的 job 里）");
                }
                ok
            }
        }
    }
}

#[cfg(not(windows))]
mod backend_job {
    pub struct BackendJob;
    pub fn create() -> Option<BackendJob> {
        None
    }
    impl BackendJob {
        pub fn assign(&self, _pid: u32) -> bool {
            false
        }
    }
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
    // 后端进程随这份 job 的生死而生死：壳没了，系统负责清干净，不留孤儿。
    let backend_job = backend_job::create();

    tauri::Builder::default()
        // 日志最先注册：这样 updater 等插件的 log::error! 才会落到文件里
        // （%APPDATA%\qio\logs\qio.log），出问题时能看见真实原因而不是一句固定文案。
        .plugin(
            tauri_plugin_log::Builder::new()
                .target(tauri_plugin_log::Target::new(
                    tauri_plugin_log::TargetKind::LogDir {
                        file_name: Some("qio".to_string()),
                    },
                ))
                .level(log::LevelFilter::Info)
                .build(),
        )
        .plugin(tauri_plugin_shell::init())
        // 应用内更新：updater（检查/下载/校验/安装）+ process（装完重启）。
        // 两者都在 Rust 侧工作，前端只通过插件 API 驱动，不需要放宽 CSP。
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .setup(move |app| {
            if let Some(proxy) = apply_updater_proxy() {
                log::info!("[qio] 更新走代理：{proxy}");
            } else {
                log::info!("[qio] 更新未配置代理（可用 %APPDATA%\\qio\\updater-proxy.txt 指定）");
            }
            let port = pick_free_port();
            let token_path = session_token_path();
            // 清掉上一轮的残留文件：令牌绝不跨进程生命周期复用。
            let _ = std::fs::remove_file(&token_path);
            app.manage(Arc::new(BackendState {
                port,
                token_path: token_path.clone(),
            }));
            // 让 qio_prepare_for_update 能拿到 sidecar 的 pid
            app.manage(Arc::clone(&backend_for_setup));
            // 内置模型：复制到用户数据目录，并把目录交给后端（失败不阻塞启动）
            let models_dir = prepare_models(app.handle());
            let child = backend_launch(app.handle(), port, &token_path, models_dir)?;
            if let Some(job) = backend_job.as_ref() {
                if job.assign(child.pid()) {
                    log::info!("[qio] 后端 pid {} 已纳入 job（随壳退出自动终止）", child.pid());
                }
            }
            *backend_for_setup.lock().unwrap() = Some(child);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            qio_backend_info,
            qio_prepare_for_update,
            qio_refresh_updater_proxy
        ])
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

    #[test]
    fn picks_https_then_http_from_windows_proxy_value() {
        // 多段形式：优先 https
        assert_eq!(
            pick_proxy_from_windows_value("http=127.0.0.1:17011;https=127.0.0.1:17012").as_deref(),
            Some("http://127.0.0.1:17012")
        );
        // 只有 http 段
        assert_eq!(
            pick_proxy_from_windows_value("http=127.0.0.1:7890").as_deref(),
            Some("http://127.0.0.1:7890")
        );
        // 单地址形式（不带协议）自动补 http://
        assert_eq!(
            pick_proxy_from_windows_value("127.0.0.1:8080").as_deref(),
            Some("http://127.0.0.1:8080")
        );
        // 已带协议则原样保留
        assert_eq!(
            pick_proxy_from_windows_value("http://127.0.0.1:8080").as_deref(),
            Some("http://127.0.0.1:8080")
        );
    }

    #[test]
    fn ignores_socks_only_and_empty_values() {
        // 只给了 socks：不当作可用（客户端不支持时不猜），也不会拼出一个错地址
        assert_eq!(pick_proxy_from_windows_value("socks=127.0.0.1:1080"), None);
        assert_eq!(pick_proxy_from_windows_value("   "), None);
    }

    #[test]
    fn proxy_reachability_detects_dead_port() {
        // 本机上没人监听的端口：必须判为不可达，否则会"一直等"
        assert!(!proxy_is_reachable("http://127.0.0.1:17011"));
        // 语法都不成立的地址同样不可达
        assert!(!proxy_is_reachable("http://"));
    }
}
