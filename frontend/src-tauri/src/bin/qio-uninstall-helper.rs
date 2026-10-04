//! qio-uninstall-helper —— 卸载器调用的「只收本安装实例 sidecar」帮助程序。
//!
//! 为什么是独立的 exe（不复用 qio.exe）：
//! * qio.exe 正在运行时删不掉也换不掉（映像以 FILE_SHARE_DELETE 打开），而卸载场景
//!   恰恰就是"qio.exe 可能正在跑"；
//! * 卸载器需要一个**控制台**程序，能取到退出码与输出（NSIS 的 nsExec::ExecToStack）。
//!   所以本 target 必须是 console 子系统：Rust 在 Windows 上默认就是 console，
//!   源文件里**不要**加 #![windows_subsystem = "windows"]（这里确实没有）。
//! * 所有权判定必须与主程序同源 —— 这里只调用 qio_core::ownership（同一份实现），
//!   本文件里没有任何"杀进程"的逻辑。
//!
//! 用法（两种模式二选一）：
//!   qio-uninstall-helper --close-installation --install-dir <DIR> [--timeout-ms 8000]
//!                        [--allow-main-exe] [--json]
//!   qio-uninstall-helper --check-only --install-dir <DIR> [--json]
//!
//! 退出码：
//!   0  --close-installation：本实例的进程已结束 / 已关闭；
//!      --check-only：**本安装实例的壳正在运行**（可收）；
//!   3  无法确认归属 / 本实例没在运行 / 记录对不上 —— **不动任何进程**（--check-only 只读）；
//!   2  参数错误（用法问题，什么都没做）。
//!
//! --json 时 stdout 只有一行 JSON（机器可读字段全在，含 pid / 判定 / reason）。

use std::path::PathBuf;
use std::time::Duration;

use qio_core::ownership::{self, CloseReport, CloseRequest};

const DEFAULT_TIMEOUT_MS: u64 = 8000;
const MAX_TIMEOUT_MS: u64 = 60_000;
const EXIT_OK: i32 = 0;
const EXIT_USAGE: i32 = 2;
const EXIT_UNCONFIRMED: i32 = 3;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Mode {
    Close,
    Check,
}

#[derive(Debug, Clone)]
struct Args {
    mode: Mode,
    install_dir: PathBuf,
    timeout_ms: u64,
    allow_main_exe: bool,
    json: bool,
}

fn usage() -> String {
    [
        "qio-uninstall-helper：只结束「本安装实例自己的」QIO 进程（按 lease + 进程三要素判定）",
        "",
        "用法：",
        "  qio-uninstall-helper --close-installation --install-dir <目录> [--timeout-ms 8000] [--allow-main-exe] [--json]",
        "  qio-uninstall-helper --check-only --install-dir <目录> [--json]",
        "",
        "  --close-installation  扫 <目录>\\sidecar.lease*.json，确认归属后收掉本实例的进程（多实例逐个收）",
        "  --check-only          只判定不动进程：0 = 本实例的壳正在运行（可收），3 = 无法确认",
        "  --install-dir <目录>  安装目录（外壳 exe 所在目录；所有权记录必须就在这里）",
        "  --timeout-ms <毫秒>   先发 WM_CLOSE 等外壳自己退出的时间，默认 8000，上限 60000",
        "  --allow-main-exe      允许只凭壳的三要素收壳（backend 记录对不上也不碰它）",
        "  --json                stdout 只输出一行 JSON 诊断",
        "",
        "退出码：0 = 已处理 / 可收；3 = 无法确认（不动任何进程）；2 = 参数错误",
    ]
    .join("\n")
}

fn set_mode(current: &mut Option<Mode>, next: Mode) -> Result<(), String> {
    match current {
        Some(existing) if *existing != next => {
            Err("--close-installation 与 --check-only 只能二选一".to_string())
        }
        _ => {
            *current = Some(next);
            Ok(())
        }
    }
}

fn parse_args(argv: &[String]) -> Result<Args, String> {
    let mut mode: Option<Mode> = None;
    let mut install_dir: Option<PathBuf> = None;
    let mut timeout_ms = DEFAULT_TIMEOUT_MS;
    let mut allow_main_exe = false;
    let mut json = false;

    let mut index = 0usize;
    while index < argv.len() {
        let raw = argv[index].clone();
        let (name, inline) = match raw.split_once('=') {
            Some((name, value)) => (name.to_string(), Some(value.to_string())),
            None => (raw.clone(), None),
        };
        // --flag=value 与 --flag value 都支持
        let value_of = |index: &mut usize| -> Result<String, String> {
            if let Some(value) = inline.clone() {
                return Ok(value);
            }
            *index += 1;
            argv.get(*index)
                .cloned()
                .ok_or_else(|| format!("{name} 缺少取值"))
        };
        match name.as_str() {
            "--close-installation" => set_mode(&mut mode, Mode::Close)?,
            "--check-only" => set_mode(&mut mode, Mode::Check)?,
            "--allow-main-exe" => allow_main_exe = true,
            "--json" => json = true,
            "--install-dir" => install_dir = Some(PathBuf::from(value_of(&mut index)?)),
            "--timeout-ms" => {
                let raw = value_of(&mut index)?;
                let parsed: u64 = raw
                    .parse()
                    .map_err(|_| format!("--timeout-ms 需要一个整数毫秒值，收到 {raw:?}"))?;
                if parsed == 0 || parsed > MAX_TIMEOUT_MS {
                    return Err(format!(
                        "--timeout-ms 必须在 1..={MAX_TIMEOUT_MS} 之间，收到 {parsed}"
                    ));
                }
                timeout_ms = parsed;
            }
            other => return Err(format!("不认识的参数：{other}")),
        }
        index += 1;
    }

    let mode = mode.ok_or_else(|| {
        "必须给出 --close-installation 或 --check-only 之一（本程序不会凭猜测动进程）".to_string()
    })?;
    let install_dir = install_dir.ok_or_else(|| "--install-dir 是必需的".to_string())?;
    if mode == Mode::Check && allow_main_exe {
        return Err("--allow-main-exe 只对 --close-installation 有意义".to_string());
    }
    Ok(Args {
        mode,
        install_dir,
        timeout_ms,
        allow_main_exe,
        json,
    })
}

