//! 安装实例所有权（lease）：谁在跑、怎么只收「本安装实例自己的」进程。
//!
//! 为什么要这个模块（2026-10-03 A 项的两条实测事实）：
//! * 卸载钩子过去按**映像名**收 sidecar（NSIS_HOOK_PREUNINSTALL 里的
//!   CheckIfAppIsRunning "qio-backend.exe"）—— 两份 QIO 安装并存时，卸载 A 会连 B 的
//!   sidecar 一起杀掉；
//! * 运行中的 qio-backend.exe 删不掉也覆盖不了（只有 rename 能成功），所以卸载前必须
//!   真的把**本实例的**后端结束掉，但又不能碰别的实例。
//!
//! 唯一事实源 = 安装目录下的 sidecar.lease.json（外壳启动时写、退出时删）。
//! 字段（schema = 1）：
//!   * schema：固定 1，不认识的版本一律拒绝；
//!   * install_dir：安装目录绝对路径（外壳 exe 所在目录）；
//!   * shell：{ pid, created_filetime, exe }；
//!   * backend：{ pid, created_filetime, exe }；
//!   * started_at：UTC ISO8601 字符串，只是给人看的。
//!
//! 判定「这个 pid 就是当时那个进程」要**三件事同时匹配**：pid + 创建时间（Windows
//! FILETIME，100ns 精度，外壳那份来自外壳进程自己的 GetProcessTimes）+ 映像路径。
//! 这就是抗 PID 复用的全部依据；两侧（外壳/后端）都用同一套三要素。
//!
//! 结束进程只允许 taskkill /PID <pid>（可加 /T），**绝不按映像名杀**：
//! taskkill /IM 之类不许出现在这条路径上。
//!
//! 这个文件被两个二进制共用（同一份实现，不是复制粘贴两份）：
//! * qio（src/main.rs，外壳）：写 / 删 lease；
//! * qio-uninstall-helper（src/bin/qio-uninstall-helper.rs，卸载器调用的帮助程序）：
//!   读 lease、验身份、只收本实例的进程。
//!
//! 两个入口：
//! * close_installation：判定 + 收进程（默认严格：backend 记录必须完全对得上；
//!   allow_main_exe = true 时允许只凭壳的三要素收壳，后端记录对不上就不碰它）；
//! * check_installation：**只读**判定（--check-only），一个进程都不碰。
//!
//! 原则是**宁可不杀**。

use std::fmt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

/// lease 的 schema 版本。帮助程序只认这一个值；不认识的版本一律拒绝（宁可不杀）。
pub const LEASE_SCHEMA: u32 = 1;
/// lease 文件名：固定放在安装目录下（与外壳 exe 同目录）。
pub const LEASE_FILE_NAME: &str = "sidecar.lease.json";

/// 结束进程的硬超时：taskkill 卡住不能拖住卸载流程（本机实测它报过 0xC0000142）。
const KILL_TIMEOUT: Duration = Duration::from_secs(5);
/// 发出结束动作之后，等进程真正消失的上限。
const KILL_VERIFY_TIMEOUT: Duration = Duration::from_secs(5);
/// 轮询间隔。
const POLL_INTERVAL: Duration = Duration::from_millis(100);
/// 外壳已经不在了之后，留给「job 关句柄 → 系统收树」的时间（这两件事是异步的）。
const SHELL_GONE_GRACE: Duration = Duration::from_secs(3);

// ---------------------------------------------------------------------------
// 跑系统命令：硬超时（从 main.rs 挪进来共享 —— 帮助程序也要用同一套语义）
// ---------------------------------------------------------------------------

/// 跑一条命令并等它结束，超时就结束子进程并按「拿不到输出」处理。
///
/// 为什么必须有硬超时：Command::output() 会一直等子进程退出。2026-09-24 实测 ——
/// reg.exe 报 0xC0000142（DLL 初始化失败）并弹出"必须先点掉"的模态框，子进程因此
/// 不退出，外壳启动流程被钉住 1 分 42 秒。taskkill 在这台机器上也报过同样的错。
pub fn run_command_with_timeout(
    cmd: &mut std::process::Command,
    timeout: Duration,
) -> Option<std::process::Output> {
    use std::process::Stdio;

    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = cmd.spawn().ok()?;
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return child.wait_with_output().ok(),
            Ok(None) => {
                if Instant::now() >= deadline {
                    // 超时就结束子进程：Windows 上「卡住的系统程序」往往挂着模态框，
                    // 不杀掉它会连带把调用方一起钉住
                    let _ = child.kill();
                    let _ = child.wait();
                    return None;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(_) => return None,
        }
    }
}

// ---------------------------------------------------------------------------
// lease 数据结构与读写
// ---------------------------------------------------------------------------

/// 进程身份（lease 里的 shell / backend 条目）：三要素。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LeaseProcess {
    pub pid: u32,
    /// 进程创建时间：Windows FILETIME（100ns，UTC）的十进制字符串。
    /// 外壳这一份必须是**外壳主进程自己**的（GetCurrentProcessId + GetProcessTimes）。
    pub created_filetime: String,
    /// 映像路径（Win32 形式）。记下来才能凑齐三要素，防止 PID 复用被当成"就是它"。
    pub exe: String,
}

/// 后端进程的身份（lease 里的 backend 条目）。字段与 shell 相同，单独一个类型是为了
/// 让「backend 才是锁住 qio-backend.exe 的那个进程」这件事在类型上看得见。
pub type LeaseBackend = LeaseProcess;

/// 一份完整的 lease。内容里**没有**任何密钥/令牌（它只是一个所有权记录）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Lease {
    pub schema: u32,
    pub install_dir: String,
    pub shell: LeaseProcess,
    pub backend: LeaseBackend,
    pub started_at: String,
}

/// lease 判定失败的原因（机器可读，进 --json 与日志；不含敏感信息）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LeaseErrorKind {
    /// 文件不存在
    Missing,
    /// 文件存在但读不出来（权限/IO）
    Unreadable,
    /// 不是合法 JSON，或字段缺失
    InvalidJson,
    /// schema 不是 1（不认识的版本）
    SchemaMismatch,
    /// lease 里的 install_dir 与调用方传入的安装目录不是同一个
    InstallDirMismatch,
}

impl LeaseErrorKind {
    /// 机器可读的 reason 字符串。
    pub fn reason(self) -> &'static str {
        match self {
            LeaseErrorKind::Missing => "lease_missing",
            LeaseErrorKind::Unreadable => "lease_unreadable",
            LeaseErrorKind::InvalidJson => "lease_invalid",
            LeaseErrorKind::SchemaMismatch => "lease_schema_mismatch",
            LeaseErrorKind::InstallDirMismatch => "install_dir_mismatch",
        }
    }
}

#[derive(Debug, Clone)]
pub struct LeaseError {
    pub kind: LeaseErrorKind,
    pub detail: String,
}

impl fmt::Display for LeaseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}（{}）", self.detail, self.kind.reason())
    }
}

impl std::error::Error for LeaseError {}

/// lease 的固定路径：<install_dir>\sidecar.lease.json。
pub fn lease_path(install_dir: &Path) -> PathBuf {
    install_dir.join(LEASE_FILE_NAME)
}

/// 外壳 exe 所在目录 = 本安装实例的安装目录。
pub fn shell_install_dir() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    exe.parent().map(|dir| dir.to_path_buf())
}

/// 路径归一化，**只用于比较**（不用于拼路径）。
///
/// Windows 路径大小写不敏感；canonicalize 会带上 \\?\ 前缀；调用方传进来的目录
/// 可能带结尾分隔符。这三件事都不该让「同一个目录」被判成两个。
pub fn normalize_dir(path: &Path) -> String {
    normalize_path_str(&path.to_string_lossy())
}

fn normalize_path_str(raw: &str) -> String {
    let mut text = raw.trim().replace('/', "\\");
    if let Some(rest) = text.strip_prefix(r"\\?\") {
        text = rest.to_string();
    }
    while text.len() > 3 && text.ends_with('\\') {
        text.pop();
    }
    text.to_ascii_lowercase()
}

/// 两个路径是否指向同一个文件/目录（只做上面的归一化后比较）。
fn same_path(a: &str, b: &str) -> bool {
    normalize_path_str(a) == normalize_path_str(b)
}

/// 写 lease：先写临时文件再原子替换（rename 在 Windows 上是 MoveFileEx + REPLACE_EXISTING）。
///
/// 为什么必须原子：卸载器随时可能读这份文件，半夜读到写了一半的 JSON 就会把
/// 「本实例还活着」误判成「lease 非法 → 宁可不杀」，该收的进程收不掉。
pub fn write_lease(install_dir: &Path, lease: &Lease) -> Result<PathBuf, String> {
    if lease.schema != LEASE_SCHEMA {
        return Err(format!(
            "拒绝写 schema={} 的 lease（只写 {LEASE_SCHEMA}）",
            lease.schema
        ));
    }
    std::fs::create_dir_all(install_dir)
        .map_err(|err| format!("建目录 {} 失败：{err}", install_dir.display()))?;
    let final_path = lease_path(install_dir);
    let tmp_path = install_dir.join(format!("{LEASE_FILE_NAME}.tmp{}", std::process::id()));
    let mut body =
        serde_json::to_string_pretty(lease).map_err(|err| format!("序列化 lease 失败：{err}"))?;
    body.push('\n');
    std::fs::write(&tmp_path, body.as_bytes())
        .map_err(|err| format!("写临时文件 {} 失败：{err}", tmp_path.display()))?;
    if let Err(err) = std::fs::rename(&tmp_path, &final_path) {
        let _ = std::fs::remove_file(&tmp_path);
        return Err(format!("原子替换 {} 失败：{err}", final_path.display()));
    }
    Ok(final_path)
}

