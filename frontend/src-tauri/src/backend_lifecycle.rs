//! 后端进程树的结束与**退出验证**（更新安装准备路径）。
//!
//! 为什么单独成模块并带单测：安装器写不进 `qio-backend.exe` 的根因就是残留进程
//! （PyInstaller onefile 的 launcher 被杀掉后，真正提供服务的 child 还活着）。
//! 「结束了」这件事必须是**验证过的结论**，不是一次 taskkill 的返回值 ——
//! 超时、权限错误、验证失败都不许报成功，否则更新会在安装那一步失败。
//!
//! 原则：
//! * 只按 **pid**（`taskkill /PID <pid> /T /F`），绝不按映像名批量杀（别的实例不能被误伤）；
//! * 结束前后都拿 pid 名单：先快照后代，再逐个验证它们真的没了；
//! * 验证窗口有硬上限，超时就如实报失败并带上幸存 pid。

use std::time::{Duration, Instant};

use qio_core::ownership::{kill_tree_by_pid_verbose, ProcessGuard};

/// 发出结束动作之后，等整棵树消失的上限。
pub const STOP_VERIFY_BUDGET: Duration = Duration::from_secs(5);
/// 轮询间隔。
const POLL_INTERVAL: Duration = Duration::from_millis(100);

/// 一次「结束后端」的真实结果。
///
/// 字段名按 camelCase 交给前端（`qio_prepare_for_update` 的返回值）。
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct StopReport {
    /// 本实例后端的 pid（本来就没有 → None）
    pub pid: Option<u32>,
    /// 调用时后端就已经不在（无需恢复）
    pub already_stopped: bool,
    /// 结束动作是否成功（`verified` 为真时恒为真）
    pub stopped: bool,
    /// 进程树是否**确认**退出（调用方只有拿到 true 才允许继续安装）
    pub verified: bool,
    /// 结束时在名单里、且已确认退出的 pid
    pub exited: Vec<u32>,
    /// 验证窗口结束时还活着的 pid（失败时才非空）
    pub survivors: Vec<u32>,
    /// 人话说明（进日志与前端错误信息）
    pub detail: String,
}

/// 结束失败时的结构化结果：调用方需要知道**还有谁活着**（恢复流程要再清一遍）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StopFailure {
    pub detail: String,
    pub survivors: Vec<u32>,
}

impl StopReport {
    /// 「本来就不在」：不是失败，也无需恢复。
    pub fn already_stopped(detail: impl Into<String>) -> Self {
        Self {
            pid: None,
            already_stopped: true,
            stopped: true,
            verified: true,
            exited: Vec::new(),
            survivors: Vec::new(),
            detail: detail.into(),
        }
    }
}

/// 进程是否还活着。
///
/// 用 `ProcessGuard`（句柄 + 退出码）而不是"OpenProcess 成不成功"：
/// 进程退出后进程对象还在时 OpenProcess 照样成功，那会把「已经死了」判成「还活着」。
pub fn process_alive(pid: u32) -> bool {
    ProcessGuard::open(pid).map(|guard| guard.is_alive()).unwrap_or(false)
}

/// 某个 pid 当前的全部后代（含孙代），按 BFS 展开。
///
/// 结束前先记名单：`taskkill /T` 会带走它们，但"有没有真的带走"只能靠这份名单验证。
#[cfg(windows)]
pub fn descendant_pids(pid: u32) -> Vec<u32> {
    use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W, TH32CS_SNAPPROCESS,
    };

    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) };
    if snapshot == INVALID_HANDLE_VALUE {
        log::warn!("[qio] 进程快照失败，无法列出后代 pid");
        return Vec::new();
    }
    let mut entry: PROCESSENTRY32W = unsafe { std::mem::zeroed() };
    entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
    let mut pairs: Vec<(u32, u32)> = Vec::new();
    let mut ok = unsafe { Process32FirstW(snapshot, &mut entry) };
    while ok != 0 {
        pairs.push((entry.th32ProcessID, entry.th32ParentProcessID));
        ok = unsafe { Process32NextW(snapshot, &mut entry) };
    }
    unsafe { CloseHandle(snapshot) };

    let mut out: Vec<u32> = Vec::new();
    let mut frontier: Vec<u32> = vec![pid];
    while let Some(parent) = frontier.pop() {
        for (child, ppid) in &pairs {
            if *ppid == parent && *child != pid && !out.contains(child) {
                out.push(*child);
                frontier.push(*child);
            }
        }
    }
    out
}

#[cfg(not(windows))]
pub fn descendant_pids(_pid: u32) -> Vec<u32> {
    Vec::new()
}

