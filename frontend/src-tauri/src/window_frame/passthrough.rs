//! poll 穿透：8ms 轮询光标，在可见圆角内容矩形之外让整窗（含 WebView2 子窗）真实点击穿透。
//!
//! 为什么是 poll 而不是 HTTRANSPARENT（0.3.2 spec §0.5 / spike 实测）：顶层窗口收不到
//! WM_NCHITTEST，HTTRANSPARENT 方案不可行；只能由壳在光标进入留白/圆角外时给窗口加
//! WS_EX_LAYERED|WS_EX_TRANSPARENT。注意：只加在顶层窗口上不够 —— WebView2 的子窗会
//! 独立参与命中，所以必须递归 2 级子窗同样加 WS_EX_TRANSPARENT。
//!
//! 安全边界：
//! * 只在 css 模式启用（默认 poll；QIO_PASSTHROUGH=none 关闭）；
//! * 窗口隐藏/最小化/尺寸拖动/转发期间一律恢复可交互；
//! * 退出时 stop_poll 只等一个 8ms 周期，不会卡住退出。

use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Duration;

use windows_sys::Win32::Foundation::{HWND, POINT};
use windows_sys::Win32::Graphics::Gdi::ClientToScreen;
use windows_sys::Win32::UI::Input::KeyboardAndMouse::ReleaseCapture;
use windows_sys::Win32::UI::WindowsAndMessaging::{
    GetClassNameW, GetCursorPos, GetWindow, GetWindowLongPtrW, IsIconic, IsWindowVisible,
    SetLayeredWindowAttributes, SetWindowLongPtrW, SetWindowPos, GWL_EXSTYLE, GW_CHILD,
    GW_HWNDNEXT, LWA_ALPHA, SWP_FRAMECHANGED, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE,
    SWP_NOZORDER, WS_EX_LAYERED, WS_EX_TRANSPARENT,
};

use super::config::PassthroughMode;
use super::overlay::OVERLAY_CLASS_NAME;
use super::state::{self, now_ms, WindowFrameState};

/// tao 自己的无边框缩放命中窗（不能把它设成穿透，否则缩放失效）。
const TAO_RESIZE_BORDERS_CLASS: &str = "TAURI_DRAG_RESIZE_BORDERS";
const POLL_INTERVAL: Duration = Duration::from_millis(8);

/// 切换穿透模式并启动/停止轮询线程。
pub fn apply_mode(state: &Arc<WindowFrameState>, mode: PassthroughMode) {
    let previous = state.passthrough();
    if previous != mode {
        state.set_passthrough(mode);
        log::info!(
            "[qio] window-frame passthrough 模式 {} -> {}",
            previous.as_str(),
            mode.as_str()
        );
    }
    if let Some(evidence) = state.evidence() {
        // 模式启动也记一条：验收要求 passthrough.jsonl 存在，而「没有切换」时也要可读。
        evidence.passthrough_toggle(&serde_json::json!({
            "ts": now_ms(),
            "transparent": false,
            "reason": format!("mode-start:{}", mode.as_str()),
            "exStyleBefore": null,
            "exStyleAfter": null,
            "webviewChildrenChanged": 0,
        }));
    }
    match mode {
        PassthroughMode::Poll => start_poll(state),
        PassthroughMode::None => {
            stop_poll(state);
            ensure_interactive(state, "mode-none");
        }
    }
}

/// 启动轮询线程（幂等）。
pub fn start_poll(state: &Arc<WindowFrameState>) {
    {
        let handle = state.poll_handle.lock().unwrap();
        if handle.is_some() && !state.poll_stop.load(Ordering::SeqCst) {
            return;
        }
    }
    state.poll_stop.store(false, Ordering::SeqCst);
    let state_thread = Arc::clone(state);
    let spawned = std::thread::Builder::new()
        .name("qio-window-passthrough-poll".to_string())
        .spawn(move || {
            log::info!(
                "[qio] window-frame poll 线程启动（{}ms 周期）",
                POLL_INTERVAL.as_millis()
            );
            while !state_thread.poll_stop.load(Ordering::SeqCst) {
                poll_tick(&state_thread);
                std::thread::sleep(POLL_INTERVAL);
            }
            set_transparent(&state_thread, false, "poll-thread-stop");
            log::info!("[qio] window-frame poll 线程已停止");
        });
    match spawned {
        Ok(handle) => {
            *state.poll_handle.lock().unwrap() = Some(handle);
        }
        Err(err) => {
            // 线程起不来只降级为 none（点击留在窗口，不穿透），不影响其他功能。
            state.set_passthrough(PassthroughMode::None);
            log::warn!("[qio] window-frame poll 线程启动失败（降级 none）：{err}");
        }
    }
}

/// 停止轮询并恢复可交互；join 最多等一个轮询周期（8ms），不阻塞退出。
pub fn stop_poll(state: &Arc<WindowFrameState>) {
    state.poll_stop.store(true, Ordering::SeqCst);
    set_transparent(state, false, "stop-poll");
    let handle = state.poll_handle.lock().unwrap().take();
    if let Some(handle) = handle {
        let _ = handle.join();
    }
}

/// 强制恢复可交互（隐藏/最小化/尺寸拖动/托盘恢复都调用；幂等）。
pub fn ensure_interactive(state: &Arc<WindowFrameState>, reason: &str) {
    set_transparent(state, false, reason);
    // capture 可能停在尺寸拖动中：释放一次，避免下次点击落在旧 capture 上。
    let _ = unsafe { ReleaseCapture() };
}

