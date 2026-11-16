//! 桌面全机单实例的**跨进程契约测试**（集成测试，真二进制、真内核对象）。
//!
//! 为什么必须是跨进程：单实例的全部语义都在「两个进程同时存在」时才成立 ——
//! 同一个进程里 CreateMutexW 会自我兼容（递归持有/ALREADY_EXISTS），测不出真实行为。
//!
//! 机制：qio.exe 在 main() 取锁分支之后带 QIO_INSTANCE_PROBE 探针（仅测试构建可用）：
//! 只走锁/激活/交接逻辑，打印一行 JSON 后退出，不初始化日志、不碰数据目录、不建窗口、
//! 不拉后端。测试用 CARGO_BIN_EXE_qio 拉起真实二进制（debug 构建，debug_assertions=true）；
//! release/安装版按设计**恒忽略** QIO_INSTANCE_PROBE/QIO_INSTANCE_PROBE_SUFFIX（r10）。
//!
//! 覆盖（计划 Task 1 的 TDD 证据）：
//! 1. 首个进程 primary；
//! 2. 第二个同会话进程 duplicate（sameSession=true），并让首个进程观察到 activated；
//! 3. 更新重启交接：旧实例置位 handoff 后延迟退出 → 新实例等待（不是直接判重复）后成为 primary。
//!
//! 隔离：每个用例用 QIO_INSTANCE_PROBE_SUFFIX 生成**独立锁名**（同名对象跨用例会互相抢锁，
//! 让测试自己变成竞态源）；后缀只在测试构建生效（见 single_instance::resolve_suffix）。
//! 不同 Windows 账号/会话的互斥在本机单会话里无法制造，记 UNVERIFIED。

#![cfg(windows)]

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::time::{Duration, Instant};

use qio_core::ownership::kill_tree_by_pid;

/// cargo 在集成测试里保证这个变量存在（值 = 构建出来的 qio.exe 绝对路径）。
const QIO_BIN: &str = env!("CARGO_BIN_EXE_qio");

/// 探针进程：包住真实 qio.exe + 按行读取的 stdout。
struct Probe {
    child: Child,
    lines: Receiver<String>,
    label: String,
}

impl Probe {
    fn spawn(suffix: &str, mode: &str, extra: &[(&str, String)]) -> Probe {
        let data_dir = std::env::temp_dir().join(format!("qio-probe-data-{suffix}"));
        let mut command = Command::new(QIO_BIN);
        command
            .env("QIO_INSTANCE_PROBE", mode)
            .env("QIO_INSTANCE_PROBE_SUFFIX", suffix)
            // 探针不会碰数据目录；这里显式隔离，避免万一退化成完整启动时污染真实用户数据。
            .env("QIO_DATA_DIR", &data_dir)
            .env("QIO_STARTUP_ERROR_DIALOG", "0")
            .env("QIO_DISABLE_DB_CHECK", "1")
            .env("QIO_SESSION_TOKEN", format!("probe-token-{suffix}"))
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        for (key, value) in extra {
            command.env(key, value);
        }
        let mut child = command
            .spawn()
            .unwrap_or_else(|err| panic!("拉起 qio.exe 探针失败（{mode}）：{err}"));
        let stdout = child.stdout.take().expect("探针 stdout 已被取走");
        let (sender, lines) = mpsc::channel();
        std::thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines() {
                match line {
                    Ok(text) => {
                        if sender.send(text).is_err() {
                            return;
                        }
                    }
                    Err(_) => return,
                }
            }
        });
        Probe {
            child,
            lines,
            label: suffix.to_string(),
        }
    }

    /// 读一行 JSON（超时/提前退出都是失败，信息里带上下文）。
    fn read_json(&mut self, timeout: Duration, what: &str) -> serde_json::Value {
        match self.lines.recv_timeout(timeout) {
            Ok(line) => match serde_json::from_str(&line) {
                Ok(value) => value,
                Err(err) => panic!(
                    "[{}] {what}：探针输出不是 JSON（{err}）；原始行={line:?}",
                    self.label
                ),
            },
            Err(RecvTimeoutError::Timeout) => {
                let state = self
                    .child
                    .try_wait()
                    .map(|status| format!("{status:?}"))
                    .unwrap_or_else(|err| format!("查询失败：{err}"));
                panic!(
                    "[{}] {what}：{:?} 内没有输出 JSON 行（进程状态={state}）。\
                     基线（无单实例闸门）在这里会卡住 → 这正是要修的缺陷",
                    self.label, timeout
                )
            }
            Err(RecvTimeoutError::Disconnected) => panic!(
                "[{}] {what}：进程在输出 JSON 之前就退出了",
                self.label
            ),
        }
    }

    fn wait_exit(&mut self, timeout: Duration, what: &str) -> ExitStatus {
        let deadline = Instant::now() + timeout;
        loop {
            match self
                .child
                .try_wait()
                .unwrap_or_else(|err| panic!("[{}] {what}：查询进程状态失败：{err}", self.label))
            {
                Some(status) => return status,
                None if Instant::now() >= deadline => {
                    panic!("[{}] {what}：{:?} 内没有退出", self.label, timeout)
                }
                None => std::thread::sleep(Duration::from_millis(50)),
            }
        }
    }
}

