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
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use qio_core::ownership;
// 跑系统命令的硬超时与「按 pid 结束整棵树」现在住在共享库里（src/ownership.rs）：
// 卸载帮助程序要用**同一套**语义，不许出现第二份实现。
use qio_core::ownership::run_command_with_timeout;
// 按 pid 结束整棵树只在 Windows 的退出/兜底路径上用（非 Windows 没有 job，也没有 taskkill 语义）。
#[cfg(windows)]
use qio_core::ownership::kill_tree_by_pid;
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

/// 安装包自带的 Python 运行时目录（资源目录下的 `python-runtime`）。
///
/// 为什么需要它：冻结后的后端 `sys.executable` 是 qio-backend.exe，**不能**当解释器用；
/// 而"用户自己装一个 Python 3.11"是产品级缺陷（绝大多数 Windows 机器上没有）。
/// 所以安装包里带一份运行时，外壳把**目录**交给后端（`QIO_BUNDLED_PYTHON_DIR`），
/// 由 backend/src/agent/tools/tool_envs.py 解析成解释器并校验版本与后端一致。
///
/// 开发态（debug 构建）通常没有这份资源：这时返回 None，后端行为与以前完全一样
/// （用后端自己的解释器）。**不猜路径**：目录里没有 python.exe 就当作没有，
/// 免得给后端一个"看起来存在、其实不可用"的目录。
fn bundled_python_dir(app: &tauri::AppHandle) -> Option<PathBuf> {
    let dir = match app.path().resource_dir() {
        Ok(root) => root.join("python-runtime"),
        Err(err) => {
            log::warn!("[qio] 找不到资源目录，自带 Python 运行时不可用：{err}");
            return None;
        }
    };
    let exe = dir.join(if cfg!(windows) { "python.exe" } else { "python3" });
    if !exe.is_file() {
        log::info!(
            "[qio] 安装包里没有自带 Python 运行时（{}）：依赖型工具会退回本机解释器或给出可行动的失败",
            dir.display()
        );
        return None;
    }
    log::info!("[qio] 自带 Python 运行时：{}", exe.display());
    Some(dir)
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
// run_command_with_timeout 已挪到 qio_core::ownership（帮助程序也要用同一份实现），
// 本文件通过 use qio_core::ownership::run_command_with_timeout 使用它。

/// 读系统代理的单次超时。
///
/// 为什么必须有超时：`Command::output()` 会一直等子进程退出。2026-09-24 实测 ——
/// `reg.exe` 报 0xC0000142（DLL 初始化失败）并弹出"必须先点掉"的模态框，子进程
/// 因此不退出，外壳启动流程被钉住 1 分 42 秒，用户看到的就是一直白屏。
/// 超时后按"拿不到系统代理"处理 → 直接直连（更新本身就有直连兜底）。
const SYSTEM_PROXY_QUERY_TIMEOUT: Duration = Duration::from_secs(3);

fn system_proxy() -> Option<String> {
    #[cfg(not(windows))]
    {
        return None;
    }
    #[cfg(windows)]
    {
        const KEY: &str = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings";
        let query = |name: &str| -> Option<String> {
            let mut cmd = std::process::Command::new("reg");
            cmd.args(["query", KEY, "/v", name]);
            let Some(out) = run_command_with_timeout(&mut cmd, SYSTEM_PROXY_QUERY_TIMEOUT) else {
                log::warn!("[qio] 读系统代理失败或超时（{name}），按直连处理");
                return None;
            };
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
    let mut cmd = std::process::Command::new("taskkill");
    cmd.args(["/PID", pid_arg.as_str(), "/T", "/F"]);
    // 同样要硬超时：taskkill 卡住会拖住更新流程（实测同一台机器上它也报过 0xC0000142）
    let status = match run_command_with_timeout(&mut cmd, Duration::from_secs(5)) {
        Some(out) => format!("{:?}", out.status),
        None => "超时或启动失败（跳过）".to_string(),
    };
    log::info!("[qio] 更新前结束后端进程树 pid={pid} status={status}");
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

// kill_tree_by_pid 已挪到 qio_core::ownership（卸载帮助程序也要用同一份实现）：
// 只按 pid 杀我们自己拉起的这棵树（taskkill /PID <pid> /T /F），绝不按进程名批量杀 ——
// 开发实例、测试实例、其它安装实例都不能被误伤。

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
        SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
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

        /// 立刻结束 job 里的所有进程（不是关句柄那种"随壳退出"的被动收）。
        ///
        /// fail-closed 用：归属记录写不下来时，绝不允许留下「后台还在跑、但卸载器
        /// 无法确认归属」的状态 —— 那条路的终点是安装目录删不干净。
        pub fn terminate(&self) -> bool {
            unsafe { TerminateJobObject(self.0, 1) != 0 }
        }
    }

    #[cfg(test)]
    mod tests {
        //! Job Object 语义的回归测试（真进程，不是 mock）。
        //!
        //! 守的是两件事：
        //! 1. 「指派之后创建的后代自动进 job」—— onefile 的 child 靠的就是这条；
        //! 2. 「先有后代再指派，后代收不到」—— 这条反证把 main.rs 里 assign 的**时序**
        //!    钉死：谁把 assign 挪到 child 创建之后，测试就会红。

        use super::*;
        use std::process::{Command, Stdio};
        use std::time::{Duration, Instant};
        use windows_sys::Win32::Foundation::{CloseHandle, STILL_ACTIVE};
        use windows_sys::Win32::System::JobObjects::{
            JobObjectBasicProcessIdList, QueryInformationJobObject,
        };
        use windows_sys::Win32::System::Threading::{
            GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
        };

        const MAX_PIDS: usize = 64;

        #[repr(C)]
        struct PidList {
            assigned: u32,
            count: u32,
            pids: [usize; MAX_PIDS],
        }

        fn job_pids(job: &BackendJob) -> Vec<u32> {
            let mut list = PidList { assigned: 0, count: 0, pids: [0; MAX_PIDS] };
            let ok = unsafe {
                QueryInformationJobObject(
                    job.0,
                    JobObjectBasicProcessIdList,
                    &mut list as *mut PidList as *mut core::ffi::c_void,
                    std::mem::size_of::<PidList>() as u32,
                    std::ptr::null_mut(),
                )
            };
            assert_ne!(ok, 0, "QueryInformationJobObject 失败");
            list.pids[..list.count as usize].iter().map(|p| *p as u32).collect()
        }

        /// 进程是否还活着。
        ///
        /// 不能只看 OpenProcess 成不成功：测试自己持有 Child 句柄时，进程对象在退出后
        /// 仍然存在，OpenProcess 照样成功 —— 那会把「已经死了」判成「还活着」。
        /// 所以要看退出码是不是 STILL_ACTIVE。
        fn process_alive(pid: u32) -> bool {
            unsafe {
                let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
                if handle.is_null() {
                    return false;
                }
                let mut code: u32 = 0;
                let ok = GetExitCodeProcess(handle, &mut code);
                CloseHandle(handle);
                ok != 0 && code == STILL_ACTIVE as u32
            }
        }

        fn spawn_spawner(seconds: u32) -> std::process::Child {
            // cmd 先等一秒再起 ping：模拟 onefile「launcher 先起、child 后到」
            let script = format!("ping -n 2 127.0.0.1 >nul & ping -n {seconds} 127.0.0.1");
            Command::new("cmd")
                .args(["/c", script.as_str()])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .expect("拉起测试子进程失败")
        }

        #[test]
        fn descendants_created_after_assignment_join_the_job_and_die_with_it() {
            let job = create().expect("CreateJobObjectW 失败");
            let mut spawner = spawn_spawner(30);
            assert!(job.assign(spawner.id()), "assign 失败");

            let deadline = Instant::now() + Duration::from_secs(20);
            let mut pids = job_pids(&job);
            while pids.len() < 2 && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(200));
                pids = job_pids(&job);
            }
            assert!(pids.len() >= 2, "指派之后创建的后代没有进入 job：{pids:?}");
            let grandchild = *pids.iter().find(|pid| **pid != spawner.id()).expect("没有后代");

            // 关掉 job 句柄 = 壳退出（正常退出/被强杀/被安装器结束都一样）
            unsafe { CloseHandle(job.0) };
            let deadline = Instant::now() + Duration::from_secs(20);
            while process_alive(grandchild) && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(200));
            }
            assert!(!process_alive(grandchild), "job 关闭后后代还活着（孤儿）");
            let _ = spawner.kill();
            let _ = spawner.wait();
        }

        #[test]
        fn descendants_created_before_assignment_are_not_captured() {
            // 反证：先让后代跑起来，再 assign —— 后代不在 job 里。
            // 这条失败通常意味着「assign 被挪到 child 创建之后」，那正是孤儿问题复发。
            let mut spawner = spawn_spawner(5);
            std::thread::sleep(Duration::from_secs(3));
            let job = create().expect("CreateJobObjectW 失败");
            assert!(job.assign(spawner.id()), "assign 失败");
            let pids = job_pids(&job);
            assert_eq!(pids, vec![spawner.id()], "先存在的后代不应被收容：{pids:?}");
            let _ = spawner.kill();
            let _ = spawner.wait();
        }

        #[test]
        fn assign_reports_failure_for_an_unknown_pid() {
            let job = create().expect("CreateJobObjectW 失败");
            assert!(!job.assign(0xFFFF_FFF0), "不存在的 pid 不该报成功");
        }

        #[test]
        fn kill_tree_by_pid_ends_the_whole_tree() {
            // job 没生效时的兜底路径：只按 pid 杀整棵树（绝不按进程名）
            let mut spawner = spawn_spawner(30);
            let pid = spawner.id();
            std::thread::sleep(Duration::from_secs(2));
            assert!(crate::kill_tree_by_pid(pid), "taskkill /T 应返回成功");
            let deadline = Instant::now() + Duration::from_secs(15);
            while process_alive(pid) && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(200));
            }
            assert!(!process_alive(pid), "按 pid 的 taskkill 没有结束这棵树");
            let _ = spawner.wait();
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

        /// 非 Windows 没有 job 语义（`create()` 返回 None，调用方拿不到实例）。
        /// 这里保留同签名只是为了跨平台编译：fail-closed 的收尾在那边走按 pid 结束进程树。
        pub fn terminate(&self) -> bool {
            false
        }
    }
}

