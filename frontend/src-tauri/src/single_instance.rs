//! 桌面全机单实例（R9 #46 Task 1）：全机互斥 + 同会话激活 + 更新重启交接。
//!
//! 为什么必须是**内核命名对象**而不是文件/端口/进程名扫描：
//! * 文件锁在跨账号/跨提权时会被 ACL 挡住，而且崩溃后会留下需要清理的残留；
//! * 端口/进程名扫描既不可靠（端口可能被别人占用、进程名可能被误伤），也不能原子地
//!   表达「只有一个实例」；
//! * 命名内核对象（mutex）由内核在最后一个句柄关闭（进程退出/崩溃）时自动释放 ——
//!   不需要任何退出清理逻辑，强杀之后也能立刻重开。
//!
//! 三个对象，职责分开（名字只与**产品 + 单实例协议版本**有关，与安装路径、Windows 账号、
//! 登录会话、是否提权、QIO 版本都无关）。
//!
//! 注意：`QIO_INSTANCE_PROBE_SUFFIX` / `QIO_INSTANCE_PROBE` 这两个**测试**环境变量
//! 只在测试构建（`cfg!(debug_assertions)`，即 cargo test 的 CARGO_BIN_EXE_qio）生效；
//! release/安装版即使继承了它们，也始终使用固定产品锁名、按正式实例启动
//! （判定集中在 `test_build_env_allowed`，语义由 `resolve_suffix` / `probe_requested_when`
//! 的显式 allowed 参数单测覆盖）。
//!
//! 1. 'Global\QIO.SingleInstance.v1'（互斥，全机唯一）
//!    取锁 = 获得「本机唯一活动实例」。创建时带**显式 NULL DACL**，所以跨账号/跨提权
//!    都能打开、都能参与判定；'Global\' 命名空间保证跨登录会话也互斥。
//! 2. 'Local\QIO.SingleInstance.Activate.v1'（手动手重置事件，会话级）
//!    主实例创建并监听；同会话后启动的进程把它置位后退出 → 主实例恢复/聚焦主窗口。
//!    用会话级命名是有意的：跨会话没有办法安全地唤醒另一个会话的窗口。
//! 3. 'Local\QIO.SingleInstance.Handoff.v1'（手动手重置事件，会话级）
//!    更新重启交接：前端在请求进程重启之前置位；新进程发现锁被占但有该事件时，
//!    等待旧进程退出释放锁（最多 30 秒）再成为主实例 —— 而不是把自己当成重复启动。
//!
//! 所有创建都带 NULL DACL（Everyone 完全访问）：这是**有意的**，否则「管理员先启动、
//! 普通用户再启动」时后者会因访问被拒而弹出「已运行」或（更糟）静默启动第二套应用。
//!
//! 主实例在取锁后**立刻**创建激活事件（手动手重置），保证「首个实例还在启动时」的
//! 激活信号不丢；窗口就绪后监听线程才启动，事件保持置位直到被处理。

use std::time::{Duration, Instant};

/// 全机互斥对象名：只与产品与单实例协议版本有关（v1）。
pub const LOCK_NAME: &str = "Global\\QIO.SingleInstance.v1";
/// 同会话激活事件名（手动手重置）。
pub const ACTIVATE_EVENT_NAME: &str = "Local\\QIO.SingleInstance.Activate.v1";
/// 更新重启交接事件名（手动手重置）。
pub const HANDOFF_EVENT_NAME: &str = "Local\\QIO.SingleInstance.Handoff.v1";
/// 测试隔离用的名字后缀环境变量（空/未设置 = 产品级名字）。
///
/// 跨进程契约测试会并发启动多个真实 qio.exe；用独立后缀让每个用例拥有自己的锁名，
/// 否则它们会互相抢锁，测试本身变成竞态源。
///
/// **只在测试构建生效**：正式构建（release/安装版）即使继承了这个变量也恒用产品锁名，
/// 判定见 `test_build_env_allowed`。
pub const SUFFIX_ENV: &str = "QIO_INSTANCE_PROBE_SUFFIX";
/// 跨进程契约测试探针的环境变量名。
///
/// 与 [`SUFFIX_ENV`] 一样**只在测试构建生效**：正式构建恒不认（`probe_requested`
/// 在 release 下恒 false），避免 release exe 因继承测试 env 而走「打印 JSON 后退出」。
pub const PROBE_ENV: &str = "QIO_INSTANCE_PROBE";
/// 后启动进程打开激活事件的重试预算：主实例取锁后立刻创建事件，但两个进程可能几乎
/// 同时到达，给一个短暂重试窗口（期间不能等太久，否则重复启动的用户要干等）。
pub const ACTIVATE_RETRY: Duration = Duration::from_secs(2);
/// 更新重启交接：新进程等待旧进程退出释放锁的上限。
pub const RELAUNCH_WAIT: Duration = Duration::from_secs(30);