/// 读 lease 并做全部静态校验：存在 / 可读 / 合法 JSON / schema=1 / install_dir 与传入目录一致。
pub fn read_lease(install_dir: &Path) -> Result<Lease, LeaseError> {
    let path = lease_path(install_dir);
    let raw = match std::fs::read_to_string(&path) {
        Ok(raw) => raw,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            return Err(LeaseError {
                kind: LeaseErrorKind::Missing,
                detail: format!("{} 不存在", path.display()),
            })
        }
        Err(err) => {
            return Err(LeaseError {
                kind: LeaseErrorKind::Unreadable,
                detail: format!("读 {} 失败：{err}", path.display()),
            })
        }
    };
    let lease: Lease = serde_json::from_str(&raw).map_err(|err| LeaseError {
        kind: LeaseErrorKind::InvalidJson,
        detail: format!("{} 不是合法的 lease JSON：{err}", path.display()),
    })?;
    if lease.schema != LEASE_SCHEMA {
        return Err(LeaseError {
            kind: LeaseErrorKind::SchemaMismatch,
            detail: format!(
                "lease schema={}，本帮助程序只认 {LEASE_SCHEMA}",
                lease.schema
            ),
        });
    }
    if normalize_dir(Path::new(&lease.install_dir)) != normalize_dir(install_dir) {
        return Err(LeaseError {
            kind: LeaseErrorKind::InstallDirMismatch,
            detail: format!(
                "lease 记的安装目录是 {}，本次卸载的是 {}",
                lease.install_dir,
                install_dir.display()
            ),
        });
    }
    Ok(lease)
}

/// 删 lease（幂等：文件本来就不在也算成功）。
pub fn remove_lease_at(path: &Path) -> Result<(), String> {
    match std::fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(err) => Err(format!("删 {} 失败：{err}", path.display())),
    }
}

/// 删安装目录下的 lease（幂等）。
pub fn remove_lease(install_dir: &Path) -> Result<(), String> {
    remove_lease_at(&lease_path(install_dir))
}

/// 由外壳调用：把「本实例」的进程身份读出来，组成一份 lease 内容。
///
/// 读不到任何一项身份就**不写**：写一份没法核验的 lease 等于给卸载器一个假的把柄。
/// shell_pid 传外壳主进程自己的 pid（std::process::id()），创建时间由这里用
/// GetProcessTimes 读出来 —— 即"外壳主进程自己的创建时间"，不是任何子进程的。
pub fn lease_for_current_processes(
    install_dir: &Path,
    shell_pid: u32,
    backend_pid: u32,
    backend_exe: Option<String>,
) -> Result<Lease, String> {
    let shell_filetime = process_created_filetime(shell_pid).ok_or_else(|| {
        format!("读不到外壳进程（pid {shell_pid}）的创建时间，不写无法核验的 lease")
    })?;
    let shell_exe = process_image_path(shell_pid).ok_or_else(|| {
        format!("读不到外壳进程（pid {shell_pid}）的映像路径，不写无法核验的 lease")
    })?;
    let backend_filetime = process_created_filetime(backend_pid).ok_or_else(|| {
        format!("读不到后端进程（pid {backend_pid}）的创建时间，不写无法核验的 lease")
    })?;
    let backend_exe = match backend_exe {
        Some(exe) => exe,
        None => process_image_path(backend_pid).ok_or_else(|| {
            format!("读不到后端进程（pid {backend_pid}）的映像路径，不写无法核验的 lease")
        })?,
    };
    Ok(Lease {
        schema: LEASE_SCHEMA,
        install_dir: install_dir.to_string_lossy().to_string(),
        shell: LeaseProcess {
            pid: shell_pid,
            created_filetime: shell_filetime,
            exe: shell_exe,
        },
        backend: LeaseBackend {
            pid: backend_pid,
            created_filetime: backend_filetime,
            exe: backend_exe,
        },
        started_at: iso8601_utc(SystemTime::now()),
    })
}

/// UTC 时间戳（2026-10-03T02:00:00Z）。自己算，不引入时间库：
/// 这里只需要一个可读的「什么时候开始的」，不值得为它加依赖。
pub fn iso8601_utc(now: SystemTime) -> String {
    let secs = now
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        rem / 3600,
        (rem % 3600) / 60,
        rem % 60
    )
}

/// 把「1970-01-01 起的天数」换成公历年月日（Howard Hinnant 的 civil_from_days）。
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let year = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32; // [1, 12]
    (if month <= 2 { year + 1 } else { year }, month, day)
}

// ---------------------------------------------------------------------------
// 进程身份：句柄 + 三要素
// ---------------------------------------------------------------------------

/// 一个「已打开」的进程句柄。
///
/// 为什么要持有句柄而不是每次按 pid 重查：**句柄在手时这个 pid 不会被系统复用**
/// （进程对象还有引用，PID 属于对象）。于是「验身份 → 结束它」之间不存在 PID 复用窗口，
/// 这比「查一次再 taskkill /PID」更强 —— 卸载时误杀别人是不可接受的。
#[cfg(windows)]
pub struct ProcessGuard {
    handle: windows_sys::Win32::Foundation::HANDLE,
    pid: u32,
    /// 打开时是否拿到了 PROCESS_TERMINATE（拿不到仍然可以读身份，只是不能用句柄结束）
    can_terminate: bool,
}

// 句柄只在调用线程内使用；显式声明是为了能放进闭包/结构体里跨线程传递
#[cfg(windows)]
unsafe impl Send for ProcessGuard {}
#[cfg(windows)]
unsafe impl Sync for ProcessGuard {}

#[cfg(windows)]
impl ProcessGuard {
    /// 按 pid 打开。进程不存在 → None。
    ///
    /// 优先连 PROCESS_TERMINATE 一起申请（这样"结束它"可以走句柄，不依赖 taskkill 能否
    /// 在进程列表里看到这个 pid）；拿不到就退回只查询权限 —— 身份判定照常，只是不能用句柄结束。
    pub fn open(pid: u32) -> Option<ProcessGuard> {
        use windows_sys::Win32::System::Threading::{
            OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_TERMINATE,
        };
        unsafe {
            let full = OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE,
                0,
                pid,
            );
            if !full.is_null() {
                return Some(ProcessGuard {
                    handle: full,
                    pid,
                    can_terminate: true,
                });
            }
            let query_only = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
            if query_only.is_null() {
                return None;
            }
            Some(ProcessGuard {
                handle: query_only,
                pid,
                can_terminate: false,
            })
        }
    }

    /// 用**句柄**结束这个进程（TerminateProcess）。
    ///
    /// 为什么需要它（2026-10-03 实测）：taskkill 靠"能在进程列表里看到这个 pid"工作，
    /// 而在受限环境里有些进程它看不到（taskkill 报 "The process ... not found"），
    /// 可我们手里明明握着这个进程的句柄。句柄路径还更强：句柄在手 → 身份已核验过，
    /// 且这个 pid 不会被系统复用（进程对象仍被引用）。
    ///
    /// 注意：它只结束**这一个**进程，不带 /T。所以只用于外壳（外壳一死，它的 job 句柄
    /// 随进程消失 → 整棵树由系统收）；后端仍然只走 taskkill /T，避免留下 onefile 的 child。
    pub fn terminate(&self) -> (bool, String) {
        use windows_sys::Win32::System::Threading::TerminateProcess;
        if !self.can_terminate {
            return (false, "打开时没拿到 PROCESS_TERMINATE 权限".to_string());
        }
        let ok = unsafe { TerminateProcess(self.handle, 1) };
        if ok != 0 {
            (true, "TerminateProcess(句柄)".to_string())
        } else {
            (
                false,
                format!(
                    "TerminateProcess 失败：{}",
                    std::io::Error::last_os_error()
                ),
            )
        }
    }

    pub fn pid(&self) -> u32 {
        self.pid
    }

    /// 创建时间（FILETIME 十进制字符串）。用**句柄**读，不受 PID 复用影响。
    pub fn created_filetime(&self) -> Option<String> {
        use windows_sys::Win32::Foundation::FILETIME;
        use windows_sys::Win32::System::Threading::GetProcessTimes;
        unsafe {
            let mut creation: FILETIME = std::mem::zeroed();
            let mut exit: FILETIME = std::mem::zeroed();
            let mut kernel: FILETIME = std::mem::zeroed();
            let mut user: FILETIME = std::mem::zeroed();
            if GetProcessTimes(self.handle, &mut creation, &mut exit, &mut kernel, &mut user) == 0 {
                return None;
            }
            Some(filetime_to_decimal(&creation))
        }
    }

    /// 映像路径（QueryFullProcessImageNameW，Win32 形式）。用**句柄**读。
    pub fn exe(&self) -> Option<String> {
        use windows_sys::Win32::System::Threading::QueryFullProcessImageNameW;
        unsafe {
            let mut buf = vec![0u16; 32_768];
            let mut size = buf.len() as u32;
            if QueryFullProcessImageNameW(self.handle, 0, buf.as_mut_ptr(), &mut size) == 0 {
                return None;
            }
            buf.truncate(size as usize);
            Some(String::from_utf16_lossy(&buf))
        }
    }

    /// 进程是否还活着：看这个**句柄**的退出码是不是 STILL_ACTIVE。
    ///
    /// 不能只看 OpenProcess 成不成功：我们自己持有句柄时，进程退出后进程对象仍然存在，
    /// OpenProcess 照样成功 —— 那会把「已经死了」判成「还活着」。
    pub fn is_alive(&self) -> bool {
        use windows_sys::Win32::Foundation::STILL_ACTIVE;
        use windows_sys::Win32::System::Threading::GetExitCodeProcess;
        unsafe {
            let mut code: u32 = 0;
            if GetExitCodeProcess(self.handle, &mut code) == 0 {
                return false;
            }
            code == STILL_ACTIVE as u32
        }
    }

    /// 三要素是否与 lease 记录一致（pid 由 open 保证，剩下创建时间 + 映像路径）。
    pub fn matches(&self, created_filetime: &str, exe: &str) -> bool {
        match (self.created_filetime(), self.exe()) {
            (Some(filetime), Some(path)) => filetime == created_filetime && same_path(&path, exe),
            _ => false,
        }
    }
}

