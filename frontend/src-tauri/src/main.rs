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
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

mod attachment_picker;
mod backend_lifecycle;
mod updater_net;

use backend_lifecycle::{
    process_alive, stop_pids, stop_tree_by_pid, StopFailure, StopReport, STOP_VERIFY_BUDGET,
};
use qio_core::ownership;
// 按 pid 结束整棵树只在 Windows 的退出/兜底路径上用（非 Windows 没有 job，也没有 taskkill 语义）。
#[cfg(windows)]
use qio_core::ownership::kill_tree_by_pid;
use serde::Serialize;
use tauri::{Manager, RunEvent};
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;
use updater_net::{install_direct_fallback_guard, resolve_updater_proxy};

/// 后端连接信息：只在壳与自己的 WebView 之间传递。
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
    /// 用户目录里已经是同一份**完整**内容（跳过复制）
    AlreadyPresent,
    /// 复制完成并已原子发布（首次安装或内置模型换版）
    Copied,
}

/// 发布用的临时文件标记。
///
/// 临时文件必须和目标在**同一个目录**：只有同目录 rename 才是原子的
/// （跨目录/跨卷 rename 会退化成"复制 + 删除"，就又出现半成品窗口了）。
const PUBLISH_TEMP_MARK: &str = ".qio-tmp-";
/// 超过这个年龄的临时文件按「上次崩溃留下的」清掉。
///
/// 为什么要有年龄门槛：另一个实例可能**正在**发布，它的临时文件是新的 —— 不能碰。
const STALE_TEMP_AGE: Duration = Duration::from_secs(60 * 60);

/// 一个要同步的文件及其完整性判据。
#[derive(Debug, Clone, PartialEq, Eq)]
struct FileSpec {
    name: String,
    /// 期望字节数：清单里声明的 `bytes`（并已与内置源核对过）。
    bytes: u64,
    /// 期望 sha256：清单里声明的；清单没给就只比体积（见 `verify_file` 注释）。
    sha256: Option<String>,
    /// 必需文件：清单里的模型档 + tokenizer + 清单本身。
    /// 许可证/声明是可选文件：缺了不该让语义检索整个关掉。
    required: bool,
}

/// 每次发布用唯一的临时名（同进程多次发布、两个实例并发发布都不会撞）。
fn next_temp_seq() -> u64 {
    static SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    SEQ.fetch_add(1, std::sync::atomic::Ordering::SeqCst)
}

fn temp_path_for(target: &Path, name: &str) -> PathBuf {
    target.join(format!(
        "{name}{PUBLISH_TEMP_MARK}{}-{}",
        std::process::id(),
        next_temp_seq()
    ))
}

/// 文件的 sha256（十六进制小写）。
fn sha256_file(path: &Path) -> Result<String, String> {
    use sha2::{Digest, Sha256};
    let mut file =
        std::fs::File::open(path).map_err(|e| format!("打开 {} 失败：{e}", path.display()))?;
    let mut hasher = Sha256::new();
    std::io::copy(&mut file, &mut hasher)
        .map_err(|e| format!("读 {} 失败：{e}", path.display()))?;
    Ok(format!("{:x}", hasher.finalize()))
}

fn short_hash(hash: &str) -> String {
    hash.chars().take(12).collect()
}

/// 完整性判据（写清在这里，便于复核）：
///
/// 1. **体积必须完全相等**（清单声明 `bytes` 的按清单，其余按内置源文件的体积）；
/// 2. 清单声明了 `sha256` 的，**必须逐字节哈希一致**；
/// 3. 清单没给哈希的（tokenizer / 清单 / 许可证），退化为体积判据 ——
///    这些文件要么来自我们自己的安装包，要么被第 1 条挡住；90MB 的模型档有清单哈希，
///    不会漏掉。
///
/// 为什么快路径也做体积校验：旧实现只查 `exists()`，于是「半成品 + `.ready` 匹配」
/// 会被永久当成就绪（多实例共享数据目录时真的会发生）。
fn verify_file(path: &Path, spec: &FileSpec, label: &str) -> Result<(), String> {
    let meta = std::fs::metadata(path)
        .map_err(|e| format!("{label} {} 读不到：{e}", path.display()))?;
    if meta.len() != spec.bytes {
        return Err(format!(
            "{label} {} 体积不对：期望 {} 字节 / 实际 {} 字节",
            path.display(),
            spec.bytes,
            meta.len()
        ));
    }
    if let Some(expected) = &spec.sha256 {
        let actual = sha256_file(path)?;
        if !actual.eq_ignore_ascii_case(expected) {
            return Err(format!(
                "{label} {} 内容与清单哈希不符：期望 {}… / 实际 {}…",
                path.display(),
                short_hash(expected),
                short_hash(&actual)
            ));
        }
    }
    Ok(())
}

/// 清单 + tokenizer + 清单文件 + （存在就带上的）许可证/声明 → 逐项判据。
fn model_file_specs(bundled: &Path, manifest: &serde_json::Value) -> Result<Vec<FileSpec>, String> {
    let mut specs: Vec<FileSpec> = Vec::new();
    if let Some(files) = manifest.get("files").and_then(|v| v.as_array()) {
        for entry in files {
            let name = entry.get("file").and_then(|v| v.as_str()).unwrap_or("");
            if name.is_empty() {
                continue;
            }
            push_spec(
                &mut specs,
                bundled,
                name,
                entry.get("bytes").and_then(|v| v.as_u64()),
                entry
                    .get("sha256")
                    .and_then(|v| v.as_str())
                    .map(|value| value.trim().to_ascii_lowercase()),
                true,
            )?;
        }
    }
    for name in ["tokenizer.json", "model_manifest.json"] {
        if !specs.iter().any(|spec| spec.name == name) {
            push_spec(&mut specs, bundled, name, None, None, true)?;
        }
    }
    for name in optional_files() {
        if bundled.join(name).exists() {
            push_spec(&mut specs, bundled, name, None, None, false)?;
        }
    }
    if specs.is_empty() {
        return Err("清单里没有可同步的文件".to_string());
    }
    Ok(specs)
}