/// 取锁结果。
pub enum Acquire {
    /// 本进程是主实例：持有锁（Guard 必须活到进程结束）。
    Primary(Guard),
    /// 已有实例在运行；same_session = 同会话（已置位激活事件）还是其它会话/账号。
    Duplicate { same_session: bool },
}

/// 名字后缀：只保留对象名安全字符，空/全非法 → 空（回退产品级名字）。
fn sanitize_suffix(raw: &str) -> String {
    let cleaned: String = raw
        .trim()
        .chars()
        .filter(|ch| ch.is_ascii_alphanumeric() || *ch == '-' || *ch == '_')
        .collect();
    if cleaned.is_empty() {
        String::new()
    } else {
        format!(".{cleaned}")
    }
}

/// 测试构建是否允许「测试环境变量」生效（后缀隔离 + 跨进程探针）。
///
/// 判定依据是 `debug_assertions`：`cargo test` 通过 `CARGO_BIN_EXE_qio` 拉起的是
/// debug 构建（debug_assertions=true）；`tauri build` / NSIS 安装版是 release 构建
/// （debug_assertions=false）。这是编译期常量，用 `cfg!` 而不是 `#[cfg]`，让两条分支
/// 都参与类型检查 —— 不会在 release 下把符号 cfg 掉、留下「环境变量被读却没人用」的隐患。
fn test_build_env_allowed() -> bool {
    cfg!(debug_assertions)
}

/// 名字后缀解析（显式 allowed 参数 = 可单测的正式构建语义）。
///
/// * allowed=false（正式构建）→ **完全不读环境变量**，恒返回空串 → 三个对象名固定为
///   产品名，与安装路径/账号/会话/提权/版本无关；
/// * allowed=true（测试构建）→ 按既有 sanitize 规则读 `QIO_INSTANCE_PROBE_SUFFIX`。
pub fn resolve_suffix(allowed: bool) -> String {
    if !allowed {
        return String::new();
    }
    std::env::var(SUFFIX_ENV)
        .map(|raw| sanitize_suffix(&raw))
        .unwrap_or_default()
}

fn suffix() -> String {
    // 唯一一处 cfg! 接入：正式构建下恒为空后缀（产品锁名）。
    resolve_suffix(test_build_env_allowed())
}

fn name_with(base: &str, suffix: &str) -> String {
    format!("{base}{suffix}")
}

pub fn lock_name() -> String {
    name_with(LOCK_NAME, &suffix())
}

pub fn activate_event_name() -> String {
    name_with(ACTIVATE_EVENT_NAME, &suffix())
}

pub fn handoff_event_name() -> String {
    name_with(HANDOFF_EVENT_NAME, &suffix())
}

/// 跨进程契约测试探针是否被请求。
///
/// 探针只走「锁 / 激活 / 交接」逻辑，打印一行 JSON 后退出 —— 不初始化日志、不碰数据目录、
/// 不建窗口、不拉后端。
///
/// **只在测试构建生效**：正式构建（release/安装版）恒为 false —— 即使进程环境里继承了
/// `QIO_INSTANCE_PROBE`，也按正式实例正常启动（写 lease/建窗口/拉后端），绝不当探针退出。
pub fn probe_requested() -> bool {
    // 与 resolve_suffix 共用同一个 cfg! 依据（test_build_env_allowed）。
    probe_requested_when(test_build_env_allowed())
}

/// 显式 allowed 的同构实现：正式构建的 false 语义由它单测覆盖
/// （cfg! 分支本身无法在 debug 测试构建里直接运行）。
fn probe_requested_when(allowed: bool) -> bool {
    if !allowed {
        return false;
    }
    std::env::var(PROBE_ENV)
        .map(|raw| !raw.trim().is_empty())
        .unwrap_or(false)
}

#[cfg(windows)]
mod win {
    //! Windows 实现：命名互斥 + 事件 + 真进程探针。