#[cfg(windows)]
impl Drop for ProcessGuard {
    fn drop(&mut self) {
        unsafe { windows_sys::Win32::Foundation::CloseHandle(self.handle) };
    }
}

/// 非 Windows 占位：本产品只在 Windows 上安装，但 lib 要能跨平台编译（同 main.rs 的写法）。
#[cfg(not(windows))]
pub struct ProcessGuard {
    pid: u32,
}

#[cfg(not(windows))]
impl ProcessGuard {
    pub fn open(_pid: u32) -> Option<ProcessGuard> {
        None
    }
    pub fn pid(&self) -> u32 {
        self.pid
    }
    pub fn created_filetime(&self) -> Option<String> {
        None
    }
    pub fn exe(&self) -> Option<String> {
        None
    }
    pub fn is_alive(&self) -> bool {
        false
    }
    pub fn matches(&self, _created_filetime: &str, _exe: &str) -> bool {
        false
    }
    pub fn terminate(&self) -> (bool, String) {
        (false, "非 Windows 平台不支持".to_string())
    }
}

#[cfg(windows)]
fn filetime_to_decimal(ft: &windows_sys::Win32::Foundation::FILETIME) -> String {
    (((ft.dwHighDateTime as u64) << 32) | ft.dwLowDateTime as u64).to_string()
}

/// 按 pid 读创建时间（FILETIME 十进制字符串）。进程不存在或读不到 → None。
pub fn process_created_filetime(pid: u32) -> Option<String> {
    ProcessGuard::open(pid)?.created_filetime()
}

/// 按 pid 读映像路径（Win32 形式）。进程不存在或读不到 → None。
pub fn process_image_path(pid: u32) -> Option<String> {
    ProcessGuard::open(pid)?.exe()
}

/// 一个 pid 的探测结果。
enum Probe {
    /// pid 上没有进程（已经退出 / 打不开）
    Gone,
    /// pid 上有进程，但身份对不上（PID 复用、映像路径不同）—— 附上实际身份供诊断
    Foreign(String),
    /// 三要素都对得上；句柄在手可以判存活（可能已退出，但进程对象还在）
    Ours(ProcessGuard),
}

fn probe_process(pid: u32, created_filetime: &str, exe: &str) -> Probe {
    match ProcessGuard::open(pid) {
        None => Probe::Gone,
        Some(guard) => {
            let actual_filetime = guard.created_filetime();
            let actual_exe = guard.exe();
            let filetime_ok = actual_filetime.as_deref() == Some(created_filetime);
            let exe_ok = actual_exe
                .as_deref()
                .map(|path| same_path(path, exe))
                .unwrap_or(false);
            if filetime_ok && exe_ok {
                return Probe::Ours(guard);
            }
            // 精确到字段：卸载日志里必须一眼看出是"创建时间"还是"映像路径"对不上，
            // 否则只能靠猜（第一版只打印整串，排查时吃过亏）。
            let mut parts: Vec<String> = Vec::new();
            if !filetime_ok {
                parts.push(format!(
                    "创建时间不符：lease={created_filetime} 实际={}",
                    actual_filetime.unwrap_or_else(|| "读不到".to_string())
                ));
            }
            if !exe_ok {
                parts.push(format!(
                    "映像路径不符：lease={exe} 实际={}",
                    actual_exe.unwrap_or_else(|| "读不到".to_string())
                ));
            }
            Probe::Foreign(parts.join("；"))
        }
    }
}

/// 身份三要素对得上（不论死活）。
fn identity_ok(probe: &Probe) -> bool {
    matches!(probe, Probe::Ours(_))
}

/// 身份对得上**且**还活着。
fn alive_ok(probe: &Probe) -> bool {
    matches!(probe, Probe::Ours(guard) if guard.is_alive())
}

/// 取走「身份对得上且活着」的句柄；其他情况返回 None（= 没有可收的目标）。
fn take_alive(probe: Probe) -> Option<ProcessGuard> {
    match probe {
        Probe::Ours(guard) if guard.is_alive() => Some(guard),
        _ => None,
    }
}

/// 给人看的短说明（进报告与日志）。
fn describe_probe(probe: &Probe, label: &str, pid: u32) -> String {
    match probe {
        Probe::Gone => format!("{label} pid {pid} 已经不在"),
        Probe::Foreign(actual) => format!("{label} pid {pid} 的身份对不上（{actual}）"),
        Probe::Ours(guard) if guard.is_alive() => String::new(),
        Probe::Ours(_) => format!("{label} pid {pid} 已经退出"),
    }
}

// ---------------------------------------------------------------------------
// 结束进程：只按 pid（可带 /T）
// ---------------------------------------------------------------------------

/// 跑一次 taskkill，返回（是否成功，说明）。
///
/// 说明里带上 taskkill 自己的输出：卸载日志里"为什么没杀掉"必须看得见原因
/// （Access denied / 找不到进程 / 超时），而不是只有一句"失败了"。
fn taskkill(args: &[&str]) -> (bool, String) {
    let mut cmd = std::process::Command::new("taskkill");
    cmd.args(args);
    match run_command_with_timeout(&mut cmd, KILL_TIMEOUT) {
        Some(out) => {
            let stdout = String::from_utf8_lossy(&out.stdout).trim().to_string();
            let stderr = String::from_utf8_lossy(&out.stderr).trim().to_string();
            let note = match (stdout.is_empty(), stderr.is_empty()) {
                (false, false) => format!("{stdout} {stderr}"),
                (false, true) => stdout,
                (true, false) => stderr,
                (true, true) => format!("退出码 {:?}", out.status.code()),
            };
            (out.status.success(), note)
        }
        None => (
            false,
            format!("taskkill {} 超时或启动失败", args.join(" ")),
        ),
    }
}

/// 按 **pid** 结束整棵进程树（taskkill /PID <pid> /T /F）。
///
/// 为什么必须带 /T：onefile 的 launcher 被杀掉之后，真正提供服务的 child 还活着，
/// 它会锁住 qio-backend.exe，安装器报 Can't write（同 main.rs 里的注释）。
/// 为什么绝不按名字：开发实例、测试实例、其它安装实例都不能被误伤。
pub fn kill_tree_by_pid(pid: u32) -> bool {
    kill_tree_by_pid_verbose(pid).0
}

/// 同上，但把 taskkill 的说明一起带回来（卸载日志要能看见"为什么没杀掉"）。
pub fn kill_tree_by_pid_verbose(pid: u32) -> (bool, String) {
    taskkill(&["/PID", &pid.to_string(), "/T", "/F"])
}

/// 按 **pid** 结束单个进程（taskkill /PID <pid> /F），不带 /T。
pub fn kill_pid(pid: u32) -> bool {
    kill_pid_verbose(pid).0
}

/// 同上，但带 taskkill 的说明。
pub fn kill_pid_verbose(pid: u32) -> (bool, String) {
    taskkill(&["/PID", &pid.to_string(), "/F"])
}

// ---------------------------------------------------------------------------
// 窗口：给外壳发 WM_CLOSE（优雅退出路径）
// ---------------------------------------------------------------------------

/// 向 pid 拥有的顶层窗口发 WM_CLOSE；投递成功 → true。
///
/// 为什么是「pid 拥有的窗口」而不是广播：绝不误关用户正在用的其它窗口。
/// 为什么要挑窗口：进程可能有多个顶层窗口（WebView2 的消息泵、伪控制台……），
/// 关错了那个不会让应用退出，于是白等一整个超时。挑法 = 打分：
/// 可见(+4) + 有标题(+2) + 不是工具窗口(+1)，取分最高者，并列时取先枚举到的（Z 序）。
#[cfg(windows)]
pub fn post_close_to_pid(pid: u32) -> bool {
    let Some(hwnd) = best_window_for_pid(pid) else {
        return false;
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::{PostMessageW, WM_CLOSE};
    unsafe { PostMessageW(hwnd, WM_CLOSE, 0, 0) != 0 }
}

#[cfg(not(windows))]
pub fn post_close_to_pid(_pid: u32) -> bool {
    false
}

/// pid 是否有可用的顶层窗口（测试夹具等窗口出现时用；与判定用的是同一套挑选逻辑）。
#[cfg(windows)]
pub fn has_top_level_window(pid: u32) -> bool {
    best_window_for_pid(pid).is_some()
}

#[cfg(not(windows))]
pub fn has_top_level_window(_pid: u32) -> bool {
    false
}

/// 明确不碰的辅助窗口类名：它们不是应用主窗口，关了也不会让外壳退出。
#[cfg(windows)]
fn is_helper_window_class(class: &str) -> bool {
    const HELPERS: [&str; 6] = [
        "Chrome_MessagePumpWindow",
        "PseudoConsoleWindow",
        "GDI+ Hook Window Class",
        "Default IME",
        "MSCTFIME UI",
        "IME",
    ];
    HELPERS.iter().any(|helper| class.eq_ignore_ascii_case(helper))
        || class.starts_with(".NET-BroadcastEventWindow")
}

#[cfg(windows)]
fn best_window_for_pid(pid: u32) -> Option<windows_sys::Win32::Foundation::HWND> {
    use windows_sys::Win32::Foundation::{HWND, LPARAM};
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        EnumWindows, GetClassNameW, GetWindowLongW, GetWindowTextW, GetWindowThreadProcessId,
        IsWindowVisible, GWL_EXSTYLE, WS_EX_TOOLWINDOW,
    };

    struct Ctx {
        pid: u32,
        best: HWND,
        best_score: i32,
    }

    unsafe extern "system" fn visit(hwnd: HWND, lparam: LPARAM) -> i32 {
        let ctx = &mut *(lparam as *mut Ctx);
        let mut owner: u32 = 0;
        GetWindowThreadProcessId(hwnd, &mut owner);
        if owner == ctx.pid {
            let mut title = [0u16; 256];
            let title_len = GetWindowTextW(hwnd, title.as_mut_ptr(), title.len() as i32);
            let mut class_buf = [0u16; 256];
            let class_len = GetClassNameW(hwnd, class_buf.as_mut_ptr(), class_buf.len() as i32);
            let class = String::from_utf16_lossy(&class_buf[..class_len.max(0) as usize]);
            if !is_helper_window_class(&class) {
                let visible = IsWindowVisible(hwnd) != 0;
                let has_title = title_len > 0;
                let is_tool = (GetWindowLongW(hwnd, GWL_EXSTYLE) as u32 & WS_EX_TOOLWINDOW) != 0;
                let score = if visible { 4 } else { 0 }
                    + if has_title { 2 } else { 0 }
                    + if is_tool { 0 } else { 1 };
                if score > ctx.best_score {
                    ctx.best_score = score;
                    ctx.best = hwnd;
                }
            }
        }
        1
    }

    let mut ctx = Ctx {
        pid,
        best: std::ptr::null_mut(),
        best_score: -1,
    };
    unsafe {
        EnumWindows(Some(visit), &mut ctx as *mut Ctx as LPARAM);
    }
    if ctx.best.is_null() {
        None
    } else {
        Some(ctx.best)
    }
}