fn push_spec(
    specs: &mut Vec<FileSpec>,
    bundled: &Path,
    name: &str,
    declared_bytes: Option<u64>,
    sha256: Option<String>,
    required: bool,
) -> Result<(), String> {
    let source = bundled.join(name);
    let meta = std::fs::metadata(&source)
        .map_err(|e| format!("内置资源里缺少 {name}（{}）：{e}", source.display()))?;
    if let Some(declared) = declared_bytes {
        if declared != meta.len() {
            return Err(format!(
                "内置资源 {name} 的体积与清单不符：清单 {declared} 字节 / 实际 {} 字节",
                meta.len()
            ));
        }
    }
    specs.push(FileSpec {
        name: name.to_string(),
        bytes: declared_bytes.unwrap_or(meta.len()),
        sha256,
        required,
    });
    Ok(())
}

/// 清掉上次崩溃留下的发布临时文件；**只动明显过期的**，绝不碰正在发布的另一实例。
fn cleanup_stale_temps(target: &Path) -> Vec<PathBuf> {
    let mut removed: Vec<PathBuf> = Vec::new();
    let Ok(entries) = std::fs::read_dir(target) else {
        return removed;
    };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        if !name.contains(PUBLISH_TEMP_MARK) {
            continue;
        }
        let Ok(metadata) = entry.metadata() else { continue };
        let Ok(modified) = metadata.modified() else { continue };
        let Ok(age) = std::time::SystemTime::now().duration_since(modified) else {
            continue;
        };
        if age < STALE_TEMP_AGE {
            continue;
        }
        let path = entry.path();
        if std::fs::remove_file(&path).is_ok() {
            removed.push(path);
        }
    }
    removed
}

/// 发布单个文件：写临时文件 → **校验临时文件** → 同目录 rename（原子替换目标）。
///
/// 任何一步失败都删掉临时文件并返回 Err：目标文件保持原样（要么旧的完整版，要么不存在），
/// 绝不会让读者看到一个写了一半的文件。
fn publish_file(source: &Path, target: &Path, spec: &FileSpec, temp: &Path) -> Result<(), String> {
    let _ = std::fs::remove_file(temp);
    std::fs::copy(source, temp).map_err(|e| format!("复制 {} 失败：{e}", spec.name))?;
    if let Err(err) = verify_file(temp, spec, "临时文件") {
        let _ = std::fs::remove_file(temp);
        return Err(format!("{}：已删除临时文件，不发布", err));
    }
    if let Err(err) = std::fs::rename(temp, target) {
        let _ = std::fs::remove_file(temp);
        return Err(format!("发布 {} 失败：{err}", target.display()));
    }
    Ok(())
}

/// 把内置模型同步到用户数据目录：幂等、可重复调用、**原子发布**。
///
/// 为什么要原子发布（多实例共享同一个数据目录）：旧实现直接 `fs::copy` 到目标文件、
/// 直接写 `.ready`，跳过条件只看 `exists()` —— 两个实例同时首启时，先写完的一方留下
/// `.ready`，另一方还在写；此后每次启动都会「指纹匹配 + 文件存在」而跳过复制，
/// 半成品被永久当成就绪（表现为语义检索静默失效）。
///
/// 现在的顺序：
/// 1. 快路径：`.ready` 指纹一致 **且逐个必需文件完整性校验通过** → `AlreadyPresent`；
/// 2. 否则：撤掉 `.ready`（内容没发布完之前不该有任何"就绪"标记）→ 清理过期临时文件；
/// 3. 逐文件「复制到临时文件 → 校验 → 同目录 rename 原子替换」；
/// 4. 全部成功之后才把 `.ready`（临时文件 + rename）发布出去。
///
/// 并发：两个实例写的是**同一份内置内容**，各自用唯一临时名 + 原子 rename，互相踩不到；
/// 版本不同的两个实例混写时，下一次启动的「指纹 + 体积/哈希校验」会判定不完整并重做
/// （`.ready` 不再被当成唯一事实）。
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
    let specs = model_file_specs(bundled, &manifest)?;

    let ready_path = target.join(".ready");
    let ready_matches = std::fs::read_to_string(&ready_path)
        .map(|content| content.trim() == fingerprint)
        .unwrap_or(false);
    if ready_matches {
        let mut incomplete: Vec<String> = Vec::new();
        for spec in specs.iter().filter(|spec| spec.required) {
            if let Err(err) = verify_file(&target.join(&spec.name), spec, "已发布文件") {
                incomplete.push(err);
            }
        }
        if incomplete.is_empty() {
            return Ok(SyncOutcome::AlreadyPresent);
        }
        log::warn!(
            "[qio] 内置模型标记存在但内容不完整，重新发布：{}",
            incomplete.join("；")
        );
    }

    std::fs::create_dir_all(target)
        .map_err(|e| format!("建目录 {} 失败：{e}", target.display()))?;
    // 先撤掉 .ready：发布完成之前不该有"就绪"标记（中断也不会留下假 ready）
    let _ = std::fs::remove_file(&ready_path);
    let removed = cleanup_stale_temps(target);
    if !removed.is_empty() {
        log::info!("[qio] 清掉 {} 个上次残留的发布临时文件", removed.len());
    }

    for spec in &specs {
        let source = bundled.join(&spec.name);
        let final_path = target.join(&spec.name);
        let temp = temp_path_for(target, &spec.name);
        // 先验内置源：坏源不该被复制进去（也避免白复制 90MB）
        if let Err(err) = verify_file(&source, spec, "内置资源") {
            let _ = std::fs::remove_file(&temp);
            if !spec.required {
                log::warn!("[qio] 可选文件 {err}：跳过它，不影响语义检索");
                continue;
            }
            return Err(format!("内置资源不完整，已中止发布：{err}"));
        }
        if let Err(err) = publish_file(&source, &final_path, spec, &temp) {
            if !spec.required {
                log::warn!("[qio] 可选文件 {err}：跳过它，不影响语义检索");
                continue;
            }
            return Err(err);
        }
    }

    // 全部发布成功之后才写标记（临时文件 + rename，和模型档同一套原子语义）
    let ready_tmp = temp_path_for(target, ".ready");
    std::fs::write(&ready_tmp, &fingerprint).map_err(|e| format!("写标记临时文件失败：{e}"))?;
    if let Err(err) = std::fs::rename(&ready_tmp, &ready_path) {
        let _ = std::fs::remove_file(&ready_tmp);
        return Err(format!("发布标记文件失败：{err}"));
    }
    Ok(SyncOutcome::Copied)
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
///
/// 代理探测（按优先级 + 总预算）、系统代理读取与可达性验证都搬到了
/// `updater_net`：那里的实现带单测，也保证不改进程级代理环境变量。

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
///
/// 异步命令 + `spawn_blocking`：等后端写令牌最多 30 秒，绝不能占住窗口主线程。
#[tauri::command]
async fn qio_backend_info(
    state: tauri::State<'_, Arc<ShellRuntime>>,
) -> Result<BackendInfo, String> {
    let port = state.port;
    let path = state.token_path.clone();
    let token = tauri::async_runtime::spawn_blocking(move || {
        read_token_file(&path, Duration::from_secs(30))
    })
    .await
    .map_err(|err| format!("token read task failed: {err}"))?
    .ok_or_else(|| "backend did not publish a session token".to_string())?;
    Ok(BackendInfo { port, token })
}