/// 结束某个 pid 的整棵进程树，并**验证**它真的退出了。
///
/// * `Ok(report)`：`verified == true` —— 要么本来就不在（`already_stopped`），要么已确认退出；
/// * `Err(failure)`：结束动作失败或验证窗口内仍有幸存进程 —— 调用方**不得**继续安装。
pub fn stop_tree_by_pid(pid: u32, budget: Duration) -> Result<StopReport, StopFailure> {
    if !process_alive(pid) {
        return Ok(StopReport::already_stopped(format!(
            "后端 pid {pid} 已经不在，无需结束"
        )));
    }
    let descendants = descendant_pids(pid);
    let mut names: Vec<u32> = vec![pid];
    names.extend(descendants.iter().copied());
    log::info!(
        "[qio] 准备结束后端进程树：pid {pid}，树内 pid {names:?}（taskkill /PID {pid} /T /F）"
    );

    let (kill_ok, kill_note) = kill_tree_by_pid_verbose(pid);
    let deadline = Instant::now() + budget;
    loop {
        let survivors: Vec<u32> = names.iter().copied().filter(|pid| process_alive(*pid)).collect();
        if survivors.is_empty() {
            let exited: Vec<u32> = names.clone();
            let detail = format!(
                "已结束本实例后端进程树（pid {pid}，共 {} 个进程）；taskkill：{kill_note}",
                exited.len()
            );
            log::info!("[qio] {detail}");
            return Ok(StopReport {
                pid: Some(pid),
                already_stopped: false,
                stopped: true,
                verified: true,
                exited,
                survivors: Vec::new(),
                detail,
            });
        }
        if Instant::now() >= deadline {
            let detail = format!(
                "结束后端进程树失败：pid {pid} 的树里仍有进程存活 {survivors:?}；\
                 taskkill 成功={kill_ok}（{kill_note}）"
            );
            log::error!("[qio] {detail}");
            return Err(StopFailure { detail, survivors });
        }
        std::thread::sleep(POLL_INTERVAL);
    }
}

/// 按 pid 结束一批进程（用于恢复流程：清掉上一次没杀掉的幸存进程）。
///
/// 仍然是**只按 pid**：这些 pid 来自我们自己的后代快照，不是按映像名找出来的。
pub fn stop_pids(pids: &[u32], budget: Duration) -> Vec<u32> {
    let deadline = Instant::now() + budget;
    let mut remaining: Vec<u32> = pids.iter().copied().filter(|pid| process_alive(*pid)).collect();
    for pid in &remaining {
        let (ok, note) = kill_tree_by_pid_verbose(*pid);
        log::warn!("[qio] 清理上次残留的后端进程 pid {pid}：taskkill 成功={ok}（{note}）");
    }
    while !remaining.is_empty() && Instant::now() < deadline {
        std::thread::sleep(POLL_INTERVAL);
        remaining.retain(|pid| process_alive(*pid));
    }
    remaining
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::{Command, Stdio};

    /// 起一棵真的有两层进程的树：cmd → ping（模拟 onefile 的 launcher → child）。
    ///
    /// 只用一个长时间运行的子进程：早期版本写的是「先跑 2 秒的短 ping，再跑长的」，
    /// 快照可能正好抓到那个已经退出的短进程，于是并行跑测试时结果不稳定（踩过）。
    fn spawn_tree(seconds: u32) -> std::process::Child {
        let script = format!("ping -n {seconds} 127.0.0.1");
        Command::new("cmd")
            .args(["/c", script.as_str()])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("拉起测试子进程失败")
    }

    #[test]
    fn stops_the_whole_tree_and_verifies_every_pid_is_gone() {
        let mut tree = spawn_tree(30);
        let root = tree.id();
        // 等后代出现（cmd 先等一秒再起 ping）
        let deadline = Instant::now() + Duration::from_secs(10);
        let mut descendants = descendant_pids(root);
        while descendants.is_empty() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(200));
            descendants = descendant_pids(root);
        }
        assert!(!descendants.is_empty(), "测试树没有后代，无法验证整棵树");

        let report = stop_tree_by_pid(root, Duration::from_secs(10)).expect("结束整棵树应成功");
        assert!(report.verified, "必须报告「已确认退出」");
        assert!(report.stopped);
        assert!(!report.already_stopped);
        assert!(report.survivors.is_empty());
        assert!(!process_alive(root), "根进程还活着");
        for pid in descendants {
            assert!(!process_alive(pid), "后代 pid {pid} 还活着（安装器会被它挡住）");
        }
        let _ = tree.wait();
    }

    #[test]
    fn an_unknown_pid_is_reported_as_already_stopped_not_as_failure() {
        // 后端已经退出（或本来就没起来）时不该让更新失败：如实报"本来就不在"。
        let report = stop_tree_by_pid(0xFFFF_FFF0, Duration::from_secs(2))
            .expect("不存在的 pid 不该报失败");
        assert!(report.already_stopped);
        assert!(report.verified);
        assert_eq!(report.pid, None);
    }

    #[test]
    fn stop_pids_clears_residual_descendants_left_by_an_earlier_partial_kill() {
        // 恢复流程用得到：上一次只杀掉了根（或只杀掉了一部分）时，幸存的后代还在占着
        // qio-backend.exe。按 pid 名单再清一遍必须能把它们清干净。
        // 「幸存 → Err」那条路径需要构造一个**杀不掉**的进程（要提权/别的会话），
        // 本机无法构造，见汇报里的 NOT RUN。
        let mut tree = spawn_tree(30);
        let root = tree.id();
        let deadline = Instant::now() + Duration::from_secs(10);
        let mut descendants = descendant_pids(root);
        while descendants.is_empty() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(200));
            descendants = descendant_pids(root);
        }
        assert!(!descendants.is_empty(), "测试树没有后代，无法构造残留场景");

        // 只结束根进程（不带 /T）：后代留下来 —— 正是安装器报 Can't write 的场景
        assert!(qio_core::ownership::kill_pid(root), "结束根进程失败");
        let _ = tree.wait();
        assert!(
            descendants.iter().any(|pid| process_alive(*pid)),
            "残留进程没有构造出来"
        );

        let left = stop_pids(&descendants, Duration::from_secs(10));
        assert!(left.is_empty(), "残留进程没被清掉：{left:?}");
    }
}