// ---------------------------------------------------------------------------
// 卸载时的判定与动作
// ---------------------------------------------------------------------------

/// 一次「收掉本安装实例」的请求。
#[derive(Debug, Clone)]
pub struct CloseRequest {
    /// 安装目录（帮助程序由 --install-dir 传入；lease 必须就在这里）。
    pub install_dir: PathBuf,
    /// 给外壳优雅退出的时间（对应 --timeout-ms）。
    pub timeout: Duration,
    /// true = 允许只凭壳的三要素收壳（对应 --allow-main-exe）：
    /// 即使 backend 记录已经对不上（后端崩了/被换过），也能把本实例的壳收掉。
    /// 默认 false = 严格：backend 记录必须完全对得上才动手。
    pub allow_main_exe: bool,
}

/// 判定结果（--json 就是把这一份打成一行；字段里没有敏感信息）。
#[derive(Debug, Clone, Serialize)]
pub struct CloseReport {
    /// 机器可读的判定：
    /// refused / would_close / already_stopped / closed / killed / failed
    pub action: String,
    /// 机器可读的原因。
    pub reason: String,
    /// 退出码：0 = 已处理（或 --check-only 判定"可收"）；3 = 无法确认归属 / 无法结束
    /// （不动任何进程 / 保留 lease）；2 = 参数错误（CLI 层）。
    pub exit_code: i32,
    pub install_dir: String,
    pub backend_pid: Option<u32>,
    pub shell_pid: Option<u32>,
    /// lease 记录的后端三要素是否与现有进程对上（不论死活）。
    pub backend_identity_ok: bool,
    /// 后端进程是否还活着。
    pub backend_alive: bool,
    /// lease 记录的外壳三要素是否与现有进程对上（不论死活）。
    pub shell_identity_ok: bool,
    /// 外壳进程是否还活着。
    pub shell_alive: bool,
    /// 是否给外壳投递过 WM_CLOSE。
    pub posted_close: bool,
    /// 是否按 pid 结束过后端整棵树。
    pub killed_backend: bool,
    /// 是否按 pid 结束过外壳。
    pub killed_shell: bool,
    /// 结束时 lease 是否已删除（外壳自己删掉也算）。
    pub lease_removed: bool,
    /// 人话（中文），给日志与 NSIS 输出用。
    pub message: String,
}

impl CloseReport {
    fn new(install_dir: &Path) -> CloseReport {
        CloseReport {
            action: "refused".to_string(),
            reason: String::new(),
            exit_code: 3,
            install_dir: install_dir.to_string_lossy().to_string(),
            backend_pid: None,
            shell_pid: None,
            backend_identity_ok: false,
            backend_alive: false,
            shell_identity_ok: false,
            shell_alive: false,
            posted_close: false,
            killed_backend: false,
            killed_shell: false,
            lease_removed: false,
            message: String::new(),
        }
    }
}

fn refuse(mut report: CloseReport, reason: &str, message: String) -> CloseReport {
    report.action = "refused".to_string();
    report.reason = reason.to_string();
    report.exit_code = 3;
    report.message = message;
    report
}

fn fail(mut report: CloseReport, reason: &str, message: String) -> CloseReport {
    report.action = "failed".to_string();
    report.reason = reason.to_string();
    report.exit_code = 3;
    report.message = message;
    report
}

/// 读 lease 并把两侧的探测结果填进报告；失败就返回拒绝报告。
fn read_and_probe(req: &CloseRequest, report: &mut CloseReport) ->
    Result<(Lease, Probe, Probe), CloseReport>
{
    let lease = match read_lease(&req.install_dir) {
        Ok(lease) => lease,
        Err(err) => {
            let text = format!("{err}：无法确认本安装实例还在不在，不动任何进程");
            return Err(refuse(report.clone(), err.kind.reason(), text));
        }
    };
    report.shell_pid = Some(lease.shell.pid);
    report.backend_pid = Some(lease.backend.pid);
    let backend = probe_process(
        lease.backend.pid,
        &lease.backend.created_filetime,
        &lease.backend.exe,
    );
    let shell = probe_process(
        lease.shell.pid,
        &lease.shell.created_filetime,
        &lease.shell.exe,
    );
    report.backend_identity_ok = identity_ok(&backend);
    report.backend_alive = alive_ok(&backend);
    report.shell_identity_ok = identity_ok(&shell);
    report.shell_alive = alive_ok(&shell);
    Ok((lease, backend, shell))
}

/// --check-only：**只读**判定，一个进程都不碰、一个文件都不删。
///
/// 退出码 0 = **本安装实例的壳正在运行**（三要素对得上且活着）→ 可以交给
/// close_installation（必要时配 --allow-main-exe）去收；
/// 退出码 3 = 无法确认 / 本实例没在运行 / 记录对不上。
pub fn check_installation(req: &CloseRequest) -> CloseReport {
    let mut report = CloseReport::new(&req.install_dir);
    let (lease, _backend, shell) = match read_and_probe(req, &mut report) {
        Ok(parts) => parts,
        Err(refused) => return refused,
    };
    let backend_note = describe_probe(&_backend, "后端", lease.backend.pid);
    match &shell {
        Probe::Ours(guard) if guard.is_alive() => {
            report.action = "would_close".to_string();
            report.reason = "shell_running".to_string();
            report.exit_code = 0;
            report.message = format!(
                "本安装实例的外壳正在运行（shell pid {}，三要素对得上），可收；后端：{}；--check-only 未动任何进程",
                lease.shell.pid,
                if backend_note.is_empty() {
                    format!("pid {} 身份对得上且活着", lease.backend.pid)
                } else {
                    backend_note
                }
            );
        }
        Probe::Ours(_) => {
            return refuse(
                report,
                "shell_exited",
                format!(
                    "lease 记录的外壳 pid {} 已经退出：本安装实例没在运行，不动任何进程",
                    lease.shell.pid
                ),
            )
        }
        Probe::Foreign(actual) => {
            return refuse(
                report,
                "shell_identity_mismatch",
                format!(
                    "外壳 pid {} 的身份对不上（{actual}）：像是 PID 被复用，不动任何进程",
                    lease.shell.pid
                ),
            )
        }
        Probe::Gone => {
            return refuse(
                report,
                "shell_exited",
                format!(
                    "lease 记录的外壳 pid {} 已经不存在：本安装实例没在运行，不动任何进程",
                    lease.shell.pid
                ),
            )
        }
    }
    report
}