/// 更新前调用：把本实例后端进程**整棵树**结束掉，并返回**真实结果**。
///
/// 为什么必须有这一步（2026-09-22 实测）：后端是 PyInstaller 单文件程序，它自己会再起一个
/// 子进程跑真正的服务。只杀父进程会留下子进程继续占着 `qio-backend.exe`，
/// 于是 NSIS 安装器报 `Can't write: ...\qio-backend.exe` 并中止安装 —— 用户看到的就是
/// 「安装失败」，实际是文件被残留进程锁住。所以按 **pid** 结束整棵树（`/T`），绝不按映像名。
///
/// **只报真实结果**：超时、权限错误、退出验证失败都返回 Err（前端据此放弃安装），
/// 不吞任何异常假装兼容旧命令。异步 + `spawn_blocking`：taskkill 与退出验证要等几秒，
/// 占住窗口主线程就是卡顿本身。
#[tauri::command]
async fn qio_prepare_for_update(
    state: tauri::State<'_, Arc<ShellRuntime>>,
) -> Result<StopReport, String> {
    let runtime = Arc::clone(state.inner());
    tauri::async_runtime::spawn_blocking(move || runtime.prepare_for_update())
        .await
        .map_err(|err| format!("结束后端任务失败：{err}"))?
}

/// 停止后端之后安装失败（或结束后端失败）时调用：恢复本实例后端与前端连接。
///
/// 幂等：后端**真的**还在跑就什么都不做（不重复启动）；已经停了就重新拉起（同一端口、
/// 同一令牌文件，重写归属记录并重新纳入 job），并等它把新令牌写出来。
/// 前端拿到 `restored=true` 之后重置连接缓存即可继续用 —— 不允许只显示「更新失败」
/// 却把聊天留在不可用状态。
#[tauri::command]
async fn qio_restore_backend(
    app: tauri::AppHandle,
    state: tauri::State<'_, Arc<ShellRuntime>>,
) -> Result<RestoreResult, String> {
    let runtime = Arc::clone(state.inner());
    tauri::async_runtime::spawn_blocking(move || runtime.restore_backend(&app))
        .await
        .map_err(|err| format!("恢复后端任务失败：{err}"))?
}

/// 前端每次检查更新之前调用：按当前网络环境重新判断该不该走代理。
///
/// 为什么由前端触发而不是只在启动时判断一次：VPN 是随时开关的。启动时缓存住判断结果，
/// 用户在会话期间开了 VPN（或关掉）就失效了。每次检查前重算一遍，用户就不用重启 QIO。
///
/// 返回**这次操作**要用的代理（null = 明确直连）：前端把它显式交给 updater 插件，
/// 检查 / 下载 / 安装共用同一个 `Update` 上下文（配置变化不会追溯改已开始的操作）。
/// 这里**不改任何进程级环境变量**。
#[tauri::command]
async fn qio_refresh_updater_proxy() -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let file = std::fs::read_to_string(data_dir().join("updater-proxy.txt")).ok();
        let started = Instant::now();
        let plan = resolve_updater_proxy(file);
        let summary = format!("{}（{}）", plan.source.label(), plan.detail);
        let proxy = plan.proxy.clone();
        log::info!(
            "[qio] 更新代理探测完成：{summary}，耗时 {}ms",
            started.elapsed().as_millis()
        );
        proxy
    })
    .await
    .map_err(|err| format!("代理探测任务失败：{err}"))
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

// ---------------------------------------------------------------------------
// 生命周期闸门与外壳运行时状态
// ---------------------------------------------------------------------------

/// 后端生命周期阶段。
///
/// 启动、退出与更新之间只允许**合法**转换（见 [`ShellRuntime::transition`]）：
/// 例如外壳已经在退出就不再拉起后端（关窗时后台准备任务不许制造孤儿进程），
/// 正在结束后端时不能再起第二个「结束」或「恢复」。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum BackendPhase {
    /// 启动准备中（后台在跑模型准备，还没拉起后端）
    Preparing,
    Running,
    /// 更新前正在结束后端
    Stopping,
    /// 后端已结束（等安装器接手，或等恢复）
    Stopped,
    /// 正在恢复后端
    Restoring,
    /// 外壳正在退出：任何后台任务都不许再拉起后端
    Exiting,
}

impl BackendPhase {
    fn label(self) -> &'static str {
        match self {
            BackendPhase::Preparing => "启动准备中",
            BackendPhase::Running => "运行中",
            BackendPhase::Stopping => "正在结束后端",
            BackendPhase::Stopped => "已结束",
            BackendPhase::Restoring => "正在恢复后端",
            BackendPhase::Exiting => "正在退出",
        }
    }
}

/// 拉起后端时的失败分类：调用方按场景决定「退出应用」还是「报告可恢复状态」。
enum LaunchFailure {
    /// 进程根本没起来（sidecar/命令缺失、参数错误……）
    Spawn(String),
    /// 归属记录写不下：进程已经按 fail-closed 收掉了，**绝不能**留下无归属的后台
    Lease { reason: String, pid: u32 },
}

/// 恢复后端的结果（前端据此决定要不要重置连接缓存）。
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct RestoreResult {
    /// 本实例后端当前可用
    restored: bool,
    /// 这次是否真的重新拉起（false = 本来就在跑，没有重复启动）
    started: bool,
    port: u16,
    detail: String,
}

/// 外壳的运行时状态：后端进程、归属记录、生命周期闸门、更新网络策略。
///
/// 加锁顺序（避免死锁）：**先 `phase`，后 `child` / `lease_path` / `residual_pids`**。
struct ShellRuntime {
    port: u16,
    token_path: PathBuf,
    /// 内置模型目录（后台准备完成后写入，恢复后端时复用）
    models_dir: Mutex<Option<PathBuf>>,
    /// 安装包自带的 Python 运行时目录（后台准备时探测出来）
    python_dir: Mutex<Option<PathBuf>>,
    child: Mutex<Option<CommandChild>>,
    lease_path: Mutex<Option<PathBuf>>,
    /// 上一次结束后端时没杀干净的 pid（恢复时按 pid 再清一遍）
    residual_pids: Mutex<Vec<u32>>,
    job: Option<Arc<backend_job::BackendJob>>,
    job_active: AtomicBool,
    phase: Mutex<BackendPhase>,
}