    use super::*;
    use std::ffi::c_void;
    use std::sync::OnceLock;
    use windows_sys::Win32::Foundation::{
        CloseHandle, GetLastError, ERROR_ACCESS_DENIED, ERROR_ALREADY_EXISTS, HANDLE,
        WAIT_ABANDONED, WAIT_OBJECT_0, WAIT_TIMEOUT,
    };
    use windows_sys::Win32::Security::{
        InitializeSecurityDescriptor, SetSecurityDescriptorDacl, PSECURITY_DESCRIPTOR,
        SECURITY_ATTRIBUTES, SECURITY_DESCRIPTOR,
    };
    use windows_sys::Win32::System::Threading::{
        CreateEventW, CreateMutexW, OpenEventW, ReleaseMutex, ResetEvent, SetEvent,
        WaitForSingleObject, EVENT_MODIFY_STATE, INFINITE,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        MessageBoxW, MB_ICONINFORMATION, MB_OK, MB_SETFOREGROUND,
    };

    /// SECURITY_DESCRIPTOR_REVISION = 1（Win32 文档常量；只为它加 Win32_System_SystemServices
    /// feature 不值得，windows-sys 的 feature 只允许加 Win32_Security）。
    const SECURITY_DESCRIPTOR_REVISION: u32 = 1;
    /// SYNCHRONIZE = 0x00100000；windows-sys 0.61 只在 Storage_FileSystem feature 下导出同名
    /// 常量，这里按 Win32 文档值本地定义（不新增 feature）。
    const SYNCHRONIZE: u32 = 0x0010_0000;

    /// 主实例锁：持有到进程结束（崩溃由内核释放）。
    pub struct Guard {
        mutex: HANDLE,
        activation: HANDLE,
        handoff: HANDLE,
    }

    // 句柄只是内核对象的引用；主实例保证只在主线程创建、进程生命周期内不移动语义。
    unsafe impl Send for Guard {}
    unsafe impl Sync for Guard {}

    impl Guard {
        fn activation_handle(&self) -> Option<HANDLE> {
            if self.activation.is_null() {
                None
            } else {
                Some(self.activation)
            }
        }
    }

    impl Drop for Guard {
        fn drop(&mut self) {
            unsafe {
                if !self.mutex.is_null() {
                    // 正常路径下 static Guard 不会 drop；这里给单元测试/异常路径兜底。
                    ReleaseMutex(self.mutex);
                    CloseHandle(self.mutex);
                }
                if !self.activation.is_null() {
                    CloseHandle(self.activation);
                }
                if !self.handoff.is_null() {
                    CloseHandle(self.handoff);
                }
            }
        }
    }

    fn to_wide(text: &str) -> Vec<u16> {
        text.encode_utf16().chain(std::iter::once(0)).collect()
    }

    /// 用**显式 NULL DACL** 的 SECURITY_ATTRIBUTES 创建内核对象。
    ///
    /// NULL DACL = 允许所有人完全访问（跨账号 / 跨提权都能打开）。这是单实例判定能跨账号
    /// 生效的前提；安全上它只保护「一个互斥对象 + 一个事件」，不承载任何数据。
    fn with_null_dacl<R>(create: impl FnOnce(*const SECURITY_ATTRIBUTES) -> R) -> R {
        unsafe {
            let mut descriptor: SECURITY_DESCRIPTOR = std::mem::zeroed();
            let descriptor_ptr =
                &mut descriptor as *mut SECURITY_DESCRIPTOR as PSECURITY_DESCRIPTOR;
            if InitializeSecurityDescriptor(descriptor_ptr, SECURITY_DESCRIPTOR_REVISION) == 0 {
                log::warn!(
                    "[qio] 初始化安全描述符失败（{}）：单实例对象将退回默认 DACL，跨账号判定可能失效",
                    GetLastError()
                );
            }
            // bDaclPresent = TRUE + pDacl = NULL = NULL DACL（Everyone 完全访问）
            if SetSecurityDescriptorDacl(descriptor_ptr, 1, std::ptr::null(), 0) == 0 {
                log::warn!(
                    "[qio] 设置 NULL DACL 失败（{}）：跨账号单实例判定可能失效",
                    GetLastError()
                );
            }
            let attributes = SECURITY_ATTRIBUTES {
                nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
                lpSecurityDescriptor: descriptor_ptr as *mut c_void,
                bInheritHandle: 0,
            };
            create(&attributes)
        }
    }