/// 只结束「本安装实例自己的」进程。判定顺序（宁可不杀）：
///
/// 默认（allow_main_exe = false，严格）：
/// 1. lease 缺失 / 不可读 / 非法 JSON / schema≠1 / install_dir 不一致 → 不动任何进程，退出码 3；
/// 2. backend 三要素对不上（PID 复用、映像路径不同）→ 不动任何进程，退出码 3；
/// 3. backend 已经退出 / 不存在 → 不动任何进程，退出码 3（确认不了归属就如实报告，不猜）；
/// 4. 都对得上 → 先给外壳发 WM_CLOSE（外壳自己的退出路径会关 job → 收掉整棵树），
///    等 timeout；超时（或外壳已经不在了）才 taskkill /F /T /PID backend，
///    再 taskkill /F /T /PID shell（**只按 pid，且 pid 已经过三要素核验**）；
///    taskkill 收不掉外壳时（受限环境里它可能看不到这个 pid），退回用手里这个
///    **已核验身份的句柄**结束它（TerminateProcess，见 ProcessGuard::terminate）；
///    外壳有没有顶层窗口**不影响身份判定**：有窗口才发 WM_CLOSE，没有就跳过直接等超时；
/// 5. 结束动作之后确认不了 backend 已经死了 → 保留 lease、退出码 3，便于下次再试。
///
/// allow_main_exe = true（模板把「按名字杀 qio.exe」换成「先 --check-only，再收自己的壳」时用）：
/// 门槛换成**外壳**的三要素 —— 壳对得上且活着就收壳（先 WM_CLOSE，超时按 pid 结束）；
/// backend 记录仍然对得上就顺手一起收；对不上就**不碰它**（绝不动无法核验的进程）。
pub fn close_installation(req: &CloseRequest) -> CloseReport {
    let mut report = CloseReport::new(&req.install_dir);

    let (lease, backend_probe, shell_probe) = match read_and_probe(req, &mut report) {
        Ok(parts) => parts,
        Err(refused) => return refused,
    };

    if !req.allow_main_exe {
        // 严格模式：backend 记录必须完全对得上才动手
        match &backend_probe {
            Probe::Gone => {
                return refuse(
                    report,
                    "backend_exited",
                    format!(
                        "lease 记录的后端 pid {} 已经不存在（进程已退出）：没有可结束的目标，但无法确认当前状态，不动任何进程",
                        lease.backend.pid
                    ),
                )
            }
            Probe::Foreign(actual) => {
                return refuse(
                    report,
                    "backend_identity_mismatch",
                    format!(
                        "后端 pid {} 的身份对不上（{actual}）：像是 PID 被复用，不动任何进程",
                        lease.backend.pid
                    ),
                )
            }
            Probe::Ours(guard) if !guard.is_alive() => {
                return refuse(
                    report,
                    "backend_exited",
                    format!(
                        "lease 记录的后端 pid {} 已经退出：没有可结束的目标，但无法确认当前状态，不动任何进程",
                        lease.backend.pid
                    ),
                )
            }
            Probe::Ours(_) => {}
        }
    } else {
        // 允许只凭壳的身份收壳：不能没有壳，但 backend 记录坏掉不算错
        match &shell_probe {
            Probe::Gone => {
                return refuse(
                    report,
                    "shell_exited",
                    format!(
                        "lease 记录的外壳 pid {} 已经不存在（进程已退出）：没有可收的目标，不动任何进程",
                        lease.shell.pid
                    ),
                )
            }
            Probe::Foreign(actual) => {
                return refuse(
                    report,
                    "shell_identity_mismatch",
                    format!(
                        "外壳 pid {} 的身份对不上（{actual}）：像是 PID 被复用，不动任何进程",
                        lease.shell.pid
                    ),
                )
            }
            Probe::Ours(guard) if !guard.is_alive() => {
                return refuse(
                    report,
                    "shell_exited",
                    format!(
                        "lease 记录的外壳 pid {} 已经退出：本安装实例没在运行，不动任何进程",
                        lease.shell.pid
                    ),
                )
            }
            Probe::Ours(_) => {}
        }
    }

    let backend_note = describe_probe(&backend_probe, "后端", lease.backend.pid);
    let shell_note = describe_probe(&shell_probe, "外壳", lease.shell.pid);
    let shell = take_alive(shell_probe);
    let backend = take_alive(backend_probe);

    // 先请外壳自己退出：它的退出路径会关 job → 整棵树一起走
    if let Some(guard) = &shell {
        report.posted_close = post_close_to_pid(guard.pid());
    }

    let deadline = Instant::now() + req.timeout;
    let mut shell_gone_at: Option<Instant> = None;
    loop {
        let backend_dead = backend.as_ref().map(|g| !g.is_alive()).unwrap_or(true);
        let shell_alive = shell.as_ref().map(|g| g.is_alive()).unwrap_or(false);
        if backend_dead && !shell_alive {
            break;
        }
        if !shell_alive {
            // 外壳没了：不会再有人帮我们收树（job 已随外壳消失），等一小会儿就兜底
            let since = *shell_gone_at.get_or_insert_with(Instant::now);
            if since.elapsed() >= SHELL_GONE_GRACE {
                break;
            }
        } else {
            shell_gone_at = None;
        }
        if Instant::now() >= deadline {
            break;
        }
        std::thread::sleep(POLL_INTERVAL);
    }

    // 兜底：按 pid 结束（句柄在手 → 身份已核验过，且这个 pid 不会被复用）
    let mut kill_notes: Vec<String> = Vec::new();
    if let Some(guard) = &backend {
        if guard.is_alive() {
            let (ok, note) = kill_tree_by_pid_verbose(lease.backend.pid);
            report.killed_backend = ok;
            if !ok {
                kill_notes.push(format!("结束后端 pid {} 失败：{note}", lease.backend.pid));
            }
        }
    }
    if let Some(guard) = &shell {
        if guard.is_alive() {
            // 带 /T：外壳这一棵树（WebView2 子进程、还没被 job 收掉的 sidecar……）一起收。
            // 前提不变：pid 已经过三要素核验，且句柄在手（这个 pid 不会被复用）。
            let (ok, note) = kill_tree_by_pid_verbose(lease.shell.pid);
            report.killed_shell = ok;
            if !ok {
                kill_notes.push(format!(
                    "结束外壳 pid {} 整棵树失败：{note}",
                    lease.shell.pid
                ));
                // taskkill 看不到这个 pid 时（受限期进程列表里没有它），
                // 用手里这个**已核验身份的句柄**收 —— 外壳一死，它的 job 句柄随之消失，
                // 整棵树由系统收掉；所以这里不需要 /T。
                let (ok_by_handle, handle_note) = guard.terminate();
                if ok_by_handle {
                    report.killed_shell = true;
                    kill_notes.push(format!(
                        "已按句柄结束外壳 pid {}（{handle_note}）",
                        lease.shell.pid
                    ));
                } else {
                    kill_notes.push(format!(
                        "按句柄结束外壳 pid {} 也失败：{handle_note}",
                        lease.shell.pid
                    ));
                }
            }
        }
    }

    // 确认结束：确认不了就保留 lease、如实报告失败（下次还能再试）
    if let Some(guard) = &backend {
        let verify_until = Instant::now() + KILL_VERIFY_TIMEOUT;
        while guard.is_alive() && Instant::now() < verify_until {
            std::thread::sleep(POLL_INTERVAL);
        }
        if guard.is_alive() {
            let killed_backend = report.killed_backend;
            return fail(
                report,
                "backend_still_alive",
                format!(
                    "后端 pid {} 仍然活着（killed_backend={killed_backend}；{}）：保留 lease，退出码 3",
                    lease.backend.pid,
                    if kill_notes.is_empty() {
                        "没有可用的 taskkill 说明".to_string()
                    } else {
                        kill_notes.join("；")
                    }
                ),
            );
        }
    }
    if let Some(guard) = &shell {
        let verify_until = Instant::now() + KILL_VERIFY_TIMEOUT;
        while guard.is_alive() && Instant::now() < verify_until {
            std::thread::sleep(POLL_INTERVAL);
        }
        if guard.is_alive() {
            let killed_shell = report.killed_shell;
            return fail(
                report,
                "shell_still_alive",
                format!(
                    "外壳 pid {} 仍然活着（killed_shell={killed_shell}；{}）：保留 lease，退出码 3",
                    lease.shell.pid,
                    if kill_notes.is_empty() {
                        "没有可用的 taskkill 说明".to_string()
                    } else {
                        kill_notes.join("；")
                    }
                ),
            );
        }
    }

    // 成功：删掉 lease（外壳退出时也会删；这里删是为了「卸载后不留记录」）
    let lease_note = match remove_lease(&req.install_dir) {
        Ok(()) => {
            report.lease_removed = true;
            String::new()
        }
        Err(err) => format!("{err}；"),
    };
    report.exit_code = 0;
    report.action = if report.killed_backend || report.killed_shell {
        "killed".to_string()
    } else if report.posted_close {
        "closed".to_string()
    } else {
        "already_stopped".to_string()
    };
    report.reason = if report.killed_backend {
        "killed_by_pid".to_string()
    } else if report.killed_shell {
        "killed_shell_by_pid".to_string()
    } else if report.posted_close {
        "closed_by_wm_close".to_string()
    } else {
        "processes_already_gone".to_string()
    };
    let how = if report.killed_backend && report.killed_shell {
        format!(
            "按 pid 结束了后端整棵树（{}）与外壳（{}）",
            lease.backend.pid, lease.shell.pid
        )
    } else if report.killed_backend {
        format!("按 pid {} 结束了后端整棵树", lease.backend.pid)
    } else if report.killed_shell {
        format!("按 pid {} 结束了外壳（后端未动）", lease.shell.pid)
    } else if report.posted_close {
        format!("外壳 pid {} 收到 WM_CLOSE 后自行退出", lease.shell.pid)
    } else {
        "本实例的进程都已经结束".to_string()
    };
    let notes: Vec<String> = [backend_note, shell_note]
        .into_iter()
        .filter(|note| !note.is_empty())
        .collect();
    report.message = format!(
        "{lease_note}{how}（backend pid {} / shell pid {}{}）",
        lease.backend.pid,
        lease.shell.pid,
        if notes.is_empty() {
            String::new()
        } else {
            format!("；{}", notes.join("；"))
        }
    );
    report
}

