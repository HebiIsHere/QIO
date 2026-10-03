//! 卸载帮助程序的**进程级**契约测试（集成测试）。
//!
//! 为什么放在 src/ 下而不是 tests/：这个文件必须作为**集成测试**被编译，
//! cargo 才会真的构建 bin 产物、并把 CARGO_BIN_EXE_qio-uninstall-helper 注入进来 ——
//! lib 的单元测试里拿不到那个变量，而帮助程序的退出码/一行 JSON/console 行为
//! 只有在**真进程**上才算证明过（NSIS 的 nsExec 就靠这些）。
//! 在 Cargo.toml 里的声明：[[test]] name = "uninstall_helper_cli" path = "src/tests_uninstall_helper_cli.rs"
//!
//! 夹具全部是真进程 + 临时目录，不 mock 进程表。

use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::SystemTime;

use qio_core::ownership::{
    iso8601_utc, kill_tree_by_pid, lease_path, write_lease, Lease, LeaseProcess, ProcessGuard,
    LEASE_SCHEMA,
};

/// cargo 在集成测试里保证这个变量存在（值 = 构建出来的帮助程序绝对路径）。
const HELPER: &str = env!("CARGO_BIN_EXE_qio-uninstall-helper");

// ---------- 夹具 ----------

struct TempDir(PathBuf);