fn lock<'a, T>(mutex: &'a Mutex<T>) -> MutexGuard<'a, T> {
    // 中毒也继续用里面的值：一个后台任务的 panic 不该让「谁在跑、什么阶段」失去事实。
    mutex.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

impl ShellRuntime {
    fn phase(&self) -> BackendPhase {
        *lock(&self.phase)
    }

    /// 唯一的生命周期转换入口：当前阶段不在 `expected` 里就拒绝（返回 Err）。
    fn transition(&self, expected: &[BackendPhase], next: BackendPhase) -> Result<(), String> {
        let mut phase = lock(&self.phase);
        if !expected.contains(&*phase) {
            return Err(format!(
                "非法生命周期转换：{} → {}",
                phase.label(),
                next.label()
            ));
        }
        *phase = next;
        Ok(())
    }

    fn backend_pid(&self) -> Option<u32> {
        lock(&self.child).as_ref().map(|child| child.pid())
    }

    /// 后端是不是**真的**还在跑（句柄在 + 进程存活）。
    ///
    /// 不能只看句柄：进程可能已经退出（句柄还在我们手里），那会把死进程当成"在跑"，
    /// 恢复流程就会跳过重启、把聊天留在不可用状态。
    fn is_backend_alive(&self) -> bool {
        self.backend_pid().map(process_alive).unwrap_or(false)
    }

    /// 拉起后端 + Job Object 指派 + 归属记录。
    ///
    /// **时序不可换**：必须在 spawn 之后立刻 assign（onefile 的 child 比 launcher 晚约 1.5s，
    /// Windows 只把「指派之后创建的后代」收进 job）；归属记录写不下就 fail-closed 收掉后台。
    ///
    /// 调用方必须已经持有一个不会再被退出/更新插进来的阶段（Preparing/Restoring）——
    /// 关窗时后台任务不许再拉起孤儿后端，靠的就是这道闸门。
    fn launch_backend_locked(&self, app: &tauri::AppHandle) -> Result<u32, LaunchFailure> {
        let models_dir = lock(&self.models_dir).clone();
        let python_dir = lock(&self.python_dir).clone();
        let child = backend_launch(app, self.port, &self.token_path, models_dir, python_dir)
            .map_err(LaunchFailure::Spawn)?;
        if let Some(job) = self.job.as_ref() {
            let assigned = job.assign(child.pid());
            self.job_active.store(assigned, Ordering::SeqCst);
            if assigned {
                log::info!(
                    "[qio] 后端 pid {} 已纳入 job（随壳退出自动终止）",
                    child.pid()
                );
            } else {
                log::warn!("[qio] job 未生效：退出时会退回 taskkill /T 按 pid 清理整棵树");
            }
        }
        let pid = child.pid();
        *lock(&self.child) = Some(child);
        // 同一安装目录重复启动：先认一认已有实例（只记日志，不阻断合法实例），
        // 顺手清掉已退出实例留下的记录 —— 绝不覆盖别人的记录。
        if let Some(install_dir) = ownership::shell_install_dir() {
            note_and_prune_instances(&install_dir, std::process::id());
        }
        match write_install_lease(std::process::id(), pid) {
            Ok(path) => {
                *lock(&self.lease_path) = Some(path);
                Ok(pid)
            }
            Err(err) if cfg!(debug_assertions) => {
                log::info!("[qio] 开发态不写 sidecar lease：{err}");
                Ok(pid)
            }
            Err(reason) => {
                // fail-closed：绝不留「后台在跑、卸载器却认不出归属」的状态。
                log::error!("[qio] 归属记录写不下（{reason}）：收掉本次启动的后端 pid {pid}");
                let _ = stop_tree_by_pid(pid, STOP_VERIFY_BUDGET);
                let _ = lock(&self.child).take();
                Err(LaunchFailure::Lease { reason, pid })
            }
        }
    }

    /// 启动准备完成之后拉起后端（后台线程里调用）。
    ///
    /// 拿不到闸门（外壳正在退出）时**什么都不做**：这是「关窗后不许再拉起孤儿后端」的落点。
    fn launch_backend_at_startup(&self, app: &tauri::AppHandle) -> Result<(), LaunchFailure> {
        let mut phase = lock(&self.phase);
        match *phase {
            BackendPhase::Preparing => {}
            BackendPhase::Exiting => {
                log::info!("[qio] 外壳正在退出，本次启动不再拉起后端");
                return Ok(());
            }
            other => {
                return Err(LaunchFailure::Spawn(format!(
                    "非法生命周期转换：{} → 运行中",
                    other.label()
                )))
            }
        }
        self.launch_backend_locked(app)?;
        *phase = BackendPhase::Running;
        Ok(())
    }

    /// 更新前结束后端进程树：**只返回真实结果**，拿不到「已确认退出」就是 Err。
    fn prepare_for_update(&self) -> Result<StopReport, String> {
        let pid = {
            let mut phase = lock(&self.phase);
            match *phase {
                BackendPhase::Stopped => {
                    return Ok(StopReport::already_stopped(
                        "本实例后端已经结束过，无需重复结束",
                    ))
                }
                BackendPhase::Running => {}
                BackendPhase::Preparing => {
                    return Err("后端还在启动准备中，请稍后再试（本次不安装）".to_string())
                }
                BackendPhase::Exiting => {
                    return Err("外壳正在退出，不再结束后端".to_string())
                }
                BackendPhase::Stopping | BackendPhase::Restoring => {
                    return Err(format!(
                        "后端生命周期正在转换中（{}），请稍后再试",
                        phase.label()
                    ))
                }
            }
            let Some(pid) = self.backend_pid() else {
                *phase = BackendPhase::Stopped;
                return Ok(StopReport::already_stopped("本实例没有在跑的后端"));
            };
            *phase = BackendPhase::Stopping;
            pid
        }; // 立刻放掉闸门：结束进程要等几秒，不能占着生命周期锁

        let outcome = stop_tree_by_pid(pid, STOP_VERIFY_BUDGET);

        let mut phase = lock(&self.phase);
        if *phase == BackendPhase::Exiting {
            // 外壳在结束后端的过程中退出了：退出阶段优先，不要把它改回去（更不要报"已结束"）
            log::info!("[qio] 结束后端期间外壳正在退出：保持退出阶段");
            return match outcome {
                Ok(report) => Ok(report),
                Err(StopFailure { detail, .. }) => Err(detail),
            };
        }
        match outcome {
            Ok(report) => {
                // 只有确认退出之后才丢掉 child 句柄（失败时保留，恢复流程要靠它判断"其实还在跑"）
                let _ = lock(&self.child).take();
                *lock(&self.residual_pids) = Vec::new();
                *phase = BackendPhase::Stopped;
                Ok(report)
            }
            Err(StopFailure { detail, survivors }) => {
                // 没确认退出：不谎报成功，也不谎报「已结束」。
                *lock(&self.residual_pids) = survivors;
                *phase = if self.is_backend_alive() {
                    BackendPhase::Running
                } else {
                    BackendPhase::Stopped
                };
                Err(detail)
            }
        }
    }