fn backend_launch(
    app: &tauri::AppHandle,
    port: u16,
    token_path: &PathBuf,
    models_dir: Option<PathBuf>,
    python_dir: Option<PathBuf>,
) -> Result<CommandChild, String> {
    let port = port.to_string();
    let token_path = token_path.to_string_lossy().to_string();
    let user_data_dir = data_dir().to_string_lossy().to_string();
    let models_env = models_dir.map(|dir| dir.to_string_lossy().to_string());
    // 自带 Python 运行时：只在**真的存在**时传，避免给后端一个不可用的目录。
    // 后端只认 QIO_PYTHON（显式指定，优先级最高）→ QIO_BUNDLED_PYTHON_DIR → 本机解释器。
    let python_env = python_dir.map(|dir| dir.to_string_lossy().to_string());
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
            .envs(python_env.clone().map(|dir| ("QIO_BUNDLED_PYTHON_DIR", dir)))
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
            .envs(python_env.clone().map(|dir| ("QIO_BUNDLED_PYTHON_DIR", dir)))
            .spawn()
            .map_err(|e| format!("sidecar spawn failed: {e}"))?;
        tauri::async_runtime::spawn(async move {
            while rx.recv().await.is_some() {}
        });
        Ok(child)
    }
}

/// 写「本安装实例」的所有权记录（安装目录 = 外壳 exe 所在目录）。
///
/// 只有 release 构建（安装态）才写：开发态的实例不能成为某个安装实例的清理目标
/// （计划 1.1 的硬约束）。写失败**不阻塞启动**：宁可没有记录（卸载器就"宁可不杀"），
/// 也不要让应用起不来。
///
/// shell_pid 传的是外壳**主进程自己**（std::process::id()），创建时间由
/// ownership 用 GetProcessTimes 读出来 —— 不是任何子进程的创建时间。
/// 启动期致命问题（目前只有一种：写不下归属记录）的**稳定标记**。
/// 卸载验证脚本按这一行 grep 原因；不要改它，改了要同步改脚本与文档。
const LEASE_ERROR_MARKER: &str = "QIO-LEASE-WRITE-FAILED";