    /// 尝试创建并取得全机互斥。
    ///
    /// Ok(Some(guard)) = 本进程成为主实例；Ok(None) = 已有实例（含 ERROR_ACCESS_DENIED）；
    /// Err = 对象建不出来（调用方按保守策略处理，绝不静默放行第二套应用）。
    fn create_lock() -> Result<Option<Guard>, String> {
        unsafe {
            let (mutex, last_error) = with_null_dacl(|attributes| {
                let name = to_wide(&lock_name());
                let handle = CreateMutexW(attributes, 0, name.as_ptr());
                // GetLastError 必须紧跟 CreateMutexW 读取（中间不能再调 Win32 API）
                (handle, GetLastError())
            });
            if mutex.is_null() {
                if last_error == ERROR_ACCESS_DENIED {
                    // ERROR_ACCESS_DENIED 也按「已有实例」处理：对象存在但我们打不开，
                    // 说明本机已经有一个实例在用这个名字（宁可提示已运行，也不启动第二套）。
                    log::warn!(
                        "[qio] 打开单实例互斥被拒（ERROR_ACCESS_DENIED）：按已有实例处理"
                    );
                    return Ok(None);
                }
                return Err(format!("CreateMutexW 失败：{last_error}"));
            }
            if last_error == ERROR_ALREADY_EXISTS {
                CloseHandle(mutex);
                return Ok(None);
            }
            // 创建成功不等于持有：用 WaitForSingleObject(0) 取得所有权（线程所有权），
            // 之后进程存活期间一直持有；进程结束（含崩溃）由内核释放。
            match WaitForSingleObject(mutex, 0) {
                WAIT_OBJECT_0 | WAIT_ABANDONED => {}
                WAIT_TIMEOUT => {
                    // 极端竞态：句柄刚创建就被别的线程/进程抢到所有权。
                    CloseHandle(mutex);
                    return Ok(None);
                }
                other => {
                    CloseHandle(mutex);
                    return Err(format!("WaitForSingleObject(锁) 失败：{other}"));
                }
            }

            // 取锁后**立刻**创建两个会话级事件（手动手重置）。
            // 激活事件先于窗口创建：首个实例还在启动时到达的激活信号不会丢（事件保持置位）。
            //
            // **不做 ResetEvent(activation)**：新建的事件对象初始就是未置位（内核对象在最后
            // 一个句柄关闭时销毁，不会跨进程残留）；而「创建后立刻 Reset」会与同会话第二
            // 进程的 SetEvent 竞态 —— CreateEventW 返回后、Reset 之前到达的激活会被吞掉，
            // 首个实例就绪后就再也不会唤出窗口。这是代码 review 明确指出的竞态，别加回来。
            let activation = with_null_dacl(|attributes| {
                let name = to_wide(&activate_event_name());
                CreateEventW(attributes, 1, 0, name.as_ptr())
            });
            if activation.is_null() {
                log::error!(
                    "[qio] 创建激活事件失败（{}）：同会话重复启动将无法唤出本窗口",
                    GetLastError()
                );
            }
            // handoff 事件只有在「旧进程正在退出、句柄还没全部关闭」的极短窗口里才可能被
            // CreateEventW 打开成**已置位**的旧对象。用 GetLastError 区分「本进程新建」：
            // 新建 = 初始未置位，不需要也不允许 Reset；打开到旧对象 = 清掉残留置位，否则
            // 之后任何一次普通重复启动都会先白等 30 秒「旧进程退出」。
            // 时序可证明：handoff 只由**当前主实例**的 qio_arm_relaunch 置位；此刻我们已经
            // 持有全机互斥（没有别的主实例），不存在另一个置位者与这里并发。
            let (handoff, handoff_last_error) = with_null_dacl(|attributes| {
                let name = to_wide(&handoff_event_name());
                let handle = CreateEventW(attributes, 1, 0, name.as_ptr());
                (handle, GetLastError())
            });
            if handoff.is_null() {
                log::warn!("[qio] 创建更新交接事件失败（{}）：更新重启将按重复启动处理", GetLastError());
            } else if handoff_last_error == ERROR_ALREADY_EXISTS {
                log::info!("[qio] 打开到旧的更新交接事件：清掉上一次生命周期残留的置位");
                ResetEvent(handoff);
            }
            Ok(Some(Guard {
                mutex,
                activation,
                handoff,
            }))
        }
    }

    /// 锁现在是否空闲（不取得所有权；只用于更新交接前的轮询探测）。
    fn lock_is_free() -> bool {
        unsafe {
            let (mutex, last_error) = with_null_dacl(|attributes| {
                let name = to_wide(&lock_name());
                let handle = CreateMutexW(attributes, 0, name.as_ptr());
                (handle, GetLastError())
            });
            if mutex.is_null() {
                // 不存在之外的原因（权限等）一律按「仍被占用」处理，绝不提前接任。
                return false;
            }
            let created_new = last_error != ERROR_ALREADY_EXISTS;
            CloseHandle(mutex);
            created_new
        }
    }

