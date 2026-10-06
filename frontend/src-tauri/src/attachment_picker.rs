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