/// 把启动期致命原因写到**不依赖日志插件**的地方：数据目录 → 安装目录 → stderr。
///
/// 为什么不能只写日志：日志插件本身可能初始化失败（2026-10-03 实测：安装态下
/// `failed to initialize plugin log: 拒绝访问 (os error 5)`），那时日志里一个字都没有，
/// 用户和排查者都只能看到"双击没反应"。
fn report_startup_error(detail: &str) -> Option<PathBuf> {
    let mut candidates: Vec<PathBuf> = vec![data_dir().join("qio-startup-error.txt")];
    if let Some(dir) = ownership::shell_install_dir() {
        candidates.push(dir.join("qio-startup-error.txt"));
    }
    for path in candidates {
        if std::fs::write(&path, detail.as_bytes()).is_ok() {
            return Some(path);
        }
    }
    eprintln!("[qio] {detail}");
    None
}

/// GUI 下把原因摆到用户面前（没有控制台可看）。`QIO_STARTUP_ERROR_DIALOG=0` 关掉它 ——
/// 无人值守/自动化环境需要进程可预期地退出，而不是停在一个模态框上等点击。
#[cfg(windows)]
fn show_startup_error_dialog(text: &str) {
    use windows_sys::Win32::UI::WindowsAndMessaging::{MessageBoxW, MB_ICONERROR, MB_OK, MB_SETFOREGROUND};
    if std::env::var("QIO_STARTUP_ERROR_DIALOG").ok().as_deref() == Some("0") {
        return;
    }
    let mut wide: Vec<u16> = text.encode_utf16().collect();
    wide.push(0);
    let mut title: Vec<u16> = "QIO 启动失败".encode_utf16().collect();
    title.push(0);
    unsafe {
        MessageBoxW(
            std::ptr::null_mut(),
            wide.as_ptr(),
            title.as_ptr(),
            MB_OK | MB_ICONERROR | MB_SETFOREGROUND,
        );
    }
}