    /// 停止后端之后把本实例后端恢复回来（幂等：本来在跑就不重复启动）。
    fn restore_backend(&self, app: &tauri::AppHandle) -> Result<RestoreResult, String> {
        log::info!("[qio] 收到恢复后端请求：当前阶段 {}", self.phase().label());
        {
            let mut phase = lock(&self.phase);
            match *phase {
                BackendPhase::Exiting => {
                    return Err("外壳正在退出，不再拉起后端".to_string())
                }
                BackendPhase::Stopping | BackendPhase::Restoring => {
                    return Err(format!(
                        "后端生命周期正在转换中（{}），请稍后再试",
                        phase.label()
                    ))
                }
                BackendPhase::Running => {}
                // Stopped：更新时停掉了；Preparing：启动还没拉起后端，恢复流程接管
                BackendPhase::Stopped | BackendPhase::Preparing => {}
            }
            if self.is_backend_alive() {
                // 没有重复启动：后端还在服务，前端只需要继续用（连接信息没变）
                *phase = BackendPhase::Running;
                return Ok(RestoreResult {
                    restored: true,
                    started: false,
                    port: self.port,
                    detail: "本实例后端仍在运行，未重复启动".to_string(),
                });
            }
            // 句柄在但进程已经没了：清掉，避免把死进程当成"在跑"
            let _ = lock(&self.child).take();
            *phase = BackendPhase::Restoring;
        }

        // 上一次没杀干净的残留：按 pid 再清一遍（只按 pid，来自我们自己的后代快照）。
        // 清理期间放掉闸门（要等几秒），但**拉起之前会重新确认阶段仍是 Restoring** ——
        // 外壳若在这期间开始退出，就绝不拉起新后端（关窗后不许制造孤儿进程）。
        let residual = std::mem::take(&mut *lock(&self.residual_pids));
        if !residual.is_empty() {
            let left = stop_pids(&residual, Duration::from_secs(5));
            if !left.is_empty() {
                let _ = self.transition(&[BackendPhase::Restoring], BackendPhase::Stopped);
                return Err(format!(
                    "恢复前清理残留后端进程失败（还在跑：{left:?}），请完全退出 QIO 后重新启动"
                ));
            }
        }

        // 令牌文件先删：绝不让前端读到上一轮的旧令牌
        let _ = std::fs::remove_file(&self.token_path);
        // 拉起必须在闸门内完成：spawn → job 指派 → 归属记录期间，退出/更新都不能插进来
        let phase_guard = lock(&self.phase);
        if *phase_guard != BackendPhase::Restoring {
            return Err(format!(
                "后端生命周期已变为 {}（外壳可能在退出），放弃恢复",
                phase_guard.label()
            ));
        }
        let pid = match self.launch_backend_locked(app) {
            Ok(pid) => pid,
            Err(LaunchFailure::Spawn(reason)) => {
                drop(phase_guard);
                let _ = self.transition(&[BackendPhase::Restoring], BackendPhase::Stopped);
                return Err(format!("重新拉起后端失败：{reason}"));
            }
            Err(LaunchFailure::Lease { reason, .. }) => {
                drop(phase_guard);
                let _ = self.transition(&[BackendPhase::Restoring], BackendPhase::Stopped);
                return Err(format!(
                    "无法写归属记录（{reason}），已停止刚拉起的内核；请完全退出 QIO 后重新启动"
                ));
            }
        };
        drop(phase_guard); // 拉起已完成、child 已登记：等令牌期间不占着生命周期锁

        // 等后端真的把令牌写出来：前端要靠它重新连上，否则恢复就是假的
        let published = read_token_file(&self.token_path, Duration::from_secs(20)).is_some();
        {
            let mut phase = lock(&self.phase);
            // 退出阶段优先：等令牌期间外壳可能已经开始退出（那时 child 已被退出路径收走）
            if *phase == BackendPhase::Restoring {
                *phase = BackendPhase::Running;
            }
        }
        if !published {
            return Err(format!(
                "后端 pid {pid} 已重新拉起，但 20 秒内没有发布会话令牌"
            ));
        }
        log::info!("[qio] 已恢复本实例后端：pid {pid}，端口 {}", self.port);
        Ok(RestoreResult {
            restored: true,
            started: true,
            port: self.port,
            detail: format!("已重新拉起后端 pid {pid}（端口 {}）", self.port),
        })
    }
}

/// 收掉本次启动/恢复的后台（job 优先，否则按 pid 结束整棵树）。
fn terminate_runtime_backend(runtime: &ShellRuntime) -> bool {
    let pid = runtime.backend_pid();
    match (pid, runtime.job.as_deref()) {
        (Some(_), Some(job)) if runtime.job_active.load(Ordering::SeqCst) => job.terminate(),
        (Some(pid), _) => {
            #[cfg(windows)]
            {
                ownership::kill_tree_by_pid(pid)
            }
            #[cfg(not(windows))]
            {
                let _ = pid;
                false
            }
        }
        (None, _) => false,
    }
}