// ---------------------------------------------------------------------------
// 测试：真进程 + 临时目录，不 mock 进程表
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // ---------- 纯逻辑（不需要进程） ----------

    #[test]
    fn iso8601_utc_formats_known_instants() {
        assert_eq!(iso8601_utc(UNIX_EPOCH), "1970-01-01T00:00:00Z");
        assert_eq!(
            iso8601_utc(UNIX_EPOCH + Duration::from_secs(1_000_000_000)),
            "2001-09-09T01:46:40Z"
        );
        // 2000-02-29（闰年）：946_684_800 是 2000-01-01，加 31 天 + 28 天
        assert_eq!(
            iso8601_utc(UNIX_EPOCH + Duration::from_secs(951_782_400)),
            "2000-02-29T00:00:00Z"
        );
    }

    #[test]
    fn normalize_dir_ignores_case_prefix_and_trailing_separator() {
        assert_eq!(
            normalize_dir(Path::new(r"C:\Program Files\QIO\")),
            normalize_dir(Path::new(r"\\?\c:\program files\qio"))
        );
        assert_ne!(
            normalize_dir(Path::new(r"C:\Program Files\QIO")),
            normalize_dir(Path::new(r"C:\Program Files\QIO2"))
        );
    }

    #[test]
    fn lease_write_read_delete_roundtrip_is_atomic_and_leaves_no_temp_files() {
        let dir = TempDir::new("roundtrip");
        let lease = lease_with(
            dir.path(),
            &(11, "133400000000000000".to_string(), r"C:\QIO\qio.exe".to_string()),
            &(
                22,
                "133400000012345678".to_string(),
                r"C:\QIO\qio-backend.exe".to_string(),
            ),
        );
        let path = write_lease(dir.path(), &lease).expect("写 lease 失败");
        assert_eq!(path, lease_path(dir.path()));
        assert_eq!(read_lease(dir.path()).expect("读 lease 失败"), lease);

        // 原子替换写：目录里只应该有 lease 本身，没有 .tmp 残留
        let mut names: Vec<String> = std::fs::read_dir(dir.path())
            .unwrap()
            .map(|entry| entry.unwrap().file_name().to_string_lossy().to_string())
            .collect();
        names.sort();
        assert_eq!(
            names,
            vec![LEASE_FILE_NAME.to_string()],
            "不该留下临时文件：{names:?}"
        );

        // 覆盖写走同一条路径
        let mut second = lease.clone();
        second.backend.pid = 33;
        write_lease(dir.path(), &second).unwrap();
        assert_eq!(read_lease(dir.path()).unwrap(), second);

        remove_lease(dir.path()).unwrap();
        assert!(!lease_path(dir.path()).exists());
        remove_lease(dir.path()).unwrap(); // 幂等
        assert_eq!(
            read_lease(dir.path()).unwrap_err().kind,
            LeaseErrorKind::Missing
        );
    }

    #[test]
    fn read_lease_reports_missing_invalid_schema_and_dir_mismatch() {
        let dir = TempDir::new("lease-errors");
        assert_eq!(
            read_lease(dir.path()).unwrap_err().kind,
            LeaseErrorKind::Missing
        );

        std::fs::write(lease_path(dir.path()), b"{ not json").unwrap();
        assert_eq!(
            read_lease(dir.path()).unwrap_err().kind,
            LeaseErrorKind::InvalidJson
        );

        let mut lease = lease_with(
            dir.path(),
            &(1, "1".to_string(), "a".to_string()),
            &(2, "2".to_string(), "b".to_string()),
        );
        lease.schema = 99;
        std::fs::write(lease_path(dir.path()), serde_json::to_vec(&lease).unwrap()).unwrap();
        assert_eq!(
            read_lease(dir.path()).unwrap_err().kind,
            LeaseErrorKind::SchemaMismatch
        );

        lease.schema = LEASE_SCHEMA;
        lease.install_dir = r"C:\Somewhere\Else".to_string();
        std::fs::write(lease_path(dir.path()), serde_json::to_vec(&lease).unwrap()).unwrap();
        assert_eq!(
            read_lease(dir.path()).unwrap_err().kind,
            LeaseErrorKind::InstallDirMismatch
        );
    }

    // ---------- 进程相关的夹具 ----------

    /// 测试用临时目录（%TEMP% 下，进程退出时清掉）。
    #[allow(dead_code)]
    struct TempDir(PathBuf);

    impl TempDir {
        #[allow(dead_code)]
        fn new(name: &str) -> TempDir {
            let mut dir = std::env::temp_dir();
            dir.push(format!("qio-ownership-{}-{}", name, std::process::id()));
            let _ = std::fs::remove_dir_all(&dir);
            std::fs::create_dir_all(&dir).expect("建测试临时目录失败");
            TempDir(dir)
        }
        #[allow(dead_code)]
        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    /// 被记录的目标 / 替身：真进程，退出时按 pid 收掉（绝不按名字）。
    #[allow(dead_code)]
    struct Proc(Option<std::process::Child>);

    impl Proc {
        #[allow(dead_code)]
        fn spawn(mut cmd: std::process::Command) -> Proc {
            use std::process::Stdio;
            cmd.stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            Proc(Some(cmd.spawn().expect("拉起测试子进程失败")))
        }
        #[allow(dead_code)]
        fn pid(&self) -> u32 {
            self.0.as_ref().expect("进程句柄已被取走").id()
        }
        /// 结束并**释放句柄**：句柄放掉之后进程对象才会消失（测试"已退出"用）。
        #[allow(dead_code)]
        fn wait_and_release(&mut self) {
            if let Some(mut child) = self.0.take() {
                let _ = kill_tree_by_pid(child.id());
                let _ = child.wait();
            }
        }
    }

    impl Drop for Proc {
        fn drop(&mut self) {
            if let Some(mut child) = self.0.take() {
                let _ = kill_tree_by_pid(child.id());
                let _ = child.wait();
            }
        }
    }

    #[allow(dead_code)]
    fn system32(name: &str) -> PathBuf {
        let root = std::env::var("SystemRoot").unwrap_or_else(|_| "C:\\Windows".to_string());
        PathBuf::from(root).join("System32").join(name)
    }

    /// 一个跑得够久的真进程（ping），当"被记录的目标"或"替身"。
    #[allow(dead_code)]
    fn ping(seconds: u32) -> std::process::Command {
        let mut cmd = std::process::Command::new(system32("ping.exe"));
        cmd.args(["-n", &seconds.to_string(), "127.0.0.1"]);
        cmd
    }

    #[allow(dead_code)]
    fn alive(pid: u32) -> bool {
        ProcessGuard::open(pid)
            .map(|guard| guard.is_alive())
            .unwrap_or(false)
    }

    #[allow(dead_code)]
    fn identity_of(pid: u32) -> (String, String) {
        let guard = ProcessGuard::open(pid).unwrap_or_else(|| panic!("打开 pid {pid} 失败"));
        (
            guard.created_filetime().expect("读创建时间失败"),
            guard.exe().expect("读映像路径失败"),
        )
    }

    #[allow(dead_code)]
    fn identity_tuple(pid: u32) -> (u32, String, String) {
        let (filetime, exe) = identity_of(pid);
        (pid, filetime, exe)
    }

    /// 把 ping.exe 复制成 qio-backend.exe 并跑起来：安装目录里的"替身后端"，
    /// 有真映像路径、真 pid —— 但（除非 lease 明确记录）它不属于任何安装实例。
    #[allow(dead_code)]
    fn decoy_backend(dir: &Path) -> (PathBuf, Proc) {
        let exe = dir.join("qio-backend.exe");
        std::fs::copy(system32("ping.exe"), &exe).expect("复制替身 exe 失败");
        let mut cmd = std::process::Command::new(&exe);
        cmd.args(["-n", "120", "127.0.0.1"]);
        (exe, Proc::spawn(cmd))
    }

    #[allow(dead_code)]
    fn powershell() -> PathBuf {
        let exe = system32(r"WindowsPowerShell\v1.0\powershell.exe");
        if exe.exists() {
            exe
        } else {
            PathBuf::from("powershell")
        }
    }

    /// 外壳替身：一个真的有顶层窗口、收到 WM_CLOSE 会退出的进程。
    /// 没有它就只能测"超时后按 pid 收"那条兜底路径，测不到优雅退出。
    #[allow(dead_code)]
    fn shell_stand_in() -> Proc {
        let script = "Add-Type -AssemblyName System.Windows.Forms; \
            $f = New-Object System.Windows.Forms.Form; \
            $f.Text = 'qio-test-shell'; \
            $f.ShowInTaskbar = $false; \
            [System.Windows.Forms.Application]::Run($f)";
        let mut cmd = std::process::Command::new(powershell());
        cmd.args(["-NoProfile", "-EncodedCommand", &encode_powershell(script)]);
        let proc = Proc::spawn(cmd);
        let deadline = Instant::now() + Duration::from_secs(25);
        while !has_top_level_window(proc.pid()) {
            assert!(
                Instant::now() < deadline,
                "外壳替身（pid {}）没有出现顶层窗口，WM_CLOSE 夹具不可用",
                proc.pid()
            );
            std::thread::sleep(Duration::from_millis(100));
        }
        proc
    }

    /// PowerShell 的 -EncodedCommand 要 UTF-16LE + base64 —— 自己算，免得依赖外部命令。
    #[allow(dead_code)]
    fn encode_powershell(script: &str) -> String {
        const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        let mut bytes: Vec<u8> = Vec::with_capacity(script.len() * 2);
        for unit in script.encode_utf16() {
            bytes.push((unit & 0xff) as u8);
            bytes.push((unit >> 8) as u8);
        }
        let mut out = String::new();
        for chunk in bytes.chunks(3) {
            let b0 = chunk[0] as u32;
            let b1 = *chunk.get(1).unwrap_or(&0) as u32;
            let b2 = *chunk.get(2).unwrap_or(&0) as u32;
            let triple = (b0 << 16) | (b1 << 8) | b2;
            out.push(TABLE[((triple >> 18) & 0x3f) as usize] as char);
            out.push(TABLE[((triple >> 12) & 0x3f) as usize] as char);
            out.push(if chunk.len() > 1 {
                TABLE[((triple >> 6) & 0x3f) as usize] as char
            } else {
                '='
            });
            out.push(if chunk.len() > 2 {
                TABLE[(triple & 0x3f) as usize] as char
            } else {
                '='
            });
        }
        out
    }

    #[allow(dead_code)]
    fn lease_with(
        install_dir: &Path,
        shell: &(u32, String, String),
        backend: &(u32, String, String),
    ) -> Lease {
        Lease {
            schema: LEASE_SCHEMA,
            install_dir: install_dir.to_string_lossy().to_string(),
            shell: LeaseProcess {
                pid: shell.0,
                created_filetime: shell.1.clone(),
                exe: shell.2.clone(),
            },
            backend: LeaseProcess {
                pid: backend.0,
                created_filetime: backend.1.clone(),
                exe: backend.2.clone(),
            },
            started_at: iso8601_utc(SystemTime::now()),
        }
    }

    #[allow(dead_code)]
    fn close_req(dir: &Path, timeout_ms: u64) -> CloseRequest {
        CloseRequest {
            install_dir: dir.to_path_buf(),
            timeout: Duration::from_millis(timeout_ms),
            allow_main_exe: false,
        }
    }

    #[allow(dead_code)]
    fn close_req_allow(dir: &Path, timeout_ms: u64) -> CloseRequest {
        CloseRequest {
            allow_main_exe: true,
            ..close_req(dir, timeout_ms)
        }
    }

    // ---------- 进程身份 ----------

    #[test]
    fn process_created_filetime_is_stable_and_identifies_the_process() {
        let first = Proc::spawn(ping(60));
        let second = Proc::spawn(ping(60));
        let fa = process_created_filetime(first.pid()).expect("读不到创建时间");
        assert!(
            fa.len() >= 17 && fa.chars().all(|ch| ch.is_ascii_digit()),
            "FILETIME 应当是十进制字符串：{fa}"
        );
        assert_eq!(
            Some(fa.clone()),
            process_created_filetime(first.pid()),
            "同一个进程的创建时间必须稳定"
        );
        let fb = process_created_filetime(second.pid()).expect("读不到第二个进程的创建时间");
        assert_ne!(fa, fb, "两个进程的创建时间不该相同（100ns 精度）");
    }

    #[test]
    fn process_image_path_reports_the_real_image_of_a_decoy_exe() {
        let dir = TempDir::new("image-path");
        let (exe, decoy) = decoy_backend(dir.path());
        let image = process_image_path(decoy.pid()).expect("读不到映像路径");
        assert!(
            same_path(&image, &exe.to_string_lossy()),
            "映像路径必须是替身 exe 自己：{image}"
        );
    }

    #[test]
    fn process_guard_matches_needs_creation_time_and_image_path() {
        let proc = Proc::spawn(ping(60));
        let (filetime, exe) = identity_of(proc.pid());
        let guard = ProcessGuard::open(proc.pid()).expect("打开失败");
        assert!(guard.matches(&filetime, &exe), "三要素一致时必须匹配");
        assert!(!guard.matches("1", &exe), "创建时间对不上不能算匹配");
        assert!(
            !guard.matches(&filetime, &format!("{exe}.not-the-same")),
            "映像路径对不上不能算匹配"
        );
        assert!(
            guard.matches(&filetime, &exe.to_ascii_uppercase()),
            "Windows 路径大小写不敏感"
        );
    }

    #[test]
    fn process_guard_terminate_ends_the_process_by_handle() {
        // taskkill 看不到某个 pid 时（受限环境）的兜底：用已核验身份的句柄结束它。
        let proc = Proc::spawn(ping(120));
        let (filetime, exe) = identity_of(proc.pid());
        let guard = ProcessGuard::open(proc.pid()).expect("打开失败");
        assert!(guard.matches(&filetime, &exe), "三要素必须一致");
        assert!(guard.is_alive(), "夹具应当是活着的");
        let (ok, note) = guard.terminate();
        assert!(ok, "按句柄结束失败：{note}");
        let deadline = Instant::now() + Duration::from_secs(10);
        while guard.is_alive() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(50));
        }
        assert!(!guard.is_alive(), "TerminateProcess 之后进程应当已结束");
    }

    #[test]
    fn post_close_to_pid_closes_a_real_window() {
        let shell = shell_stand_in();
        let pid = shell.pid();
        assert!(has_top_level_window(pid), "夹具应该已经有窗口");
        assert!(post_close_to_pid(pid), "WM_CLOSE 投递失败");
        let deadline = Instant::now() + Duration::from_secs(20);
        while alive(pid) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(100));
        }
        assert!(!alive(pid), "收到 WM_CLOSE 的窗口进程应该自己退出");
    }

    // ---------- 判定：拒绝（宁可不杀） ----------

    #[test]
    fn close_installation_refuses_without_a_lease_and_leaves_the_decoy_alone() {
        let dir = TempDir::new("no-lease");
        let (_exe, decoy) = decoy_backend(dir.path());
        let report = close_installation(&close_req(dir.path(), 500));
        assert_eq!(report.exit_code, 3, "{report:?}");
        assert_eq!(report.reason, "lease_missing", "{report:?}");
        assert!(
            !report.posted_close && !report.killed_backend && !report.killed_shell,
            "{report:?}"
        );
        assert!(alive(decoy.pid()), "没有 lease 时替身后端不能被结束");
    }

    #[test]
    fn close_installation_refuses_when_pid_was_reused_by_an_unrelated_process() {
        let dir = TempDir::new("pid-reuse");
        let (_exe, decoy) = decoy_backend(dir.path());
        let unrelated = Proc::spawn(ping(120));
        let (filetime, real_exe) = identity_of(unrelated.pid());
        // 外壳条目故意指向这个无关进程且三要素都真 —— 如果拒绝没发生在动手之前，
        // 这条测试就会看到它被 WM_CLOSE / taskkill 掉。
        let shell = (unrelated.pid(), filetime.clone(), real_exe.clone());

        // 伪造 1：pid + 创建时间是真的，但映像路径写成安装目录里的 qio-backend.exe
        let forged_exe = dir.path().join("qio-backend.exe").to_string_lossy().to_string();
        let backend = (unrelated.pid(), filetime.clone(), forged_exe);
        write_lease(dir.path(), &lease_with(dir.path(), &shell, &backend)).unwrap();
        let report = close_installation(&close_req(dir.path(), 300));
        assert_eq!(report.exit_code, 3, "{report:?}");
        assert_eq!(report.reason, "backend_identity_mismatch", "{report:?}");
        assert!(!report.posted_close, "拒绝时不许给外壳发 WM_CLOSE");
        assert!(alive(unrelated.pid()), "无关进程不能被结束");
        assert!(alive(decoy.pid()), "替身后端不能被结束");

        // 伪造 2：映像路径是真的，但创建时间是编的（= PID 复用后的新进程）
        let backend = (unrelated.pid(), "1".to_string(), system32("ping.exe").to_string_lossy().to_string());
        write_lease(dir.path(), &lease_with(dir.path(), &shell, &backend)).unwrap();
        let report = close_installation(&close_req(dir.path(), 300));
        assert_eq!(report.exit_code, 3, "{report:?}");
        assert_eq!(report.reason, "backend_identity_mismatch", "{report:?}");
        assert!(alive(unrelated.pid()), "无关进程不能被结束");
        assert!(alive(decoy.pid()), "替身后端不能被结束");
    }

    #[test]
    fn close_installation_refuses_when_the_recorded_backend_already_exited() {
        let dir = TempDir::new("backend-exited");
        let mut backend = Proc::spawn(ping(60));
        let backend_identity = identity_tuple(backend.pid());
        backend.wait_and_release();
        // 外壳条目指向一个真实存活的无关进程：拒绝必须发生在碰它之前
        let shell_proc = Proc::spawn(ping(120));
        let shell = identity_tuple(shell_proc.pid());
        write_lease(
            dir.path(),
            &lease_with(dir.path(), &shell, &backend_identity),
        )
        .unwrap();

        let report = close_installation(&close_req(dir.path(), 300));
        assert_eq!(report.exit_code, 3, "{report:?}");
        assert_eq!(report.reason, "backend_exited", "{report:?}");
        assert!(alive(shell_proc.pid()), "拒绝时不许碰外壳");
    }

    #[test]
    fn close_installation_refuses_when_install_dir_differs() {
        let dir = TempDir::new("dir-mismatch");
        let other = TempDir::new("dir-mismatch-other");
        let (_exe, decoy) = decoy_backend(dir.path());
        let shell_proc = Proc::spawn(ping(120));
        let shell = identity_tuple(shell_proc.pid());
        let backend = identity_tuple(decoy.pid());
        // lease 写在 dir 里，但里面记的是 other —— 卸载别的实例时不许动 dir 里的东西
        write_lease(dir.path(), &lease_with(other.path(), &shell, &backend)).unwrap();

        let report = close_installation(&close_req(dir.path(), 300));
        assert_eq!(report.exit_code, 3, "{report:?}");
        assert_eq!(report.reason, "install_dir_mismatch", "{report:?}");
        assert!(alive(decoy.pid()) && alive(shell_proc.pid()));
    }

    // ---------- 判定：收本实例（只有本实例的进程会死） ----------

    #[test]
    fn close_installation_closes_the_recorded_installation_gracefully() {
        let dir = TempDir::new("close-ok");
        let (_exe, backend) = decoy_backend(dir.path());
        let shell = shell_stand_in();
        let shell_identity = identity_tuple(shell.pid());
        let backend_identity = identity_tuple(backend.pid());
        write_lease(
            dir.path(),
            &lease_with(dir.path(), &shell_identity, &backend_identity),
        )
        .unwrap();

        let report = close_installation(&close_req(dir.path(), 8000));
        assert_eq!(report.exit_code, 0, "{report:?}");
        assert!(report.posted_close, "应该给外壳替身发了 WM_CLOSE：{report:?}");
        assert!(
            report.killed_backend,
            "外壳替身没有 job，替身后端只能按 pid 收：{report:?}"
        );
        assert!(!alive(backend.pid()), "后端替身必须已经结束");
        assert!(!alive(shell.pid()), "外壳替身必须已经退出");
        assert!(!lease_path(dir.path()).exists(), "成功后必须删掉 lease");
        assert_eq!(report.reason, "killed_by_pid", "{report:?}");
        assert!(report.lease_removed, "{report:?}");
    }

    #[test]
    fn close_installation_falls_back_to_pid_kill_when_the_shell_has_no_window() {
        let dir = TempDir::new("kill-fallback");
        let (_exe, backend) = decoy_backend(dir.path());
        let shell = Proc::spawn(ping(120));
        let shell_identity = identity_tuple(shell.pid());
        let backend_identity = identity_tuple(backend.pid());
        write_lease(
            dir.path(),
            &lease_with(dir.path(), &shell_identity, &backend_identity),
        )
        .unwrap();

        let report = close_installation(&close_req(dir.path(), 300));
        assert_eq!(report.exit_code, 0, "{report:?}");
        assert!(
            !report.posted_close,
            "没有窗口就不该假装发过 WM_CLOSE：{report:?}"
        );
        assert!(
            report.killed_backend && report.killed_shell,
            "超时后必须按 pid 收掉两侧：{report:?}"
        );
        assert!(!alive(backend.pid()) && !alive(shell.pid()));
        assert!(!lease_path(dir.path()).exists());
    }

    // ---------- --check-only：只读判定 ----------

    #[test]
    fn check_only_reports_a_running_shell_and_touches_nothing() {
        let dir = TempDir::new("check-only-ok");
        let (_exe, backend) = decoy_backend(dir.path());
        let shell = Proc::spawn(ping(120));
        let shell_identity = identity_tuple(shell.pid());
        let backend_identity = identity_tuple(backend.pid());
        write_lease(
            dir.path(),
            &lease_with(dir.path(), &shell_identity, &backend_identity),
        )
        .unwrap();

        let report = check_installation(&close_req(dir.path(), 500));
        assert_eq!(report.exit_code, 0, "壳在跑就应当返回 0（可收）：{report:?}");
        assert_eq!(report.action, "would_close", "{report:?}");
        assert_eq!(report.reason, "shell_running", "{report:?}");
        assert!(
            report.shell_alive && report.shell_identity_ok,
            "{report:?}"
        );
        assert!(report.backend_alive && report.backend_identity_ok, "{report:?}");
        assert!(
            !report.posted_close && !report.killed_backend && !report.killed_shell,
            "--check-only 绝不许动进程：{report:?}"
        );
        assert!(
            alive(shell.pid()) && alive(backend.pid()),
            "--check-only 跑完两个真进程都必须还活着"
        );
        assert!(lease_path(dir.path()).exists(), "--check-only 不许删 lease");
    }

    #[test]
    fn check_only_refuses_when_the_shell_is_not_running() {
        let dir = TempDir::new("check-only-dead");
        let (_exe, backend) = decoy_backend(dir.path());
        let mut shell = Proc::spawn(ping(60));
        let shell_identity = identity_tuple(shell.pid());
        shell.wait_and_release();
        let backend_identity = identity_tuple(backend.pid());
        write_lease(
            dir.path(),
            &lease_with(dir.path(), &shell_identity, &backend_identity),
        )
        .unwrap();

        let report = check_installation(&close_req(dir.path(), 300));
        assert_eq!(report.exit_code, 3, "{report:?}");
        assert_eq!(report.reason, "shell_exited", "{report:?}");
        assert!(!report.posted_close && !report.killed_backend && !report.killed_shell);
        assert!(alive(backend.pid()), "只读判定不许碰后端");
    }

    #[test]
    fn check_only_refuses_when_the_shell_pid_was_reused() {
        let dir = TempDir::new("check-only-reuse");
        let (_exe, backend) = decoy_backend(dir.path());
        let unrelated = Proc::spawn(ping(120));
        let (filetime, real_exe) = identity_of(unrelated.pid());
        // 外壳记录：pid 是真的、创建时间是编的 —— 典型的 PID 复用
        let shell = (unrelated.pid(), "1".to_string(), real_exe);
        let backend_identity = identity_tuple(backend.pid());
        let _ = filetime;
        write_lease(
            dir.path(),
            &lease_with(dir.path(), &shell, &backend_identity),
        )
        .unwrap();

        let report = check_installation(&close_req(dir.path(), 300));
        assert_eq!(report.exit_code, 3, "{report:?}");
        assert_eq!(report.reason, "shell_identity_mismatch", "{report:?}");
        assert!(alive(unrelated.pid()), "只读判定不许碰任何进程");
        assert!(alive(backend.pid()));
    }

    // ---------- --allow-main-exe：只凭壳的三要素收壳 ----------

    #[test]
    fn strict_mode_still_refuses_when_the_backend_record_is_stale() {
        let dir = TempDir::new("allow-off");
        let (_exe, decoy) = decoy_backend(dir.path());
        let unrelated = Proc::spawn(ping(120));
        let shell_proc = Proc::spawn(ping(120));
        let shell = identity_tuple(shell_proc.pid());
        let backend = (
            unrelated.pid(),
            "1".to_string(),
            system32("ping.exe").to_string_lossy().to_string(),
        );
        write_lease(dir.path(), &lease_with(dir.path(), &shell, &backend)).unwrap();

        let report = close_installation(&close_req(dir.path(), 300));
        assert_eq!(report.exit_code, 3, "默认必须严格：{report:?}");
        assert_eq!(report.reason, "backend_identity_mismatch", "{report:?}");
        assert!(alive(shell_proc.pid()), "严格模式下不许收壳");
        assert!(alive(unrelated.pid()) && alive(decoy.pid()));
    }

    #[test]
    fn allow_main_exe_refuses_when_the_shell_identity_is_wrong() {
        // --allow-main-exe 把门槛换成了壳：壳的三要素对不上 → exit 3，一个进程都不动
        let dir = TempDir::new("allow-on-bad-shell");
        let (_exe, decoy) = decoy_backend(dir.path());
        let unrelated = Proc::spawn(ping(120));
        let backend_proc = Proc::spawn(ping(120));
        let (filetime, exe) = identity_of(unrelated.pid());
        let shell = (unrelated.pid(), "1".to_string(), exe); // 创建时间是编的
        let backend = (backend_proc.pid(), filetime, system32("ping.exe").to_string_lossy().to_string());
        write_lease(dir.path(), &lease_with(dir.path(), &shell, &backend)).unwrap();

        let report = close_installation(&close_req_allow(dir.path(), 300));
        assert_eq!(report.exit_code, 3, "{report:?}");
        assert_eq!(report.reason, "shell_identity_mismatch", "{report:?}");
        assert!(
            !report.posted_close && !report.killed_backend && !report.killed_shell,
            "{report:?}"
        );
        assert!(lease_path(dir.path()).exists(), "拒绝时必须保留 lease");
        assert!(alive(unrelated.pid()) && alive(backend_proc.pid()) && alive(decoy.pid()));
    }

    #[test]
    fn allow_main_exe_collects_the_shell_and_leaves_the_unverified_backend_alone() {
        let dir = TempDir::new("allow-on");
        let (_exe, decoy) = decoy_backend(dir.path());
        let unrelated = Proc::spawn(ping(120));
        let shell_proc = Proc::spawn(ping(120));
        let shell = identity_tuple(shell_proc.pid());
        let backend = (
            unrelated.pid(),
            "1".to_string(),
            system32("ping.exe").to_string_lossy().to_string(),
        );
        write_lease(dir.path(), &lease_with(dir.path(), &shell, &backend)).unwrap();

        let report = close_installation(&close_req_allow(dir.path(), 300));
        assert_eq!(report.exit_code, 0, "{report:?}");
        assert!(report.killed_shell, "应当按 pid 收掉本实例的壳：{report:?}");
        assert!(
            !report.killed_backend && !report.backend_identity_ok,
            "backend 记录对不上就不许碰它：{report:?}"
        );
        assert!(!alive(shell_proc.pid()), "本实例的壳必须被收掉");
        assert!(
            alive(unrelated.pid()),
            "backend 记录对不上的进程即使活着也不许被结束"
        );
        assert!(alive(decoy.pid()), "安装目录里的替身后端不许被碰");
        assert!(!lease_path(dir.path()).exists());
    }

    // 帮助程序（真 exe / CLI 退出码 / 一行 JSON）的测试在
    // src/tests_uninstall_helper_cli.rs：那里是**集成测试**，cargo 会真的构建 bin 产物
    // 并给出 CARGO_BIN_EXE_qio-uninstall-helper（lib 单元测试里拿不到它）。

    /// 实测事故（2026-09-24）：reg.exe 报 0xC0000142 并弹出"必须先点掉"的模态框，
    /// 子进程因此一直不退出，Command::output() 把外壳的启动流程钉住 1 分 42 秒。
    /// 所以跑系统命令必须有硬超时：超时就结束子进程、按"拿不到"处理。
    #[cfg(windows)]
    #[test]
    fn hanging_command_is_killed_after_the_timeout() {
        let mut cmd = std::process::Command::new("cmd");
        cmd.args(["/c", "ping", "-n", "6", "127.0.0.1"]);
        let started = Instant::now();
        let out = run_command_with_timeout(&mut cmd, Duration::from_millis(800));
        let elapsed = started.elapsed();
        assert!(out.is_none(), "超时的命令不能返回输出");
        assert!(
            elapsed < Duration::from_secs(4),
            "超时后必须立刻返回（实测 {elapsed:?}）"
        );
    }

    #[cfg(windows)]
    #[test]
    fn fast_command_still_returns_its_output() {
        let mut cmd = std::process::Command::new("cmd");
        cmd.args(["/c", "echo", "hello-qio"]);
        let out = run_command_with_timeout(&mut cmd, Duration::from_secs(10)).expect("应拿到输出");
        assert!(String::from_utf8_lossy(&out.stdout).contains("hello-qio"));
    }
}