#[cfg(not(windows))]
fn show_startup_error_dialog(text: &str) {
    eprintln!("[qio] {text}");
}

/// 写不下归属记录时的统一处理（fail-closed）：
///   1. 留下**明确原因**（数据目录 → 安装目录 → stderr，不依赖日志插件）；
///   2. **收掉已经启动的后台**：job 优先（一次收整棵树），否则按 pid 结束进程树；
///   3. 让用户看到原因（`QIO_STARTUP_ERROR_DIALOG=0` 可关）；
///   4. 以 1 退出 —— 绝不带着「后台在跑、卸载器却认不出归属」的状态继续运行。
///
/// 为什么是退出而不是"继续跑但没有记录"：那条路的终点是卸载时宁可不杀 → 安装目录删不干净，
/// 用户看到的是"卸载失败"，而且没有任何线索指向真正的原因（2026-10-03 用户明确要求堵掉）。
fn fatal_lease_failure(
    reason: &str,
    backend_pid: u32,
    job: Option<&backend_job::BackendJob>,
    child: Option<tauri_plugin_shell::process::CommandChild>,
) -> ! {
    let _ = &child; // 非 Windows 才用得到（那里没有 job 兜底）
    let detail = format!(
        "{LEASE_ERROR_MARKER}\n原因：{reason}\n后台 pid：{backend_pid}\n\
         处理：已终止本次启动的后台进程，并以退出码 1 结束（不留无归属的后台）。"
    );
    log::error!("[qio] {detail}");
    let written = report_startup_error(&detail);
    let killed = match job {
        Some(job) => job.terminate(),
        None => {
            #[cfg(windows)]
            {
                ownership::kill_tree_by_pid(backend_pid)
            }
            #[cfg(not(windows))]
            {
                // CommandChild::kill(self) 是按值消费，这里必须 move（Linux 上编译才过）。
                match child {
                    Some(c) => c.kill().is_ok(),
                    None => false,
                }
            }
        }
    };
    let where_text = match &written {
        Some(path) => format!("原因已写入：{}", path.display()),
        None => "原因写盘失败，见 stderr".to_string(),
    };
    let killed_text = if killed {
        "已终止本次启动的后台进程。"
    } else {
        "终止后台进程失败 —— 请手动结束它，再重新启动 QIO。"
    };
    let text = format!(
        "QIO 无法记录本次安装的所有权信息，已停止启动。\n\n原因：{reason}\n{killed_text}\n{where_text}\n\n\
         常见原因：安装目录不可写（杀毒软件/权限）。修好后重试即可。"
    );
    show_startup_error_dialog(&text);
    std::process::exit(1)
}