/// 后端根本起不来时的统一处置：留原因 → 收后台 → 让用户看到 → 退出码 1。
///
/// 旧行为是 setup 里 `?` 直接失败 → `Builder::build().expect()` panic；这里保持
/// 「进程退出 + 原因可见」，但不再把失败藏在 panic 里。
fn fatal_backend_start_failure(reason: &str, runtime: &ShellRuntime) -> ! {
    let detail = format!(
        "QIO-BACKEND-START-FAILED\n原因：{reason}\n\
         处理：本次启动没有可用的后端，已终止后台并以退出码 1 结束。"
    );
    log::error!("[qio] {detail}");
    let written = report_startup_error(&detail);
    let killed = terminate_runtime_backend(runtime);
    let where_text = match &written {
        Some(path) => format!("原因已写入：{}", path.display()),
        None => "原因写盘失败，见 stderr".to_string(),
    };
    let killed_text = if killed {
        "已终止本次启动的后台进程。"
    } else {
        "没有需要终止的后台进程（或终止失败）。"
    };
    let text = format!("QIO 的后端没有启动起来，应用无法工作。\n\n原因：{reason}\n{killed_text}\n{where_text}");
    show_startup_error_dialog(&text);
    std::process::exit(1)
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
            // 壳为了「直连」判定把 NO_PROXY 固定成 *（见 updater_net::install_direct_fallback_guard）。
            // 那是**壳自己**的更新策略，不能泄漏给后端：后端/工具链自己的 HTTP 客户端
            // 该按用户环境走代理，就还按用户环境走。
            .env("NO_PROXY", "")
            .env("no_proxy", "")
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
            // 同 debug 分支：壳的 NO_PROXY=* 只是壳的更新策略，不泄漏给后端。
            .env("NO_PROXY", "")
            .env("no_proxy", "")
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

/// 写「本安装实例」的所有权记录（安装目录 = 外壳 exe 所在目录）。
///
/// 按实例区分（2026-10-05 §7 修复）：
/// * **权威记录** = `sidecar.lease.<shell_pid>.json`（本实例自己一份，别人覆盖不到，
///   退出时也只删这一份）；
/// * 另外写一份旧版单文件名的**兼容镜像**（NSIS 卸载钩子、e2e 脚本、老帮助程序都认它），
///   但如果这个名字已被**另一个还活着的实例**占着就不覆盖（否则又退回"后写覆盖先写"）。
/// * 旧版文件名写不下（安装目录不可写 / 被预建成目录）**仍然按"归属记录写不下"处理**：
///   这是既有的 fail-closed 语义，也是验证脚本构造真实失败路径的方式，不许改成忽略。
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
    let path = ownership::write_instance_lease(&install_dir, &lease)
        .map_err(|err| format!("写本实例记录失败：{err}"))?;
    match ownership::write_legacy_mirror(&install_dir, &lease) {
        Ok(Some(mirror)) => log::info!(
            "[qio] 已写 sidecar lease：shell pid {shell_pid} / backend pid {backend_pid} → {}（兼容镜像 {}）",
            path.display(),
            mirror.display()
        ),
        Ok(None) => log::info!(
            "[qio] 已写 sidecar lease：shell pid {shell_pid} / backend pid {backend_pid} → {}（旧版镜像已被另一个活实例占用，未覆盖）",
            path.display()
        ),
        Err(err) => return Err(format!("写旧版 lease 镜像失败：{err}")),
    }
    Ok(path)
}

/// 同一安装目录重复启动时的识别与清理（不阻断合法实例，也不覆盖别人的记录）。
fn note_and_prune_instances(install_dir: &Path, own_shell_pid: u32) {
    let removed = ownership::prune_dead_instance_leases(install_dir);
    if !removed.is_empty() {
        log::info!(
            "[qio] 清掉 {} 份已退出实例留下的所有权记录",
            removed.len()
        );
    }
    let others = ownership::live_instances(install_dir, own_shell_pid);
    if !others.is_empty() {
        let listed: Vec<String> = others
            .iter()
            .map(|candidate| {
                format!(
                    "shell pid {}（{}）",
                    candidate.lease.shell.pid,
                    candidate
                        .path
                        .file_name()
                        .map(|name| name.to_string_lossy().to_string())
                        .unwrap_or_default()
                )
            })
            .collect();
        log::warn!(
            "[qio] 同一安装目录已有 {} 个实例在跑：{}；本实例继续启动，各自写自己的记录（互不覆盖）",
            listed.len(),
            listed.join("、")
        );
    }
}

/// 启动准备（全部在后台线程/任务里）：代理探测 → 模型准备 → 拉起后端。
///
/// 窗口先可用（可拖动 / 缩放 / 关闭）：setup 只做日志与状态注册，耗时工作不占主线程。
/// 只有**真实依赖**被保留顺序：模型目录 → 后端启动 → job 指派 → 归属记录。
fn start_backend_in_background(app: tauri::AppHandle, runtime: Arc<ShellRuntime>) {
    // 1) 代理探测：与后端启动**无关**，不作为后端启动的前置条件（改代理也不用重启后端）
    tauri::async_runtime::spawn_blocking(move || {
        let file = std::fs::read_to_string(data_dir().join("updater-proxy.txt")).ok();
        let started = Instant::now();
        let plan = resolve_updater_proxy(file);
        let summary = format!("{}（{}）", plan.source.label(), plan.detail);
        log::info!(
            "[qio] 启动期代理探测完成：{summary}，耗时 {}ms",
            started.elapsed().as_millis()
        );
    });

    tauri::async_runtime::spawn(async move {
        let t_start = Instant::now();
        // 2) 内置模型 + 自带 Python 运行时：真实依赖（后端启动要用这两个目录）
        let app_for_prepare = app.clone();
        let runtime_for_prepare = Arc::clone(&runtime);
        let models_ms = tauri::async_runtime::spawn_blocking(move || {
            let started = Instant::now();
            let models = prepare_models(&app_for_prepare);
            let elapsed = started.elapsed().as_millis();
            let python = bundled_python_dir(&app_for_prepare);
            *lock(&runtime_for_prepare.models_dir) = models;
            *lock(&runtime_for_prepare.python_dir) = python;
            elapsed
        })
        .await
        .unwrap_or(0);
        log::info!("[qio] 启动准备：内置模型 {models_ms}ms（在后台线程完成，不占窗口主线程）");

        // 3) 拉起后端：spawn → job 指派 → 归属记录的顺序固定在 launch_backend_locked 里
        let app_for_launch = app.clone();
        let runtime_for_launch = Arc::clone(&runtime);
        let t_backend = Instant::now();
        let outcome = tauri::async_runtime::spawn_blocking(move || {
            runtime_for_launch.launch_backend_at_startup(&app_for_launch)
        })
        .await;
        match outcome {
            Ok(Ok(())) => {
                if let Some(pid) = runtime.backend_pid() {
                    log::info!(
                        "[qio] 后端已拉起：pid {pid}（拉起 {}ms / 启动合计 {}ms）",
                        t_backend.elapsed().as_millis(),
                        t_start.elapsed().as_millis()
                    );
                } else {
                    log::info!("[qio] 外壳在启动准备期间退出：本次没有拉起后端");
                }
            }
            Ok(Err(LaunchFailure::Spawn(reason))) => fatal_backend_start_failure(&reason, &runtime),
            Ok(Err(LaunchFailure::Lease { reason, pid })) => {
                // 归属记录写不下（fail-closed 已经收掉了后台）：与旧行为一致 —— 停下启动
                fatal_lease_failure(&reason, pid, runtime.job.as_deref(), None);
            }
            Err(join_error) => fatal_backend_start_failure(
                &format!("启动任务失败：{join_error}"),
                &runtime,
            ),
        }
    });
}

