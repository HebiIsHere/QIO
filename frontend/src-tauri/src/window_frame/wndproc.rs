//! 主窗口 subclass：最小尺寸、DPI、几何/状态变化、尺寸移动与退出清理。
//!
//! 0.3.2 的命中测试由 overlay 子窗负责（见 overlay.rs）；这里**不**在顶层做命中判定，
//! 只保留一个计数 + 限速日志，用于验收证明「顶层 WM_NCHITTEST 在 WebView2 下不被派发」。
//!
//! 安装顺序：必须在 tao 的 subclass 之后安装（后装先调用），未处理的消息一律
//! DefSubclassProc 交回 tao/系统。

use std::sync::atomic::Ordering;
use std::sync::{Arc, OnceLock};

use windows_sys::Win32::Foundation::{HWND, LPARAM, LRESULT, POINT, WPARAM};
use windows_sys::Win32::UI::HiDpi::GetDpiForWindow;
use windows_sys::Win32::UI::Shell::{
    DefSubclassProc, RemoveWindowSubclass, SetWindowSubclass,
};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    MINMAXINFO, WM_DPICHANGED, WM_ENTERSIZEMOVE, WM_EXITSIZEMOVE, WM_GETMINMAXINFO, WM_NCDESTROY,
    WM_NCHITTEST, WM_SHOWWINDOW, WM_SIZE, WM_WINDOWPOSCHANGED,
};

use super::config::{CONTENT_MIN_CSS_HEIGHT, CONTENT_MIN_CSS_WIDTH, FRAME_PADDING_CSS};
use super::state::{self, WindowFrameState};

const SUBCLASS_ID: usize = 0x5149_4F46; // "QIOF"

static GLOBAL: OnceLock<Arc<WindowFrameState>> = OnceLock::new();

/// 安装主窗 subclass；返回是否成功（失败只降级：最小尺寸/DPI 联动不可用，不影响启动）。
pub fn install(state: &Arc<WindowFrameState>) -> bool {
    let _ = GLOBAL.set(Arc::clone(state));
    let hwnd = state.hwnd();
    if hwnd == 0 {
        log::warn!("[qio] window-frame wndproc: hwnd 尚未就绪，跳过安装");
        return false;
    }
    let ok = unsafe { SetWindowSubclass(hwnd as HWND, Some(subclass_proc), SUBCLASS_ID, 0) } != 0;
    state.inner.lock().unwrap().subclass_installed = ok;
    if ok {
        log::info!("[qio] window-frame wndproc: SetWindowSubclass 已安装 id=0x{SUBCLASS_ID:X}");
    } else {
        log::warn!(
            "[qio] window-frame wndproc: SetWindowSubclass 失败：{:?}",
            std::io::Error::last_os_error()
        );
    }
    ok
}

unsafe extern "system" fn subclass_proc(
    hwnd: HWND,
    msg: u32,
    wparam: WPARAM,
    lparam: LPARAM,
    _uidsubclass: usize,
    _dwrefdata: usize,
) -> LRESULT {
    if let Some(state) = GLOBAL.get() {
        if let Some(result) = handle(state, hwnd, msg, wparam, lparam) {
            return result;
        }
    }
    DefSubclassProc(hwnd, msg, wparam, lparam)
}