    /// 交接事件是否处于置位状态（只有同会话的更新重启会置位它）。
    fn handoff_armed() -> bool {
        unsafe {
            let name = to_wide(&handoff_event_name());
            let handle = OpenEventW(EVENT_MODIFY_STATE | SYNCHRONIZE, 0, name.as_ptr());
            if handle.is_null() {
                return false;
            }
            let armed = WaitForSingleObject(handle, 0) == WAIT_OBJECT_0;
            CloseHandle(handle);
            armed
        }
    }

    /// 同会话把已有实例的激活事件置位（等事件出现最多 ACTIVATE_RETRY）。
    /// 返回 false = 打不开（不同会话 / 主实例还没创建事件 / 创建失败）。
    pub fn signal_activation() -> bool {
        let deadline = Instant::now() + ACTIVATE_RETRY;
        loop {
            unsafe {
                let name = to_wide(&activate_event_name());
                let handle = OpenEventW(EVENT_MODIFY_STATE | SYNCHRONIZE, 0, name.as_ptr());
                if !handle.is_null() {
                    let signalled = SetEvent(handle) != 0;
                    CloseHandle(handle);
                    if signalled {
                        return true;
                    }
                    log::warn!("[qio] 置位激活事件失败：{}", GetLastError());
                    return false;
                }
            }
            if Instant::now() >= deadline {
                return false;
            }
            std::thread::sleep(Duration::from_millis(80));
        }
    }

    /// 取得单实例（主入口唯一入口；必须在任何共享写操作之前调用）。
    pub fn acquire() -> Acquire {
        match create_lock() {
            Ok(Some(guard)) => {
                log::info!("[qio] 已取得全机单实例锁：{}", lock_name());
                return Acquire::Primary(guard);
            }
            Ok(None) => {}
            Err(err) => {
                // 锁对象建不出来（极端环境故障）：按「已有实例」保守处理 —— 宁可让用户
                // 看到一次提示，也绝不静默启动第二套应用去争同一份数据目录/端口。
                log::error!("[qio] 单实例锁不可用（{err}）：按重复启动处理，本次不启动第二套应用");
                return Acquire::Duplicate { same_session: false };
            }
        }

        // 已有实例：先判断是不是「更新重启」交接。
        if handoff_armed() {
            log::info!(
                "[qio] 检测到更新重启交接：等待旧进程退出并释放单实例锁（最多 {}s）",
                RELAUNCH_WAIT.as_secs()
            );
            if try_wait_for_relaunch_handoff(RELAUNCH_WAIT) {
                match create_lock() {
                    Ok(Some(guard)) => {
                        log::info!("[qio] 旧进程已退出：本进程接任主实例");
                        return Acquire::Primary(guard);
                    }
                    Ok(None) => {
                        log::warn!(
                            "[qio] 旧进程释放锁后又被其它进程抢先：按重复启动处理"
                        );
                    }
                    Err(err) => {
                        log::error!("[qio] 接任主实例失败（{err}）：按重复启动处理");
                    }
                }
            } else {
                log::warn!(
                    "[qio] 等待旧进程退出超时（{}s）：按重复启动处理",
                    RELAUNCH_WAIT.as_secs()
                );
            }
        }

        // 同会话：置位激活事件让已有窗口出来；打不开 = 不同会话/账号（或主实例事件还没建好）。
        let same_session = signal_activation();
        Acquire::Duplicate { same_session }
    }