/// 退出：先关生命周期闸门（后台准备/恢复不许再拉起后端），再收进程与记录。
fn shutdown_runtime(runtime: &ShellRuntime) {
    let mut phase = lock(&runtime.phase);
    *phase = BackendPhase::Exiting;
    if let Some(child) = lock(&runtime.child).take() {
        if runtime.job_active.load(Ordering::SeqCst) {
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
    // 记录是「本安装实例还活着」的事实源：退出（任何路径）只删**自己那一份**
    // （+ 旧版镜像里记的正是自己时才删镜像）。别人的记录一律不动 ——
    // 旧实现删的是固定文件名，先退出的实例会把后启动实例的记录删掉（2026-10-05 §7 缺陷）。
    if let Some(path) = lock(&runtime.lease_path).take() {
        match ownership::remove_lease_at(&path) {
            Ok(()) => log::info!("[qio] 已删除本实例的 sidecar lease：{}", path.display()),
            Err(err) => log::warn!("[qio] 删除本实例 sidecar lease 失败：{err}"),
        }
    }
    if !cfg!(debug_assertions) {
        if let Some(install_dir) = ownership::shell_install_dir() {
            match ownership::remove_legacy_mirror_if_owned(&install_dir, std::process::id()) {
                Ok(true) => log::info!("[qio] 已删除旧版 sidecar lease 镜像（记的是本实例）"),
                Ok(false) => {}
                Err(err) => log::warn!("[qio] 删除旧版 sidecar lease 镜像失败：{err}"),
            }
        }
    }
    let _ = std::fs::remove_file(session_token_path());
}

fn main() {
    // 后端进程随这份 job 的生死而生死：壳没了，系统负责清干净，不留孤儿。
    let job = backend_job::create().map(Arc::new);

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

    let port = pick_free_port();
    let token_path = session_token_path();
    // 清掉上一轮的残留文件：令牌绝不跨进程生命周期复用。
    let _ = std::fs::remove_file(&token_path);
    let runtime = Arc::new(ShellRuntime {
        port,
        token_path,
        models_dir: Mutex::new(None),
        python_dir: Mutex::new(None),
        child: Mutex::new(None),
        lease_path: Mutex::new(None),
        residual_pids: Mutex::new(Vec::new()),
        job,
        job_active: AtomicBool::new(false),
        // 启动准备中：后台准备完成后才允许拉起后端（见 launch_backend_at_startup）
        phase: Mutex::new(BackendPhase::Preparing),
    });
    let runtime_for_setup = Arc::clone(&runtime);

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
            // 「直连」这个结论要真的生效：壳自己按操作决定代理，不让底层 HTTP 客户端再自动挑
            // 系统代理（reqwest 的 system-proxy 会直接读 Windows 设置，只清环境变量不够）。
            // 这不是「按操作改进程环境变量」—— 它是一次性常量，且后端子进程会显式清掉它
            // （见 backend_launch 里的 NO_PROXY=""）。
            install_direct_fallback_guard();

            app.manage(Arc::clone(&runtime_for_setup));
            // 窗口先可用：代理探测、模型准备、拉起后端全部在后台完成（见函数注释）。
            start_backend_in_background(app.handle().clone(), Arc::clone(&runtime_for_setup));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            qio_backend_info,
            qio_prepare_for_update,
            qio_restore_backend,
            qio_refresh_updater_proxy,
            // 附件：点击「附件」时的原生文件选择（Win32 对话框给真实路径；不新增 crate）
            attachment_picker::pick_attachment_file,
            attachment_picker::pick_attachment_file_available
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(move |_app, event| match event {
            RunEvent::Exit | RunEvent::ExitRequested { .. } => shutdown_runtime(&runtime),
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

    /// 造一个内置模型目录：清单里的 `bytes` 与 `sha256` 都是**真实**值
    /// （发布路径会逐字节核对，假的哈希连自己都发布不出去）。
    fn write_bundle(bundled: &Path, body: &[u8]) -> String {
        fs::create_dir_all(bundled).unwrap();
        fs::write(bundled.join("model.onnx"), body).unwrap();
        fs::write(bundled.join("tokenizer.json"), b"{}").unwrap();
        fs::write(bundled.join("NOTICE.txt"), b"notice").unwrap();
        let sha = sha256_file(&bundled.join("model.onnx")).unwrap();
        let manifest = serde_json::json!({
            "name": "bge-small-zh-v1.5",
            "default": "model.onnx",
            "files": [
                {"file": "model.onnx", "precision": "fp32", "bytes": body.len(), "sha256": sha}
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

    /// 目标目录里的临时文件（发布中途的残留）。
    fn temp_files_in(target: &Path) -> Vec<String> {
        fs::read_dir(target)
            .map(|entries| {
                entries
                    .flatten()
                    .map(|entry| entry.file_name().to_string_lossy().to_string())
                    .filter(|name| name.contains(PUBLISH_TEMP_MARK))
                    .collect()
            })
            .unwrap_or_default()
    }

    #[test]
    fn copies_bundle_into_data_dir_and_skips_second_time() {
        let root = temp_dir("copy");
        let bundled = root.join("bundled");
        let target = root.join("data").join("models").join("bge-small-zh-v1.5");
        let fingerprint = write_bundle(&bundled, b"fake-onnx-bytes");

        assert_eq!(sync_model_dir(&bundled, &target).unwrap(), SyncOutcome::Copied);
        assert!(target.join("model.onnx").exists());
        assert!(target.join("tokenizer.json").exists());
        // 许可证/声明是可选的：这里没放，也必须复制成功（不该因为它禁用语义检索）
        assert!(target.join("model_manifest.json").exists());
        assert_eq!(
            fs::read_to_string(target.join(".ready")).unwrap().trim(),
            fingerprint
        );
        // 原子发布：目录里不许留下临时文件
        assert!(
            temp_files_in(&target).is_empty(),
            "发布后不该有临时文件：{:?}",
            temp_files_in(&target)
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
        write_bundle(&bundled, b"old-model-bytes");
        sync_model_dir(&bundled, &target).unwrap();

        // 升级换了模型（哈希变了）→ 必须重写
        write_bundle(&bundled, b"new-model-bytes!!");
        assert_eq!(sync_model_dir(&bundled, &target).unwrap(), SyncOutcome::Copied);
        assert_eq!(
            fs::read_to_string(target.join(".ready")).unwrap().trim(),
            model_fingerprint(
                &serde_json::from_str(&fs::read_to_string(bundled.join("model_manifest.json")).unwrap())
                    .unwrap()
            )
        );
        assert_eq!(
            fs::read(target.join("model.onnx")).unwrap(),
            b"new-model-bytes!!".to_vec()
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
    fn a_matching_ready_marker_over_a_truncated_file_is_republished() {
        // 多实例共享数据目录时最危险的状态：`.ready` 匹配 + 文件存在，但内容是半成品。
        // 旧实现只查 exists() → 永久跳过复制（语义检索静默失效）；新实现必须重做。
        let root = temp_dir("truncated");
        let bundled = root.join("bundled");
        let target = root.join("data").join("models").join("bge-small-zh-v1.5");
        write_bundle(&bundled, b"fake-onnx-bytes");
        assert_eq!(sync_model_dir(&bundled, &target).unwrap(), SyncOutcome::Copied);

        // 模拟"另一个实例写到一半就崩了"：截断目标文件，`.ready` 原样留着
        fs::write(target.join("model.onnx"), b"fake").unwrap();
        assert_eq!(
            sync_model_dir(&bundled, &target).unwrap(),
            SyncOutcome::Copied,
            "半成品不得被当成就绪"
        );
        assert_eq!(
            fs::read(target.join("model.onnx")).unwrap(),
            b"fake-onnx-bytes".to_vec()
        );
        assert!(temp_files_in(&target).is_empty());
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn a_matching_ready_marker_over_same_size_wrong_content_is_caught_by_hash() {
        // 体积判据挡不住"同体积但内容不对"（例如并发混写）。清单声明了 sha256，
        // 发布/校验就必须用哈希 —— 这条用例把"至少大小，能做哈希更好"钉在哈希这一档。
        let root = temp_dir("hashcheck");
        let bundled = root.join("bundled");
        let target = root.join("data").join("models").join("bge-small-zh-v1.5");
        write_bundle(&bundled, b"fake-onnx-bytes");
        assert_eq!(sync_model_dir(&bundled, &target).unwrap(), SyncOutcome::Copied);

        fs::write(target.join("model.onnx"), b"FAKE-ONNX-BYTES").unwrap(); // 同体积，内容不同
        assert_eq!(
            sync_model_dir(&bundled, &target).unwrap(),
            SyncOutcome::Copied,
            "同体积的错内容必须靠哈希发现"
        );
        assert_eq!(
            fs::read(target.join("model.onnx")).unwrap(),
            b"fake-onnx-bytes".to_vec()
        );
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn a_failed_publish_leaves_no_ready_marker_and_no_temp_files() {
        // 发布失败（这里用"清单声明的哈希与内置内容不符"构造）→ 不许留下假 ready、
        // 不许留下临时文件；下次启动还能重做。
        let root = temp_dir("failpublish");
        let bundled = root.join("bundled");
        let target = root.join("data").join("models").join("bge-small-zh-v1.5");
        write_bundle(&bundled, b"fake-onnx-bytes");
        // 把清单里的哈希改成错的（内容不变）
        let manifest_path = bundled.join("model_manifest.json");
        let mut manifest: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&manifest_path).unwrap()).unwrap();
        manifest["files"][0]["sha256"] = serde_json::json!("0".repeat(64));
        fs::write(&manifest_path, serde_json::to_string(&manifest).unwrap()).unwrap();

        let err = sync_model_dir(&bundled, &target).unwrap_err();
        assert!(err.contains("哈希"), "失败原因要说清是哈希不符：{err}");
        assert!(
            !target.join(".ready").exists(),
            "失败之后不许留下 .ready（假 ready）"
        );
        assert!(temp_files_in(&target).is_empty(), "失败要清掉自己的临时文件");

        // 修好清单之后重做必须成功（可恢复）
        write_bundle(&bundled, b"fake-onnx-bytes");
        assert_eq!(sync_model_dir(&bundled, &target).unwrap(), SyncOutcome::Copied);
        assert_eq!(
            fs::read(target.join("model.onnx")).unwrap(),
            b"fake-onnx-bytes".to_vec()
        );
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn stale_publish_temps_are_cleaned_and_fresh_ones_are_left_alone() {
        // 崩溃残留（老）要清掉；另一实例正在写的（新）绝不能碰。
        let root = temp_dir("staletemp");
        let bundled = root.join("bundled");
        let target = root.join("data").join("models").join("bge-small-zh-v1.5");
        write_bundle(&bundled, b"fake-onnx-bytes");
        fs::create_dir_all(&target).unwrap();

        let stale = target.join(format!("model.onnx{PUBLISH_TEMP_MARK}999-1"));
        let fresh = target.join(format!("tokenizer.json{PUBLISH_TEMP_MARK}999-2"));
        fs::write(&stale, b"half").unwrap();
        fs::write(&fresh, b"in-progress").unwrap();
        // 把 stale 的修改时间拨到 2 小时前
        let old = std::time::SystemTime::now() - Duration::from_secs(2 * 60 * 60);
        fs::File::options()
            .write(true)
            .open(&stale)
            .unwrap()
            .set_modified(old)
            .unwrap();

        assert_eq!(sync_model_dir(&bundled, &target).unwrap(), SyncOutcome::Copied);
        assert!(!stale.exists(), "过期的临时文件要被清掉");
        assert!(fresh.exists(), "另一实例正在写的临时文件不许碰");
        let _ = fs::remove_dir_all(&root);
    }

    // 代理相关的测试（优先级、总预算、系统代理值解析、可达性、有界解析器）随实现一起
    // 挪到了 `updater_net`；跑命令的硬超时测试在 `qio_core::ownership`。
}