impl Drop for Probe {
    fn drop(&mut self) {
        // 只按 pid 收整棵树（绝不按映像名）：测试失败/进程卡死时也必须收干净。
        let _ = kill_tree_by_pid(self.child.id());
        let _ = self.child.wait();
    }
}

/// 每个用例独立的锁名后缀（避免并发用例互相抢同一把锁）。
fn unique_suffix(tag: &str) -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("系统时间早于 UNIX_EPOCH")
        .as_nanos();
    format!("t{}-{}-{}", std::process::id(), tag, nanos)
}

#[test]
fn first_process_becomes_primary() {
    let suffix = unique_suffix("primary");
    let mut probe = Probe::spawn(&suffix, "acquire", &[]);
    let role = probe.read_json(Duration::from_secs(60), "首个进程应报告 primary");
    assert_eq!(
        role["role"], "primary",
        "本机没有其它实例时，第一个进程必须成为主实例：{role}"
    );
    let status = probe.wait_exit(Duration::from_secs(30), "primary 探针退出");
    assert!(status.success(), "primary 探针应正常退出（exit 0）：{status:?}");
}

#[test]
fn second_same_session_process_is_duplicate_and_activates_first() {
    let suffix = unique_suffix("duplicate");
    let mut first = Probe::spawn(
        &suffix,
        "hold",
        &[("QIO_INSTANCE_PROBE_ACTIVATE_WAIT_MS", "30000".to_string())],
    );
    let first_role = first.read_json(Duration::from_secs(60), "首个进程应报告 primary");
    assert_eq!(first_role["role"], "primary", "第一个进程应是主实例：{first_role}");

    let mut second = Probe::spawn(&suffix, "acquire", &[]);
    let second_role = second.read_json(Duration::from_secs(60), "第二个同会话进程应报告 duplicate");
    assert_eq!(
        second_role["role"], "duplicate",
        "第二个同会话进程不得成为主实例（否则就是两套应用）：{second_role}"
    );
    assert_eq!(
        second_role["sameSession"], true,
        "同会话重复启动必须能打开激活事件（sameSession=true）：{second_role}"
    );
    let second_status = second.wait_exit(Duration::from_secs(30), "duplicate 探针退出");
    assert!(second_status.success(), "duplicate 探针应正常退出（exit 0）");

    let activated = first.read_json(Duration::from_secs(30), "首个进程应观察到 activated");
    assert_eq!(activated["event"], "activated", "激活事件未送达：{activated}");
    assert_eq!(
        activated["activated"], true,
        "同会话重复启动必须置位首个进程的激活事件：{activated}"
    );
    let first_status = first.wait_exit(Duration::from_secs(30), "hold 探针退出");
    assert!(first_status.success(), "hold 探针应正常退出（exit 0）");
}

#[test]
fn relaunch_handoff_waits_for_old_instance_then_becomes_primary() {
    let suffix = unique_suffix("handoff");
    // 旧实例：先成为 primary，200ms 后置位交接事件，再延迟 2.5s 退出（模拟更新重启时
    // 「新进程已拉起、旧进程还没退完」的窗口）。
    let mut old = Probe::spawn(
        &suffix,
        "hold",
        &[
            ("QIO_INSTANCE_PROBE_ARM_HANDOFF_MS", "200".to_string()),
            ("QIO_INSTANCE_PROBE_EXIT_AFTER_ARM_MS", "2500".to_string()),
        ],
    );
    let old_role = old.read_json(Duration::from_secs(60), "旧实例应报告 primary");
    assert_eq!(old_role["role"], "primary", "旧实例应是主实例：{old_role}");
    let armed = old.read_json(Duration::from_secs(30), "旧实例应报告 handoff 置位");
    assert_eq!(armed["event"], "handoff", "旧实例没有进入交接流程：{armed}");
    assert_eq!(armed["armed"], true, "更新交接事件置位失败：{armed}");

    // 新实例：旧实例仍持有锁，但交接事件已置位 → 必须等待旧进程退出后接任，而不是判重复。
    let started = Instant::now();
    let mut new_process = Probe::spawn(&suffix, "acquire", &[]);
    let role = new_process.read_json(
        Duration::from_secs(60),
        "更新交接中的新实例应等待后成为 primary",
    );
    let waited = started.elapsed();
    assert_eq!(
        role["role"], "primary",
        "更新重启的新实例必须接任主实例（而不是弹「已运行」后退出）：{role}"
    );
    assert!(
        waited >= Duration::from_millis(1000),
        "新实例应等待旧实例退出释放锁（实测只等了 {waited:?}，说明没有走交接等待）"
    );
    let new_status = new_process.wait_exit(Duration::from_secs(30), "新实例探针退出");
    assert!(new_status.success(), "新实例探针应正常退出（exit 0）");
    let old_status = old.wait_exit(Duration::from_secs(30), "旧实例探针退出");
    assert!(old_status.success(), "旧实例探针应正常退出（exit 0）");
}