impl TempDir {
    fn new(name: &str) -> TempDir {
        let mut dir = std::env::temp_dir();
        dir.push(format!("qio-helper-cli-{}-{}", name, std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("建测试临时目录失败");
        TempDir(dir)
    }
    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

struct Proc(Option<std::process::Child>);

impl Proc {
    fn spawn(mut cmd: Command) -> Proc {
        use std::process::Stdio;
        cmd.stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        Proc(Some(cmd.spawn().expect("拉起测试子进程失败")))
    }
    fn pid(&self) -> u32 {
        self.0.as_ref().expect("进程句柄已被取走").id()
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

fn system32(name: &str) -> PathBuf {
    let root = std::env::var("SystemRoot").unwrap_or_else(|_| "C:\\Windows".to_string());
    PathBuf::from(root).join("System32").join(name)
}

fn ping(seconds: u32) -> Command {
    let mut cmd = Command::new(system32("ping.exe"));
    cmd.args(["-n", &seconds.to_string(), "127.0.0.1"]);
    cmd
}

fn alive(pid: u32) -> bool {
    ProcessGuard::open(pid)
        .map(|guard| guard.is_alive())
        .unwrap_or(false)
}

fn identity_tuple(pid: u32) -> (u32, String, String) {
    let guard = ProcessGuard::open(pid).unwrap_or_else(|| panic!("打开 pid {pid} 失败"));
    (
        pid,
        guard.created_filetime().expect("读创建时间失败"),
        guard.exe().expect("读映像路径失败"),
    )
}

/// 安装目录里的"替身后端"：真 exe、真 pid，但没有 lease 认领它。
fn decoy_backend(dir: &Path) -> (PathBuf, Proc) {
    let exe = dir.join("qio-backend.exe");
    std::fs::copy(system32("ping.exe"), &exe).expect("复制替身 exe 失败");
    let mut cmd = Command::new(&exe);
    cmd.args(["-n", "120", "127.0.0.1"]);
    (exe, Proc::spawn(cmd))
}

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

/// 跑真的帮助程序，返回 (退出码, stdout, stderr)。
fn run_helper(args: &[&str]) -> (i32, String, String) {
    let out = Command::new(HELPER)
        .args(args)
        .output()
        .expect("运行帮助程序失败");
    (
        out.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&out.stdout).to_string(),
        String::from_utf8_lossy(&out.stderr).to_string(),
    )
}

// ---------- 测试 ----------

#[test]
fn refuses_the_decoy_backend_with_exit_code_3() {
    let dir = TempDir::new("refuse");
    let (_exe, decoy) = decoy_backend(dir.path());
    let dir_arg = dir.path().to_string_lossy().to_string();
    let (code, stdout, stderr) = run_helper(&[
        "--close-installation",
        "--install-dir",
        dir_arg.as_str(),
        "--json",
    ]);
    assert_eq!(code, 3, "stdout={stdout} stderr={stderr}");
    assert_eq!(
        stdout.trim().lines().count(),
        1,
        "--json 必须只有一行：{stdout}"
    );
    assert!(stdout.contains("\"reason\":\"lease_missing\""), "{stdout}");
    assert!(stdout.contains("\"exit_code\":3"), "{stdout}");
    assert!(alive(decoy.pid()), "被拒绝的替身后端必须还活着");
}

#[test]
fn rejects_missing_or_conflicting_arguments_with_exit_code_2() {
    let (code, _out, err) = run_helper(&["--close-installation"]);
    assert_eq!(code, 2, "缺 --install-dir 应当是用法错误：{err}");
    let (code, _out, err) = run_helper(&[
        "--check-only",
        "--close-installation",
        "--install-dir",
        "C:\\x",
    ]);
    assert_eq!(code, 2, "两个模式不能同时给：{err}");
    let (code, _out, _err) = run_helper(&["--unknown-flag"]);
    assert_eq!(code, 2);
}

#[test]
fn check_only_is_read_only_and_reports_a_running_shell() {
    let dir = TempDir::new("check-only");
    let (_exe, backend) = decoy_backend(dir.path());
    let shell = Proc::spawn(ping(120));
    let shell_identity = identity_tuple(shell.pid());
    let backend_identity = identity_tuple(backend.pid());
    write_lease(
        dir.path(),
        &lease_with(dir.path(), &shell_identity, &backend_identity),
    )
    .unwrap();
    let dir_arg = dir.path().to_string_lossy().to_string();

    let (code, stdout, stderr) = run_helper(&[
        "--check-only",
        "--install-dir",
        dir_arg.as_str(),
        "--json",
    ]);
    assert_eq!(code, 0, "壳在跑就应当返回 0（可收）：stdout={stdout} stderr={stderr}");
    assert_eq!(stdout.trim().lines().count(), 1, "{stdout}");
    assert!(stdout.contains("\"action\":\"would_close\""), "{stdout}");
    assert!(stdout.contains("\"reason\":\"shell_running\""), "{stdout}");
    assert!(
        alive(shell.pid()) && alive(backend.pid()),
        "--check-only 跑完两个真进程都必须还活着"
    );
    assert!(lease_path(dir.path()).exists(), "--check-only 不许删 lease");
}

#[test]
fn closes_the_recorded_installation_and_removes_the_lease() {
    let dir = TempDir::new("close");
    let (_exe, backend) = decoy_backend(dir.path());
    let shell = Proc::spawn(ping(120)); // 没有顶层窗口 → 走"超时后按 pid 收"的兜底路径
    let shell_identity = identity_tuple(shell.pid());
    let backend_identity = identity_tuple(backend.pid());
    write_lease(
        dir.path(),
        &lease_with(dir.path(), &shell_identity, &backend_identity),
    )
    .unwrap();
    let dir_arg = dir.path().to_string_lossy().to_string();

    let (code, stdout, stderr) = run_helper(&[
        "--close-installation",
        "--install-dir",
        dir_arg.as_str(),
        "--timeout-ms",
        "300",
        "--json",
    ]);
    assert_eq!(code, 0, "stdout={stdout} stderr={stderr}");
    assert!(stdout.contains("\"killed_backend\":true"), "{stdout}");
    assert!(stdout.contains("\"killed_shell\":true"), "{stdout}");
    assert!(!alive(shell.pid()) && !alive(backend.pid()));
    assert!(!lease_path(dir.path()).exists());
}

#[test]
fn allow_main_exe_collects_only_the_recorded_shell() {
    let dir = TempDir::new("allow-main-exe");
    let (_exe, decoy) = decoy_backend(dir.path());
    let unrelated = Proc::spawn(ping(120));
    let shell = Proc::spawn(ping(120));
    let shell_identity = identity_tuple(shell.pid());
    // backend 记录对不上（创建时间是编的）：--allow-main-exe 时不许碰它，但要收壳
    let backend = (
        unrelated.pid(),
        "1".to_string(),
        system32("ping.exe").to_string_lossy().to_string(),
    );
    write_lease(
        dir.path(),
        &lease_with(dir.path(), &shell_identity, &backend),
    )
    .unwrap();
    let dir_arg = dir.path().to_string_lossy().to_string();

    let (code, stdout, stderr) = run_helper(&[
        "--close-installation",
        "--allow-main-exe",
        "--install-dir",
        dir_arg.as_str(),
        "--timeout-ms",
        "300",
        "--json",
    ]);
    assert_eq!(code, 0, "stdout={stdout} stderr={stderr}");
    assert!(stdout.contains("\"killed_shell\":true"), "{stdout}");
    assert!(stdout.contains("\"killed_backend\":false"), "{stdout}");
    assert!(!alive(shell.pid()), "本实例的壳必须被收掉");
    assert!(
        alive(unrelated.pid()),
        "backend 记录对不上的进程即使活着也不许被结束"
    );
    assert!(alive(decoy.pid()), "安装目录里的替身后端不许被碰");
}
