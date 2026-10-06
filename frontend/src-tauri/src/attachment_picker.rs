//! 附件：点击「附件」时用**原生 Win32 选择器**拿真实路径。
//!
//! 为什么不加 tauri-plugin-dialog：本轮的验收约束是**不联网、不新增 crate**
//! （缓存里没有该插件）。这里只用已经在依赖里的 windows-sys 调 GetOpenFileNameW，
//! 拿到的就是真实路径 —— WebView 里 input[type=file] 给的 fakepath 一律不当路径用。
//!
//! 平台：只有 Windows 有原生实现；其他平台 pick_attachment_file_available 返回 false，
//! 前端据此退回「字节上传 / 粘贴路径」，而不是假装拿到了路径。
//!
//! 对话框必须在**主线程**打开（这是 Win32 的 UI 线程要求），所以命令走
//! AppHandle::run_on_main_thread，等待结果放到 blocking 线程池里，不占运行时工作线程。

/// 点击选择：返回真实路径；用户取消返回 None。
#[tauri::command]
pub async fn pick_attachment_file(app: tauri::AppHandle) -> Result<Option<String>, String> {
    let owner = owner_hwnd(&app);
    let (tx, rx) = std::sync::mpsc::channel::<Result<Option<String>, String>>();
    app.run_on_main_thread(move || {
        let _ = tx.send(pick_native(owner));
    })
    .map_err(|err| format!("无法在主线程打开文件选择器：{err}"))?;
    tauri::async_runtime::spawn_blocking(move || {
        rx.recv()
            .unwrap_or_else(|_| Err("文件选择器没有返回结果".to_string()))
    })
    .await
    .map_err(|err| format!("等待文件选择器结果失败：{err}"))?
}

/// 前端用它探测「有没有原生选择器」：false → 退回字节上传 / 粘贴路径。
#[tauri::command]
pub fn pick_attachment_file_available() -> bool {
    cfg!(windows)
}

/// 主窗口句柄（拿不到就用无主窗口的对话框：不阻断选择，只是不模态）。
fn owner_hwnd(app: &tauri::AppHandle) -> isize {
    #[cfg(windows)]
    {
        use tauri::Manager;
        if let Some(window) = app.get_webview_window("main") {
            if let Ok(hwnd) = window.hwnd() {
                // tauri 的 HWND 是 windows crate 的 newtype：取里面的裸指针再转 isize
                return hwnd.0 as isize;
            }
        }
        let _ = Manager::app_handle(app);
    }
    0
}

#[cfg(windows)]
fn pick_native(owner: isize) -> Result<Option<String>, String> {
    use std::ffi::c_void;
    // 老式通用对话框在 windows-sys 的 UI\Controls\Dialogs 下（不是 UI\Shell）
    use windows_sys::Win32::UI::Controls::Dialogs::{
        GetOpenFileNameW, OPENFILENAMEW, OFN_EXPLORER, OFN_FILEMUSTEXIST, OFN_NOCHANGEDIR,
        OFN_PATHMUSTEXIST,
    };

    // 过滤器是「双 NUL 结尾」的字符串对（显示名 \0 模式 \0 … \0\0）
    let filter: Vec<u16> = "所有文件\0*.*\0文本/代码 (*.txt;*.md;*.json;*.csv;*.log)\0*.txt;*.md;*.json;*.csv;*.log\0\0"
        .encode_utf16()
        .collect();
    let title: Vec<u16> = "选择要附加的文件"
        .encode_utf16()
        .chain(std::iter::once(0))
        .collect();
    // 路径缓冲：Windows 的 MAX_PATH 早就不是上限，给足空间（32K UTF-16 单元）
    let mut buffer = vec![0u16; 32_768];

    let mut ofn: OPENFILENAMEW = unsafe { std::mem::zeroed() };
    ofn.lStructSize = std::mem::size_of::<OPENFILENAMEW>() as u32;
    ofn.hwndOwner = owner as *mut c_void;
    ofn.lpstrFilter = filter.as_ptr();
    ofn.nFilterIndex = 1;
    ofn.lpstrFile = buffer.as_mut_ptr();
    ofn.nMaxFile = buffer.len() as u32;
    ofn.lpstrTitle = title.as_ptr();
    // NOCHANGEDIR：老式对话框会把进程当前目录改成用户选中的目录，那会影响后端相对路径
    ofn.Flags = OFN_EXPLORER | OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR;

    let ok = unsafe { GetOpenFileNameW(&mut ofn) };
    if ok == 0 {
        // 取消（或对话框打开失败）：返回 None。绝不编造一个路径。
        return Ok(None);
    }
    let end = buffer.iter().position(|unit| *unit == 0).unwrap_or(0);
    if end == 0 {
        return Ok(None);
    }
    Ok(Some(String::from_utf16_lossy(&buffer[..end])))
}

