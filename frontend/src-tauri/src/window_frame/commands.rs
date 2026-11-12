//! 窗口层 IPC 命令（spec §3.3）：qio_set_hit_regions / qio_get_window_frame /
//! qio_window_action / qio_set_passthrough。自定义命令不走 capability，无需新增权限。
//!
//! 所有几何参数/返回值均为**物理像素**；前端按 devicePixelRatio 换算 CSS px。

use std::sync::Arc;

use tauri::State;
use windows_sys::Win32::Foundation::HWND;
use windows_sys::Win32::UI::WindowsAndMessaging::{IsZoomed, SetForegroundWindow};

use super::config::PassthroughMode;
use super::state::{self, now_ms, HitRegions, WindowFrameState};

type FrameState<'a> = State<'a, Arc<WindowFrameState>>;

fn ok_json() -> serde_json::Value {
    serde_json::json!({ "ok": true })
}

#[tauri::command]
pub fn qio_set_hit_regions(state: FrameState<'_>, regions: HitRegions) -> serde_json::Value {
    let regions = regions.sanitize();
    if let Some(evidence) = state.evidence() {
        if let Ok(value) = serde_json::to_value(&regions) {
            evidence.hit_regions(&serde_json::json!({ "ts": now_ms(), "regions": value }));
        }
    }
    log::debug!(
        "[qio] window-frame qio_set_hit_regions dpr={:?} framePadding={:?} radius={:?} resizeBorder={:?} state={:?} titlebar={:?} maxButton={:?} otherButtons={} interactive={}",
        regions.dpr,
        regions.frame_padding,
        regions.radius,
        regions.resize_border,
        regions.state,
        regions.titlebar,
        regions.max_button,
        regions.other_buttons.len(),
        regions.interactive.len(),
    );
    *state.regions.lock().unwrap() = regions;
    super::overlay::request_sync(&state);
    state::refresh_and_emit(&state, false, "hit-regions-updated");
    ok_json()
}

#[tauri::command]
pub fn qio_get_window_frame(state: FrameState<'_>) -> serde_json::Value {
    serde_json::to_value(state::runtime_payload(&state)).unwrap_or_else(|_| ok_json())
}

#[tauri::command]
pub fn qio_window_action(state: FrameState<'_>, action: String) -> serde_json::Value {
    let Some(window) = state.window() else {
        return serde_json::json!({ "ok": false, "error": "window not ready" });
    };
    let result: Result<(), String> = match action.as_str() {
        "minimize" => window.minimize().map_err(|err| err.to_string()),
        "toggle_maximize" => toggle_maximize(&state, &window),
        "maximize" => window.maximize().map_err(|err| err.to_string()),
        "unmaximize" => window.unmaximize().map_err(|err| err.to_string()),
        "close" => window.close().map_err(|err| err.to_string()),
        "start_drag" => window.start_dragging().map_err(|err| err.to_string()),
        "set_foreground" => {
            let hwnd = state.hwnd();
            if hwnd == 0 {
                Err("no hwnd".to_string())
            } else {
                unsafe {
                    SetForegroundWindow(hwnd as HWND);
                }
                Ok(())
            }
        }
        other => Err(format!("unknown action: {other}")),
    };
    log::info!(
        "[qio] window-frame qio_window_action action={} ok={}",
        action,
        result.is_ok()
    );
    match result {
        Ok(()) => {
            let payload = state::refresh_and_emit(&state, true, "window-action");
            serde_json::json!({ "ok": true, "state": payload.state })
        }
        Err(err) => serde_json::json!({ "ok": false, "error": err }),
    }
}

fn toggle_maximize(
    state: &Arc<WindowFrameState>,
    window: &tauri::WebviewWindow,
) -> Result<(), String> {
    let hwnd = state.hwnd();
    let zoomed = hwnd != 0 && unsafe { IsZoomed(hwnd as HWND) } != 0;
    if zoomed {
        window.unmaximize().map_err(|err| err.to_string())
    } else {
        window.maximize().map_err(|err| err.to_string())
    }
}

#[tauri::command]
pub fn qio_set_passthrough(state: FrameState<'_>, mode: String) -> serde_json::Value {
    let parsed = match mode.trim().to_ascii_lowercase().as_str() {
        "none" => PassthroughMode::None,
        "poll" => PassthroughMode::Poll,
        other => {
            log::warn!("[qio] window-frame qio_set_passthrough 非法 mode={other:?}");
            return serde_json::json!({ "ok": false, "error": format!("invalid mode: {other}") });
        }
    };
    if !state.cfg.transparent() {
        log::warn!(
            "[qio] window-frame qio_set_passthrough 在 frame={} 模式下忽略（仅 css 生效）",
            state.cfg.frame.as_str()
        );
        return serde_json::json!({ "ok": false, "error": "passthrough only applies to css frame" });
    }
    super::passthrough::apply_mode(&state, parsed);
    state::refresh_and_emit(&state, true, "passthrough-mode");
    serde_json::json!({ "ok": true, "mode": parsed.as_str() })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn passthrough_mode_strings_are_stable() {
        assert_eq!(PassthroughMode::None.as_str(), "none");
        assert_eq!(PassthroughMode::Poll.as_str(), "poll");
    }
}