fn write_install_lease(shell_pid: u32, backend_pid: u32) -> Result<PathBuf, String> {
    if cfg!(debug_assertions) {
        log::info!("[qio] 开发态（debug 构建）不写 sidecar lease");
        // 开发态不是失败：调用方按 Ok 处理，只是不记路径（见 setup 里的 Debug 分支）。
        return Err("开发态（debug 构建）不写 lease".to_string());
    }
    let install_dir = ownership::shell_install_dir()
        .ok_or_else(|| "拿不到外壳 exe 所在目录，无法确定安装目录".to_string())?;
    let lease = ownership::lease_for_current_processes(&install_dir, shell_pid, backend_pid, None)
        .map_err(|err| format!("读进程身份失败：{err}"))?;
    let path = ownership::write_lease(&install_dir, &lease)
        .map_err(|err| format!("写 {} 失败：{err}", ownership::lease_path(&install_dir).display()))?;
    log::info!(
        "[qio] 已写 sidecar lease：shell pid {shell_pid} / backend pid {backend_pid} → {}",
        path.display()
    );
    Ok(path)
}

fn main() {
    let backend: Arc<Mutex<Option<CommandChild>>> = Arc::new(Mutex::new(None));
    let backend_for_setup = Arc::clone(&backend);
    // 本安装实例的 lease 路径（release 启动时写、退出回调里删）：setup 与退出共享这一个 Option。
    let lease_path: Arc<Mutex<Option<PathBuf>>> = Arc::new(Mutex::new(None));
    let lease_path_for_setup = Arc::clone(&lease_path);
    let lease_path_for_exit = Arc::clone(&lease_path);
    // job 是否真的生效（create + assign 都成功）。没生效时退出必须自己按 pid 杀整棵树 ——
    // 「失败时还有 taskkill 兜底」这句话必须真的接在退出路径上，否则就是一句注释。
    let job_active = Arc::new(AtomicBool::new(false));
    let job_active_for_setup = Arc::clone(&job_active);
    // 后端进程随这份 job 的生死而生死：壳没了，系统负责清干净，不留孤儿。
    let backend_job = backend_job::create();

    // 日志目录先探测一次可写性：**写不了日志不该让应用起不来**。
    // 2026-10-03 实测（安装版外壳）：tauri_plugin_log 的 setup 里 acquire_logger 一旦返回 Err，
    // 插件初始化就失败；而插件初始化失败会让 Builder::build() 直接 panic —— 用户看到的是
    // "双击没反应"（panic 里只有 PluginInitialization("log", 拒绝访问)）。
    // 日志是诊断手段，不是运行前提：写不了就退回只打控制台，并把原因说清楚。
    //
    // 探针用**独立文件名**、用完即删：探针自己占着 qio.log 会让插件的 RotatingFile
    // 在同一路径上再开一次，失败原因会被搅浑（排查时踩过）。
    let log_dir = data_dir().join("logs");
    let probe_path = log_dir.join(format!("qio-write-probe-{}.tmp", std::process::id()));
    let log_dir_ready = std::fs::create_dir_all(&log_dir).is_ok()
        && std::fs::write(&probe_path, b"probe").is_ok();
    let _ = std::fs::remove_file(&probe_path);
    if !log_dir_ready {
        eprintln!(
            "[qio] 日志目录不可写（{}）：本次只打控制台，不影响使用",
            log_dir.display()
        );
    }
    // 注意用 `targets([...])` 而**不是** `target(...)`：`Builder::new()` 的默认 targets 是
    // [Stdout, LogDir]，`target()` 是追加 —— 那个默认 LogDir 目标一旦初始化失败，整个插件
    // 初始化就失败，和我们自己加的 target 成不成没关系（2026-10-03 实测：两个变体报的错
    // 一模一样，都是 `拒绝访问 (os error 5)`，就是被默认 target 拖死的）。
    // `targets()` 是替换，所以这里显式列全我们真正要的落点。
    let logger_with = |kind: tauri_plugin_log::TargetKind| {
        tauri_plugin_log::Builder::new()
            .level(log::LevelFilter::Info)
            .targets([tauri_plugin_log::Target::new(kind)])
            .build()
    };
    let folder_logger = {
        let dir = log_dir.clone();
        move || {
            logger_with(tauri_plugin_log::TargetKind::Folder {
                path: dir.clone(),
                file_name: Some("qio".to_string()),
            })
        }
    };
    let logdir_logger = move || logger_with(tauri_plugin_log::TargetKind::LogDir { file_name: None });
    let console_logger = move || logger_with(tauri_plugin_log::TargetKind::Stdout);

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        // 应用内更新：updater（检查/下载/校验/安装）+ process（装完重启）。
        // 两者都在 Rust 侧工作，前端只通过插件 API 驱动，不需要放宽 CSP。
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .setup(move |app| {
            // 日志插件**在这里**注册（AppHandle::plugin 返回 Result），而不是在 Builder 链上：
            // Builder::plugin 的初始化错误会在 build() 里变成 panic（用户看到"双击没反应"），
            // 而日志只是诊断手段、不是运行前提。注册失败就退回控制台并说明原因，应用照常启动。
            //
            // 按「文件夹 → 控制台」顺序试：装到文件里最好，但**任何一个能成就行**。
            // 每个失败原因都留档（数据目录 → 安装目录 → stderr），不能只在控制台上喊一声 ——
            // 安装态下没有控制台可看。2026-10-03 实测：文件夹目标会报 `拒绝访问 (os error 5)`，
            // 用户据此能知道该去查什么（目录权限/杀软）。
            let mut log_errors: Vec<String> = Vec::new();
            let mut log_ok = false;
            for (label, plugin) in [
                ("数据目录文件", folder_logger()),
                ("插件默认日志目录", logdir_logger()),
                ("控制台", console_logger()),
            ] {
                match app.handle().plugin(plugin) {
                    Ok(()) => {
                        log_ok = true;
                        eprintln!("[qio] 日志落点：{label}");
                        break;
                    }
                    Err(err) => log_errors.push(format!("{label}目标初始化失败：{err}")),
                }
            }
            if !log_ok {
                let detail = format!(
                    "QIO-LOG-INIT-FAILED\n原因：{}\n说明：日志不可用不影响使用，但排查时只能靠这一行。",
                    log_errors.join("；")
                );
                eprintln!("[qio] {detail}");
                report_startup_error(&detail);
            }
            // 启动各步骤的耗时写进日志：下次"白屏很久"能直接看出卡在哪一步，
            // 而不是只能靠猜（2026-09-24 那次就是没有这一步，排查全靠推断）。
            let t_start = Instant::now();
            if let Some(proxy) = apply_updater_proxy() {
                log::info!("[qio] 更新走代理：{proxy}");
            } else {
                log::info!("[qio] 更新未配置代理（可用 %APPDATA%\\qio\\updater-proxy.txt 指定）");
            }
            let proxy_ms = t_start.elapsed().as_millis();
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
            let t_models = Instant::now();
            let models_dir = prepare_models(app.handle());
            let models_ms = t_models.elapsed().as_millis();
            let t_backend = Instant::now();
            // 自带 Python 运行时（安装包资源）：交给后端去建依赖环境；没有就按以前的行为。
            let python_dir = bundled_python_dir(app.handle());
            let child = backend_launch(app.handle(), port, &token_path, models_dir, python_dir)?;
            // 时序是这份修复的一部分：**必须在 spawn 之后立刻 assign**。
            // onefile 的 launcher 是先把压缩包解到临时目录、再创建真正提供服务的 child
            // （实测 child 比 launcher 晚约 1.5s）。Windows 只把「指派之后创建的后代」
            // 自动收进 job —— 等 child 出现再 assign 就收不到它，关 job 也杀不掉，
            // 安装器又会报 Can't write。反证见 scripts/verify_backend_process_model.py
            // 的 job_late 实验与 src/main.rs 里 backend_job::tests::descendants_created_before_assignment_are_not_captured。
            if let Some(job) = backend_job.as_ref() {
                let assigned = job.assign(child.pid());
                job_active_for_setup.store(assigned, Ordering::SeqCst);
                if assigned {
                    log::info!("[qio] 后端 pid {} 已纳入 job（随壳退出自动终止）", child.pid());
                } else {
                    log::warn!("[qio] job 未生效：退出时会退回 taskkill /T 按 pid 清理整棵树");
                }
            }
            log::info!(
                "[qio] 启动耗时：代理探测 {proxy_ms}ms / 内置模型 {models_ms}ms / 拉起后端 {}ms / 合计 {}ms",
                t_backend.elapsed().as_millis(),
                t_start.elapsed().as_millis()
            );
            let backend_pid = child.pid();
            *backend_for_setup.lock().unwrap() = Some(child);
            // 所有权记录：只有 release（安装态）才写；开发态不写（见 write_install_lease）。
            //
            // fail-closed：写不下归属就**不能留下后台**（否则卸载时"无法确认归属 → 宁可不杀"
            // → 安装目录删不干净）。处理见 fatal_lease_failure：留原因 → 收后台 → 退出码 1。
            match write_install_lease(std::process::id(), backend_pid) {
                Ok(path) => *lease_path_for_setup.lock().unwrap() = Some(path),
                Err(err) if cfg!(debug_assertions) => {
                    log::info!("[qio] 开发态不写 sidecar lease：{err}");
                }
                Err(err) => {
                    let child = backend_for_setup.lock().unwrap().take();
                    fatal_lease_failure(&err, backend_pid, backend_job.as_ref(), child);
                }
            }
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
                    if job_active.load(Ordering::SeqCst) {
                        // job 生效：杀掉 launcher 就够，系统会带走 job 里整棵树
                        let _ = child.kill();
                    } else {
                        // job 没生效：必须按 pid 结束整棵树，否则 onefile 的 child 会留下
                        // 锁住 qio-backend.exe（安装器报 Can't write 的根因）
                        #[cfg(windows)]
                        {
                            let _ = kill_tree_by_pid(child.pid());
                        }
                        #[cfg(not(windows))]
                        {
                            let _ = child.kill();
                        }
                    }
                }
                // lease 是「本安装实例还活着」的唯一记录：外壳退出（任何路径）都要删掉，
                // 否则下次卸载会读到一个指向已死进程的文件，帮助程序只会如实报"无法确认"。
                if let Some(path) = lease_path_for_exit.lock().unwrap().take() {
                    match ownership::remove_lease_at(&path) {
                        Ok(()) => log::info!("[qio] 已删除 sidecar lease：{}", path.display()),
                        Err(err) => log::warn!("[qio] 删除 sidecar lease 失败：{err}"),
                    }
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

    // 跑命令的硬超时那两条测试随 run_command_with_timeout 一起挪到了
    // qio_core::ownership（同一份实现，就在它自己的 crate 里测）。
}