fn print_human(args: &Args, report: &CloseReport) {
    let mode = match args.mode {
        Mode::Close => "close-installation",
        Mode::Check => "check-only",
    };
    println!("[qio-uninstall-helper] 模式={mode} 安装目录={}", report.install_dir);
    println!(
        "[qio-uninstall-helper] 判定={} reason={} exit={}",
        report.action, report.reason, report.exit_code
    );
    println!(
        "[qio-uninstall-helper] 后端 pid={:?} 身份对得上={} 活着={}",
        report.backend_pid, report.backend_identity_ok, report.backend_alive
    );
    println!(
        "[qio-uninstall-helper] 外壳 pid={:?} 身份对得上={} 活着={}",
        report.shell_pid, report.shell_identity_ok, report.shell_alive
    );
    println!(
        "[qio-uninstall-helper] 动作：WM_CLOSE={} 按 pid 结束后端={} 按 pid 结束外壳={} 删 lease={}",
        report.posted_close, report.killed_backend, report.killed_shell, report.lease_removed
    );
    println!("[qio-uninstall-helper] 说明：{}", report.message);
}

fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    if argv.iter().any(|arg| arg == "--help" || arg == "-h") {
        println!("{}", usage());
        std::process::exit(EXIT_OK);
    }
    let args = match parse_args(&argv) {
        Ok(args) => args,
        Err(err) => {
            eprintln!("[qio-uninstall-helper] 参数错误：{err}");
            eprintln!("{}", usage());
            std::process::exit(EXIT_USAGE);
        }
    };

    let request = CloseRequest {
        install_dir: args.install_dir.clone(),
        timeout: Duration::from_millis(args.timeout_ms),
        allow_main_exe: args.allow_main_exe,
    };
    let report = match args.mode {
        Mode::Close => ownership::close_installation(&request),
        Mode::Check => ownership::check_installation(&request),
    };

    if args.json {
        match serde_json::to_string(&report) {
            Ok(line) => println!("{line}"),
            Err(err) => {
                eprintln!("[qio-uninstall-helper] 序列化诊断失败：{err}");
                std::process::exit(EXIT_USAGE);
            }
        }
    } else {
        print_human(&args, &report);
    }

    let code = match report.exit_code {
        EXIT_OK => EXIT_OK,
        EXIT_UNCONFIRMED => EXIT_UNCONFIRMED,
        other => other,
    };
    std::process::exit(code);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Result<Args, String> {
        let owned: Vec<String> = args.iter().map(|arg| arg.to_string()).collect();
        parse_args(&owned)
    }

    #[test]
    fn parses_both_modes_and_both_value_forms() {
        let close = parse(&["--close-installation", "--install-dir", "C:\\QIO"]).unwrap();
        assert_eq!(close.mode, Mode::Close);
        assert_eq!(close.install_dir, PathBuf::from("C:\\QIO"));
        assert_eq!(close.timeout_ms, DEFAULT_TIMEOUT_MS);
        assert!(!close.allow_main_exe);

        let check = parse(&["--check-only", "--install-dir=C:\\QIO", "--json"]).unwrap();
        assert_eq!(check.mode, Mode::Check);
        assert!(check.json);
        assert_eq!(check.install_dir, PathBuf::from("C:\\QIO"));

        let allow = parse(&[
            "--close-installation",
            "--install-dir",
            "C:\\QIO",
            "--allow-main-exe",
            "--timeout-ms=250",
        ])
        .unwrap();
        assert!(allow.allow_main_exe);
        assert_eq!(allow.timeout_ms, 250);
    }

    #[test]
    fn rejects_conflicting_or_missing_arguments() {
        assert!(parse(&["--install-dir", "C:\\QIO"]).is_err(), "缺少模式");
        assert!(parse(&["--check-only"]).is_err(), "缺少 --install-dir");
        assert!(
            parse(&["--check-only", "--close-installation", "--install-dir", "C:\\QIO"]).is_err(),
            "两个模式互斥"
        );
        assert!(
            parse(&["--check-only", "--install-dir", "C:\\QIO", "--allow-main-exe"]).is_err(),
            "--allow-main-exe 只对 close 有意义"
        );
        assert!(parse(&["--close-installation", "--install-dir", "C:\\QIO", "--timeout-ms", "0"]).is_err());
        assert!(
            parse(&["--close-installation", "--install-dir", "C:\\QIO", "--timeout-ms", "abc"]).is_err()
        );
        assert!(parse(&["--close-installation", "--install-dir", "C:\\QIO", "--nope"]).is_err());
    }
}