fn handle(
    state: &Arc<WindowFrameState>,
    hwnd: HWND,
    msg: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> Option<LRESULT> {
    match msg {
        WM_NCHITTEST => {
            // 只计数 + 限速日志：真实鼠标命中不会走到这里（WebView2 子窗覆盖客户区），
            // 验收时用 winprobe pointinfo 的 SendMessage 才会触发，属预期。
            let count = state.nchittest_count.fetch_add(1, Ordering::Relaxed) + 1;
            let x = (lparam as u32 & 0xFFFF) as i16 as i32;
            let y = ((lparam as u32 >> 16) & 0xFFFF) as i16 as i32;
            state.log_hit_decision(
                "DEFPROC",
                format!(
                    "top-level NCHITTEST count={count} screen=({x},{y}) — 真实输入不会派发到顶层，命中由 overlay 负责"
                ),
            );
            None
        }
        WM_GETMINMAXINFO => {
            apply_min_track_size(state, hwnd, lparam);
            // 由我们完全接管：不交给 tao，避免它用 builder 的逻辑最小尺寸覆盖物理换算结果。
            Some(0)
        }
        WM_WINDOWPOSCHANGED | WM_SIZE => {
            state::queue_refresh(state, "window-pos-or-size");
            super::overlay::request_sync(state);
            None
        }
        WM_DPICHANGED => {
            let new_dpi = (wparam as u32) & 0xFFFF;
            if new_dpi > 0 {
                state.inner.lock().unwrap().dpi = new_dpi;
                log::info!(
                    "[qio] window-frame DPI 变化 dpi={new_dpi} scale={:.2}",
                    new_dpi as f64 / 96.0
                );
            }
            state::queue_refresh(state, "dpi-changed");
            super::overlay::request_sync(state);
            None
        }
        WM_ENTERSIZEMOVE => {
            state.in_size_move.store(true, Ordering::SeqCst);
            super::passthrough::ensure_interactive(state, "enter-size-move");
            None
        }
        WM_EXITSIZEMOVE => {
            state.in_size_move.store(false, Ordering::SeqCst);
            state::queue_refresh(state, "exit-size-move");
            super::overlay::request_sync(state);
            None
        }
        WM_SHOWWINDOW => {
            if wparam == 0 {
                super::passthrough::ensure_interactive(state, "window-hide");
            }
            state::queue_refresh(state, "show-window");
            super::overlay::request_sync(state);
            None
        }
        WM_NCDESTROY => {
            // 退出路径也做过一次；这里幂等兜底（覆盖窗口被系统提前销毁的路径）。
            super::passthrough::stop_poll(state);
            super::overlay::destroy_all();
            unsafe {
                RemoveWindowSubclass(hwnd, Some(subclass_proc), SUBCLASS_ID);
            }
            log::info!("[qio] window-frame wndproc: WM_NCDESTROY，subclass 已卸载");
            None
        }
        _ => None,
    }
}

/// WM_GETMINMAXINFO：最小窗口 = 可见内容 800x600 CSS × dpr + 2×padding 物理。
///
/// * css 模式：padding 恒按 28 CSS 基线计算（即使当前最大化/贴墙 padding 显示为 0），
///   保证还原后的可见内容不小于 800×600；
/// * dwm/native：padding=0，minTrack = 800×600 × dpr。
fn apply_min_track_size(state: &Arc<WindowFrameState>, hwnd: HWND, lparam: LPARAM) {
    if lparam == 0 {
        return;
    }
    let dpi = unsafe { GetDpiForWindow(hwnd) }.max(96);
    let scale = dpi as f64 / 96.0;
    let padding_physical = if state.cfg.transparent() {
        FRAME_PADDING_CSS * scale
    } else {
        0.0
    };
    let min_w = (CONTENT_MIN_CSS_WIDTH * scale + padding_physical * 2.0).round() as i32;
    let min_h = (CONTENT_MIN_CSS_HEIGHT * scale + padding_physical * 2.0).round() as i32;
    let info = unsafe { &mut *(lparam as *mut MINMAXINFO) };
    info.ptMinTrackSize = POINT {
        x: min_w.max(1),
        y: min_h.max(1),
    };
    log::debug!(
        "[qio] window-frame WM_GETMINMAXINFO dpi={dpi} scale={scale:.2} padPx={padding_physical:.0} minTrack={min_w}x{min_h}"
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::window_frame::config::FrameMode;

    /// 纯计算部分与窗口解耦：抽出来验证 css/native 两种最小值规则。
    fn min_track(css: bool, dpi: u32) -> (i32, i32) {
        let scale = dpi as f64 / 96.0;
        let padding_physical = if css { FRAME_PADDING_CSS * scale } else { 0.0 };
        (
            (CONTENT_MIN_CSS_WIDTH * scale + padding_physical * 2.0).round() as i32,
            (CONTENT_MIN_CSS_HEIGHT * scale + padding_physical * 2.0).round() as i32,
        )
    }

    #[test]
    fn min_track_at_100_percent() {
        assert_eq!(min_track(true, 96), (856, 656));
        assert_eq!(min_track(false, 96), (800, 600));
    }

    #[test]
    fn min_track_at_200_percent_adds_padding_physically() {
        assert_eq!(min_track(true, 192), (1712, 1312));
        assert_eq!(min_track(false, 192), (1600, 1200));
    }

    #[test]
    fn min_track_rule_matches_frame_mode() {
        assert!(min_track(FrameMode::Css != FrameMode::Native, 120).0 > min_track(false, 120).0);
    }
}