#[cfg(not(windows))]
fn pick_native(_owner: isize) -> Result<Option<String>, String> {
    Ok(None)
}
// ---------------------------------------------------------------------------
// 打开 / 在文件夹中显示（问题 5）
// ---------------------------------------------------------------------------

/// 打开一个附件：交给系统默认程序。
///
/// 安全边界（与前端同一套规则，这里是 fail-closed 的最后一道）：
/// * 只接受**绝对路径上的真实文件**（空路径 / 相对路径 / 目录 / 不存在一律拒绝）；
/// * 可执行 / 脚本类扩展名**不自动执行** —— 前端已改成「在文件夹中显示」，这里再挡一次，
///   保证即使前端漏判也不会因为一次点击把附件变成进程。
// shell:open 在 tauri-plugin-shell 2.1 之后被标记为 deprecated（建议换 tauri-plugin-opener）。
// 这里继续用它是有意的：共享契约（docs/plans/2026-10-06-audit-seven-fixes.md §1.6）就写的是
// 「Tauri 命令 + shell:open」，而新增 opener 插件意味着在这个离线环境里再加一个 crate —— 不划算。
#[allow(deprecated)]
#[tauri::command]
pub async fn open_attachment_path(app: tauri::AppHandle, path: String) -> Result<(), String> {
    use tauri_plugin_shell::ShellExt;

    let target = checked_file(&path)?;
    if is_executable_file(&target) {
        return Err(
            "这个文件是可执行 / 脚本类：QIO 不自动运行附件。请用「在文件夹中显示」，确认来源后再自行打开"
                .to_string(),
        );
    }
    app.shell()
        .open(
            target.to_string_lossy().to_string(),
            None::<tauri_plugin_shell::open::Program>,
        )
        .map_err(|err| format!("交给系统默认程序失败：{err}"))
}

/// 在文件管理器中显示这个附件（可执行 / 脚本类附件只提供这个入口）。
#[tauri::command]
pub fn reveal_attachment_path(path: String) -> Result<(), String> {
    let target = checked_file(&path)?;
    reveal_in_file_manager(&target)
}

/// 只接受绝对路径上的真实文件：不猜、不补全、不打开目录。
fn checked_file(path: &str) -> Result<std::path::PathBuf, String> {
    let raw = path.trim().trim_matches('"');
    if raw.is_empty() {
        return Err("没有可打开的路径".to_string());
    }
    let candidate = std::path::PathBuf::from(raw);
    if !candidate.is_absolute() {
        return Err("只接受绝对路径".to_string());
    }
    let metadata = std::fs::metadata(&candidate)
        .map_err(|err| format!("打不开这个位置（{err}）"))?;
    if !metadata.is_file() {
        return Err("这个位置不是文件".to_string());
    }
    Ok(candidate)
}

/// 可执行 / 脚本类扩展名（只影响「自动打开」，不影响「在文件夹中显示」）。
fn is_executable_file(path: &std::path::Path) -> bool {
    const EXECUTABLE_EXTS: &[&str] = &[
        "exe", "com", "scr", "msi", "msix", "appx", "bat", "cmd", "ps1", "psm1", "vbs", "vbe",
        "js", "jse", "ws", "wsf", "wsh", "jar", "lnk", "reg", "sh", "bash", "zsh", "py", "pyw",
        "pl", "rb", "dll", "so", "dylib", "apk", "deb", "rpm", "run", "cpl", "hta", "inf",
    ];
    match path.extension().and_then(|ext| ext.to_str()) {
        Some(ext) => EXECUTABLE_EXTS
            .iter()
            .any(|known| ext.eq_ignore_ascii_case(known)),
        None => false,
    }
}

#[cfg(windows)]
fn reveal_in_file_manager(path: &std::path::Path) -> Result<(), String> {
    // explorer 的 /select 整体是一个参数（路径带空格也一样）：这里是参数数组，不拼命令行
    std::process::Command::new("explorer.exe")
        .arg(format!("/select,{}", path.display()))
        .spawn()
        .map(|_| ())
        .map_err(|err| format!("无法在文件管理器中显示：{err}"))
}

#[cfg(not(windows))]
fn reveal_in_file_manager(path: &std::path::Path) -> Result<(), String> {
    let dir = path.parent().unwrap_or(path);
    std::process::Command::new("xdg-open")
        .arg(dir)
        .spawn()
        .map(|_| ())
        .map_err(|err| format!("无法打开所在目录：{err}"))
}