    /// 轮询等待锁释放（不取得所有权）：旧进程退出后返回 true，超时返回 false。
    pub fn try_wait_for_relaunch_handoff(max_wait: Duration) -> bool {
        let deadline = Instant::now() + max_wait;
        loop {
            if lock_is_free() {
                return true;
            }
            if Instant::now() >= deadline {
                return false;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
    }

    /// 更新重启交接：置位同会话交接事件（前端在 plugin-process 的 relaunch() 之前调用）。
    ///
    /// 主实例启动时已经创建了这个事件；这里正常情况下是「打开 + 置位」。事件不存在时
    /// （例如被外部清理）主动创建 —— 交接失败不该让更新重启卡住（新实例会退回重复启动兜底）。
    pub fn arm_relaunch_handoff() -> bool {
        unsafe {
            let name = to_wide(&handoff_event_name());
            let mut handle = OpenEventW(EVENT_MODIFY_STATE | SYNCHRONIZE, 0, name.as_ptr());
            if handle.is_null() {
                handle = with_null_dacl(|attributes| CreateEventW(attributes, 1, 0, name.as_ptr()));
            }
            if handle.is_null() {
                log::warn!(
                    "[qio] 无法创建/打开更新交接事件（{}）：新实例会按重复启动处理",
                    GetLastError()
                );
                return false;
            }
            let armed = SetEvent(handle) != 0;
            CloseHandle(handle);
            armed
        }
    }

    static PRIMARY_GUARD: OnceLock<Guard> = OnceLock::new();

    /// 把主实例锁保存到进程级静态：持有到进程结束（正常退出/崩溃都由内核释放）。
    pub fn install_guard(guard: Guard) {
        if PRIMARY_GUARD.set(guard).is_err() {
            log::error!("[qio] 主实例锁被重复安装（内部错误）：忽略第二次，锁仍由第一次持有");
        }
    }

    /// 窗口就绪后启动激活监听线程：同会话重复启动 → ResetEvent → 主线程恢复/聚焦窗口。
    ///
    /// 复用 main.rs 的 show_main_window（show + unminimize + focus + 窗口层状态同步），
    /// 覆盖正常显示 / 最小化 / 托盘隐藏三种状态；只操作既有窗口，绝不创建第二个窗口。
    pub fn spawn_activation_listener(app: tauri::AppHandle) {
        let Some(handle) = PRIMARY_GUARD.get().and_then(Guard::activation_handle) else {
            log::warn!("[qio] 主实例锁未安装或激活事件不可用：同会话重复启动无法唤出窗口");
            return;
        };
        // HANDLE 是裸指针（!Send），但内核同步对象跨线程使用是合法的；用 usize 传递句柄值
        // 避免把裸指针搬进线程（也让闭包捕获的是普通整数，而不是绕过 Send 检查的字段投影）。
        let handle_bits = handle as usize;
        let builder = std::thread::Builder::new().name("qio-activation-listener".to_string());
        let spawned = builder.spawn(move || {
            let handle = handle_bits as HANDLE;
            log::info!("[qio] 单实例激活监听已启动：同会话重复启动会恢复并聚焦主窗口");
            loop {
                let state = unsafe { WaitForSingleObject(handle, INFINITE) };
                if state != WAIT_OBJECT_0 {
                    log::warn!("[qio] 单实例激活等待失败（{state}）：监听线程退出");
                    return;
                }
                unsafe { ResetEvent(handle) };
                let handle_for_main = app.clone();
                let dispatched = app.run_on_main_thread(move || {
                    crate::show_main_window(&handle_for_main);
                    log::info!(
                        "[qio] 收到同会话重复启动请求：已恢复并聚焦主窗口（没有启动第二套应用）"
                    );
                });
                if let Err(err) = dispatched {
                    log::warn!("[qio] 激活处理调度到主线程失败：{err}");
                }
            }
        });
        if let Err(err) = spawned {
            log::warn!("[qio] 启动单实例激活监听线程失败：{err}");
        }
    }

    /// 不同会话/账号已有实例：可关闭的提示框（说明为什么没有第二个窗口），然后调用方 exit 0。
    ///
    /// 无人值守环境（真机脚本）用 QIO_STARTUP_ERROR_DIALOG=0 关掉模态框，行为保持「退出 0」。
    pub fn show_already_running_dialog() {
        if std::env::var("QIO_STARTUP_ERROR_DIALOG").ok().as_deref() == Some("0") {
            return;
        }
        let text = "QIO 已在此电脑运行。\n\n为了避免两个实例同时写同一份数据，本次不会启动第二个窗口。\n请切换到已经打开的 QIO 窗口；如果找不到它，请从任务栏或系统托盘打开，或结束已有的 QIO 进程后再启动。";
        let mut wide: Vec<u16> = text.encode_utf16().collect();
        wide.push(0);
        let mut title: Vec<u16> = "QIO 已在此电脑运行".encode_utf16().collect();
        title.push(0);
        unsafe {
            MessageBoxW(
                std::ptr::null_mut(),
                wide.as_ptr(),
                title.as_ptr(),
                MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND,
            );
        }
    }

    /// 跨进程契约测试探针：打印 JSON 后退出（不初始化日志/数据目录/窗口/后端）。
    ///
    /// 模式来自 QIO_INSTANCE_PROBE：
    /// * 'acquire'（默认）：只取锁并报告角色；
    /// * 'hold'：主实例保持存活，可选等待激活 / 置位交接 / 延迟退出（由 QIO_INSTANCE_PROBE_* 控制）。
    pub fn run_probe(instance: Acquire) -> ! {
        let mode = std::env::var(PROBE_ENV)
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase();
        match instance {
            Acquire::Duplicate { same_session } => {
                emit_probe(serde_json::json!({
                    "role": "duplicate",
                    "sameSession": same_session,
                }));
                std::process::exit(0);
            }
            Acquire::Primary(guard) => {
                emit_probe(serde_json::json!({ "role": "primary" }));
                if mode == "hold" {
                    if let Some(millis) = env_u64("QIO_INSTANCE_PROBE_ACTIVATE_WAIT_MS") {
                        let activated = wait_activation(
                            guard.activation_handle(),
                            Duration::from_millis(millis),
                        );
                        emit_probe(serde_json::json!({
                            "event": "activated",
                            "activated": activated,
                        }));
                    }
                    if let Some(millis) = env_u64("QIO_INSTANCE_PROBE_ARM_HANDOFF_MS") {
                        std::thread::sleep(Duration::from_millis(millis));
                        let armed = arm_relaunch_handoff();
                        emit_probe(serde_json::json!({
                            "event": "handoff",
                            "armed": armed,
                        }));
                    }
                    if let Some(millis) = env_u64("QIO_INSTANCE_PROBE_EXIT_AFTER_ARM_MS") {
                        std::thread::sleep(Duration::from_millis(millis));
                    }
                    if let Some(millis) = env_u64("QIO_INSTANCE_PROBE_HOLD_MS") {
                        std::thread::sleep(Duration::from_millis(millis));
                    }
                }
                drop(guard);
                std::process::exit(0);
            }
        }
    }

    fn wait_activation(handle: Option<HANDLE>, timeout: Duration) -> bool {
        let Some(handle) = handle else {
            return false;
        };
        let millis = timeout.as_millis().min(u32::MAX as u128) as u32;
        unsafe {
            let state = WaitForSingleObject(handle, millis);
            if state == WAIT_OBJECT_0 {
                ResetEvent(handle);
                true
            } else {
                false
            }
        }
    }

    fn emit_probe(value: serde_json::Value) {
        // 一行一份 JSON：跨进程测试按行读，不受日志/调试输出影响。
        println!("{value}");
    }

    fn env_u64(name: &str) -> Option<u64> {
        std::env::var(name).ok()?.trim().parse::<u64>().ok()
    }
}

#[cfg(not(windows))]
mod fallback {
    //! 非 Windows：没有命名内核对象语义（本产品只在 Windows 分发）。
    //! 保留同签名让 main.rs 跨平台可编译；不声称实现了单实例。

    use super::*;

    pub struct Guard;

    impl Drop for Guard {
        fn drop(&mut self) {}
    }

    pub fn acquire() -> Acquire {
        Acquire::Primary(Guard)
    }

    pub fn install_guard(_guard: Guard) {}

    pub fn spawn_activation_listener(_app: tauri::AppHandle) {}

    pub fn arm_relaunch_handoff() -> bool {
        false
    }

    pub fn try_wait_for_relaunch_handoff(_max_wait: Duration) -> bool {
        true
    }

    pub fn signal_activation() -> bool {
        false
    }

    pub fn show_already_running_dialog() {}

    pub fn run_probe(_instance: Acquire) -> ! {
        eprintln!("[qio] 单实例探针只在 Windows 上可用");
        std::process::exit(2);
    }
}

// 这些是给 main.rs / 契约测试用的模块级 API；bin crate 内没有外部引用时不算未使用。
#[allow(unused_imports)]
#[cfg(windows)]
pub use win::{
    acquire, arm_relaunch_handoff, install_guard, run_probe, show_already_running_dialog,
    signal_activation, spawn_activation_listener, try_wait_for_relaunch_handoff, Guard,
};

// 同 win 分支：模块级 API，bin crate 内可能没有直接引用。
#[allow(unused_imports)]
#[cfg(not(windows))]
pub use fallback::{
    acquire, arm_relaunch_handoff, install_guard, run_probe, show_already_running_dialog,
    signal_activation, spawn_activation_listener, try_wait_for_relaunch_handoff, Guard,
};

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lock_name_is_global_and_product_scoped() {
        let name = name_with(LOCK_NAME, "");
        assert_eq!(name, "Global\\QIO.SingleInstance.v1");
        // Global 命名空间 + 只有产品/协议版本：不带安装路径、账号、会话、提权、版本。
        assert!(name.starts_with("Global\\"));
        assert_eq!(name.matches('\\').count(), 1);
        for forbidden in [
            "qio.exe", "Users", "AppData", "Program Files", "S-1-", "0.3", "session",
        ] {
            assert!(
                !name.to_ascii_lowercase().contains(&forbidden.to_ascii_lowercase()),
                "锁名不应包含 {forbidden}：{name}"
            );
        }
    }

    #[test]
    fn events_are_session_local() {
        assert!(name_with(ACTIVATE_EVENT_NAME, "").starts_with("Local\\"));
        assert!(name_with(HANDOFF_EVENT_NAME, "").starts_with("Local\\"));
        assert_eq!(ACTIVATE_EVENT_NAME, "Local\\QIO.SingleInstance.Activate.v1");
        assert_eq!(HANDOFF_EVENT_NAME, "Local\\QIO.SingleInstance.Handoff.v1");
    }

    #[test]
    fn test_suffix_isolated_and_sanitized() {
        // 空/空白 → 产品级名字（生产路径）
        assert_eq!(sanitize_suffix(""), "");
        assert_eq!(sanitize_suffix("   "), "");
        // 合法后缀直接拼在后面
        assert_eq!(sanitize_suffix("abc-1_2"), ".abc-1_2");
        assert_eq!(name_with(LOCK_NAME, ".t1"), "Global\\QIO.SingleInstance.v1.t1");
        // 路径/空格/反斜杠/冒号不能进入对象名
        assert_eq!(sanitize_suffix("C:\\Users\\a b"), ".CUsersab");
        assert_eq!(sanitize_suffix("x/y:z"), ".xyz");
    }

    /// 读/写进程级环境变量的用例必须串行（cargo test 默认多线程）。
    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    fn env_guard() -> std::sync::MutexGuard<'static, ()> {
        ENV_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    #[test]
    fn formal_build_objects_are_fixed_product_names() {
        // 正式构建（release / 安装版）：三个对象名恒为产品名，与安装路径、Windows
        // 账号、登录会话、是否提权、QIO 版本都无关。
        assert_eq!(resolve_suffix(false), "");
        assert_eq!(name_with(LOCK_NAME, &resolve_suffix(false)), LOCK_NAME);
        assert_eq!(
            name_with(ACTIVATE_EVENT_NAME, &resolve_suffix(false)),
            ACTIVATE_EVENT_NAME
        );
        assert_eq!(
            name_with(HANDOFF_EVENT_NAME, &resolve_suffix(false)),
            HANDOFF_EVENT_NAME
        );
    }

    #[test]
    fn formal_build_ignores_suffix_env_even_when_set() {
        let _guard = env_guard();
        std::env::set_var(SUFFIX_ENV, "inherited-a");
        // allowed=false（正式构建语义）：即使进程继承了 QIO_INSTANCE_PROBE_SUFFIX 也
        // 不读 → 空后缀 / 产品锁名。release exe 继承测试 env 时必须是这个结果。
        assert_eq!(resolve_suffix(false), "");
        assert_eq!(name_with(LOCK_NAME, &resolve_suffix(false)), LOCK_NAME);
        // 测试构建（cargo test 的 debug 构建）仍按既有 sanitize 规则隔离锁名，
        // 否则跨进程契约测试会互相抢锁。
        if test_build_env_allowed() {
            assert_eq!(resolve_suffix(true), ".inherited-a");
            assert_eq!(suffix(), ".inherited-a");
        }
        std::env::remove_var(SUFFIX_ENV);
    }

    #[test]
    fn probe_only_requested_in_test_build() {
        let _guard = env_guard();
        std::env::set_var(PROBE_ENV, "hold");
        // 显式 allowed=false = 正式构建语义：即使 QIO_INSTANCE_PROBE 有值也恒 false
        // （release exe 不允许因为继承测试 env 而走「探针：打印 JSON 后退出」）。
        assert!(!probe_requested_when(false));
        // 正式构建的 probe_requested() 只有一处 cfg! 接入：必须等价于显式参数版本。
        assert_eq!(
            probe_requested(),
            probe_requested_when(test_build_env_allowed())
        );
        if test_build_env_allowed() {
            assert!(probe_requested());
            assert!(probe_requested_when(true));
        }
        std::env::remove_var(PROBE_ENV);
    }

    #[test]
    fn test_build_gate_is_debug_assertions() {
        // gating 依据：debug_assertions。cargo test 的 CARGO_BIN_EXE_qio 是 debug 构建，
        // tauri build / 安装版是 release（debug_assertions=false）。
        // 若有人用 --release 跑 cargo test，后缀隔离与探针的前提不成立：应尽早失败，
        // 而不是悄悄让所有用例共用产品锁名。
        assert!(
            test_build_env_allowed(),
            "cargo test 必须是 debug 构建（后缀隔离与探针依赖 debug_assertions）"
        );
    }
}
