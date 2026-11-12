//! DWM 窗口外轮廓属性：三模式分流 + 每个属性的 HRESULT 日志。
//!
//! 为什么必须在壳侧做：窗口四角是 DWM 合成器裁切的，网页层给容器加 border-radius
//! 只会得到「方形窗口里再画一个圆角矩形」。0.3.2 的 css 模式把圆角交给网页 CSS，
//! 所以必须显式关掉系统圆角（DWMWCP_DONOTROUND），否则系统再裁一刀会与 CSS 圆角错位。
//!
//! 规格：docs/.qio032-part1.md §0.2、docs/.qio032-part2.md §2.3。

use tauri::WebviewWindow;

use super::config::{FrameConfig, FrameMode};

/// 单条 DWM 属性设置结果（日志/诊断用），附带设置后的读回值。
#[derive(Clone, Debug)]
pub struct DwmAttrResult {
    pub name: &'static str,
    pub value: i64,
    pub hresult: i32,
    /// DwmSetWindowAttribute 之后立即 DwmGetWindowAttribute 的读回值（None=读回失败）。
    pub readback: Option<i64>,
    /// 读回的 HRESULT。
    pub readback_hresult: i32,
}

impl DwmAttrResult {
    pub fn hresult_text(&self) -> String {
        format_hresult(self.hresult)
    }
}

pub fn format_hresult(hr: i32) -> String {
    if hr == 0 {
        "S_OK(0x00000000)".to_string()
    } else if hr > 0 {
        format!("S_FALSE_OR_SUCCESS(0x{:08X})", hr as u32)
    } else {
        format!("FAILED(0x{:08X})", hr as u32)
    }
}

/// 按模式应用 DWM 属性；返回实际调用的属性列表（native 模式为空）。
///
/// * css：DONOTROUND + BORDER_COLOR_NONE（圆角由网页 CSS 画）；
/// * dwm：ROUND + BORDER_COLOR_NONE（0.3.1 的圆角 + 补上缺失的去边框）；
/// * native：不设置，交给系统窗框。
pub fn apply(window: &WebviewWindow, cfg: &FrameConfig) -> Vec<DwmAttrResult> {
    let mut results = Vec::new();
    if !cfg.frameless() {
        log::info!(
            "[qio] window-frame outline: native 模式，保留系统窗框与系统圆角（不调用 DwmSetWindowAttribute）"
        );
        return results;
    }
    let Ok(raw) = window.hwnd() else {
        log::warn!("[qio] window-frame outline: 拿不到 HWND，跳过 DWM 属性（不影响启动）");
        return results;
    };
    #[cfg(windows)]
    {
        use std::ffi::c_void;
        use windows_sys::Win32::Foundation::HWND;
        use windows_sys::Win32::Graphics::Dwm::{
            DwmGetWindowAttribute, DwmSetWindowAttribute, DWMWA_BORDER_COLOR, DWMWA_COLOR_NONE,
            DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_DONOTROUND, DWMWCP_ROUND,
        };

        let hwnd = raw.0 as HWND;
        let (corner_value, corner_name) = match cfg.frame {
            FrameMode::Dwm => (DWMWCP_ROUND, "DWMWCP_ROUND"),
            _ => (DWMWCP_DONOTROUND, "DWMWCP_DONOTROUND"),
        };
        let corner: i32 = corner_value;
        let hr = unsafe {
            DwmSetWindowAttribute(
                hwnd,
                DWMWA_WINDOW_CORNER_PREFERENCE as u32,
                &corner as *const i32 as *const c_void,
                std::mem::size_of::<i32>() as u32,
            )
        };
        let mut corner_read: i32 = -1;
        let read_hr = unsafe {
            DwmGetWindowAttribute(
                hwnd,
                DWMWA_WINDOW_CORNER_PREFERENCE as u32,
                &mut corner_read as *mut i32 as *mut c_void,
                std::mem::size_of::<i32>() as u32,
            )
        };
        results.push(DwmAttrResult {
            name: "DWMWA_WINDOW_CORNER_PREFERENCE",
            value: corner as i64,
            hresult: hr,
            readback: if read_hr >= 0 { Some(corner_read as i64) } else { None },
            readback_hresult: read_hr,
        });
        log_dwm("DWMWA_WINDOW_CORNER_PREFERENCE", corner_name, hr);

        let border: u32 = DWMWA_COLOR_NONE;
        let hr = unsafe {
            DwmSetWindowAttribute(
                hwnd,
                DWMWA_BORDER_COLOR as u32,
                &border as *const u32 as *const c_void,
                std::mem::size_of::<u32>() as u32,
            )
        };
        let mut border_read: u32 = 0;
        let read_hr = unsafe {
            DwmGetWindowAttribute(
                hwnd,
                DWMWA_BORDER_COLOR as u32,
                &mut border_read as *mut u32 as *mut c_void,
                std::mem::size_of::<u32>() as u32,
            )
        };
        results.push(DwmAttrResult {
            name: "DWMWA_BORDER_COLOR",
            value: border as i64,
            hresult: hr,
            readback: if read_hr >= 0 { Some(border_read as i64) } else { None },
            readback_hresult: read_hr,
        });
        log_dwm_readback("DWMWA_BORDER_COLOR", read_hr, if read_hr >= 0 { Some(border_read as i64) } else { None });
        log_dwm("DWMWA_BORDER_COLOR", "DWMWA_COLOR_NONE", hr);
    }
    #[cfg(not(windows))]
    {
        let _ = raw;
    }
    results
}

#[cfg(windows)]
fn log_dwm_readback(attribute: &str, read_hr: i32, readback: Option<i64>) {
    if read_hr < 0 {
        log::debug!(
            "[qio] window-frame DWM {attribute} 读回不可用：hr={}",
            format_hresult(read_hr)
        );
    } else if let Some(value) = readback {
        log::info!(
            "[qio] window-frame DWM {attribute} 读回=0x{value:08X} hr={}",
            format_hresult(read_hr)
        );
    }
}

#[cfg(windows)]
fn log_dwm(attribute: &str, value_name: &str, hr: i32) {
    // 失败只 debug（沿用 0.3.1 语义：DWM 不可用不影响启动）；成功留 info，验收可复核。
    if hr < 0 {
        log::debug!(
            "[qio] window-frame DWM {attribute}={value_name} 不可用（需要 Windows 11 22000+）：hr={}",
            format_hresult(hr)
        );
    } else {
        log::info!(
            "[qio] window-frame DWM {attribute}={value_name} hr={}",
            format_hresult(hr)
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hresult_formatting_is_stable() {
        assert_eq!(format_hresult(0), "S_OK(0x00000000)");
        assert_eq!(format_hresult(1), "S_FALSE_OR_SUCCESS(0x00000001)");
        assert_eq!(format_hresult(-2147467259), "FAILED(0x80004005)");
    }
}