fn poll_tick(state: &Arc<WindowFrameState>) {
    let hwnd = state.hwnd();
    if hwnd == 0 {
        return;
    }
    let hwnd = hwnd as HWND;
    if state.forwarding.load(Ordering::SeqCst) {
        return;
    }
    if state.in_size_move.load(Ordering::SeqCst) {
        set_transparent(state, false, "size-move");
        return;
    }
    unsafe {
        if IsWindowVisible(hwnd) == 0 || IsIconic(hwnd) != 0 {
            set_transparent(state, false, "window-hidden");
            return;
        }
        let mut origin = POINT { x: 0, y: 0 };
        if ClientToScreen(hwnd, &mut origin) == 0 {
            return;
        }
        let mut cursor = POINT { x: 0, y: 0 };
        if GetCursorPos(&mut cursor) == 0 {
            return;
        }
        let geo = state::geometry(state);
        let regions = state.regions.lock().unwrap().clone();
        let client = state::client_rect(state.hwnd());
        let (padding, radius) = state::effective_padding_radius(state, &geo, &regions);
        let content = state::content_rect(client, padding);
        let local_x = (cursor.x - origin.x) as f64;
        let local_y = (cursor.y - origin.y) as f64;
        let interactive = content.contains_rounded(local_x, local_y, radius);
        set_transparent(state, !interactive, "cursor");
    }
}

fn set_transparent(state: &Arc<WindowFrameState>, want_transparent: bool, reason: &str) {
    let hwnd = state.hwnd();
    if hwnd == 0 {
        return;
    }
    let hwnd = hwnd as HWND;
    unsafe {
        let ex_style = GetWindowLongPtrW(hwnd, GWL_EXSTYLE);
        let current = (ex_style as u32) & (WS_EX_TRANSPARENT | WS_EX_LAYERED) != 0;
        state.poll_transparent.store(current, Ordering::SeqCst);
        if current == want_transparent {
            return;
        }
        let mut new_ex = ex_style;
        if want_transparent {
            new_ex |= (WS_EX_TRANSPARENT | WS_EX_LAYERED) as isize;
        } else {
            new_ex &= !((WS_EX_TRANSPARENT | WS_EX_LAYERED) as isize);
        }
        SetWindowLongPtrW(hwnd, GWL_EXSTYLE, new_ex);
        if want_transparent {
            // 保持合成层完全不透明；只改变命中行为（否则 layered 窗会整体消失）。
            SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA);
        }
        SetWindowPos(
            hwnd,
            std::ptr::null_mut(),
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        );
        let children_changed = set_webview_children_transparent(hwnd, want_transparent);
        state.poll_transparent.store(want_transparent, Ordering::SeqCst);
        log::debug!(
            "[qio] window-frame passthrough {} reason={} exStyle 0x{:08X} -> 0x{:08X} childrenChanged={}",
            if want_transparent {
                "ON (WS_EX_TRANSPARENT|WS_EX_LAYERED)"
            } else {
                "OFF (interactive)"
            },
            reason,
            ex_style as u32,
            new_ex as u32,
            children_changed
        );
        if let Some(evidence) = state.evidence() {
            evidence.passthrough_toggle(&serde_json::json!({
                "ts": now_ms(),
                "transparent": want_transparent,
                "reason": reason,
                "exStyleBefore": format!("0x{:08X}", ex_style as u32),
                "exStyleAfter": format!("0x{:08X}", new_ex as u32),
                "webviewChildrenChanged": children_changed,
            }));
        }
    }
}

/// 给 WebView2 子窗（2 级）加/去 WS_EX_TRANSPARENT：父窗透明不代表子窗命中透明。
fn set_webview_children_transparent(parent: HWND, want: bool) -> usize {
    let mut changed = 0usize;
    unsafe {
        let mut child = GetWindow(parent, GW_CHILD);
        while !child.is_null() {
            changed += apply_child_transparent(child, want);
            let mut grand = GetWindow(child, GW_CHILD);
            while !grand.is_null() {
                changed += apply_child_transparent(grand, want);
                grand = GetWindow(grand, GW_HWNDNEXT);
            }
            child = GetWindow(child, GW_HWNDNEXT);
        }
    }
    changed
}

unsafe fn apply_child_transparent(hwnd: HWND, want: bool) -> usize {
    let mut buffer = [0u16; 96];
    let len = unsafe { GetClassNameW(hwnd, buffer.as_mut_ptr(), buffer.len() as i32) };
    if len <= 0 {
        return 0;
    }
    let class_name = String::from_utf16_lossy(&buffer[..len as usize]);
    if class_name == OVERLAY_CLASS_NAME || class_name == TAO_RESIZE_BORDERS_CLASS {
        return 0;
    }
    let ex_style = unsafe { GetWindowLongPtrW(hwnd, GWL_EXSTYLE) };
    let has = (ex_style as u32) & WS_EX_TRANSPARENT != 0;
    if has == want {
        return 0;
    }
    let new_style = if want {
        ex_style | (WS_EX_TRANSPARENT as isize)
    } else {
        ex_style & !(WS_EX_TRANSPARENT as isize)
    };
    unsafe { SetWindowLongPtrW(hwnd, GWL_EXSTYLE, new_style) };
    1
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn poll_interval_is_eight_ms() {
        assert_eq!(POLL_INTERVAL, Duration::from_millis(8));
    }
}