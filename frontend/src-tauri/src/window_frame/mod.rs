//! QIO 0.3.2 窗口层：程序化建窗 + DWM 三模式 + overlay 命中 + poll 穿透 + 状态通道。
//!
//! 规格：docs/.qio032-part1.md（结论摘要）、part2（spike 逐函数映射）、part3（IPC 契约）。

pub mod commands;
pub mod config;
pub mod outline;
pub mod overlay;
pub mod passthrough;
pub mod state;
pub mod wndproc;

use std::path::PathBuf;
use std::sync::atomic::Ordering;
use std::sync::Arc;

use tauri::webview::PageLoadEvent;
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

use self::config::{FrameConfig, FrameMode};
use self::state::{Evidence, WindowFrameState};

const INITIAL_WIDTH: f64 = 1200.0;
const INITIAL_HEIGHT: f64 = 800.0;
const SHOW_FALLBACK_DELAY: std::time::Duration = std::time::Duration::from_secs(2);

pub fn create_main_window(
    app: &AppHandle,
    requested: FrameConfig,
    evidence_dir: Option<PathBuf>,
) -> tauri::Result<(Arc<WindowFrameState>, WebviewWindow)> {
    // 证据目录必须在建窗前创建：css 初始化失败也必须有 init.jsonl 失败记录，
    // 验收脚本只读文件判定，不依赖 IPC。
    let evidence = match evidence_dir {
        Some(dir) => match Evidence::new(dir) {
            Ok(evidence) => Some(evidence),
            Err(err) => {
                log::warn!("[qio] window-frame 证据目录创建失败（不影响启动）：{err}");
                None
            }
        },
        None => None,
    };
    let mut effective = requested.clone();
    let mut css_failure: Option<String> = None;
    if effective.frame == FrameMode::Css {
        if effective.test_fail_css {
            css_failure = Some("QIO_WINDOW_TEST_FAIL_CSS=1".to_string());
        } else if !dwm_composition_enabled() {
            css_failure = Some("dwm-composition-disabled".to_string());
        }
        if let Some(reason) = css_failure.clone() {
            log::error!("[qio] window-frame css-init-failed reason={reason}（回退 native）");
            effective = effective.fallback_to_native(&reason);
        }
    }

    let (effective, window) = match build_window(app, &effective) {
        Ok(window) => (effective, window),
        Err(err) if effective.frame == FrameMode::Css => {
            let reason = format!("builder-create-failed: {err}");
            log::error!("[qio] window-frame css-init-failed reason={reason}（回退 native）");
            let fallback = effective.fallback_to_native(&reason);
            css_failure = Some(reason);
            match build_window(app, &fallback) {
                Ok(window) => (fallback, window),
                Err(err) => {
                    if let Some(evidence) = &evidence {
                        evidence.init(&serde_json::json!({
                            "ts": state::now_ms(),
                            "phase": "create-failed",
                            "requestedFrame": requested.frame.as_str(),
                            "build": requested.windows_build,
                            "cssFailure": css_failure,
                            "error": err.to_string(),
                        }));
                    }
                    return Err(err);
                }
            }
        }
        Err(err) => {
            if let Some(evidence) = &evidence {
                evidence.init(&serde_json::json!({
                    "ts": state::now_ms(),
                    "phase": "create-failed",
                    "requestedFrame": requested.frame.as_str(),
                    "build": requested.windows_build,
                    "error": err.to_string(),
                }));
            }
            return Err(err);
        }
    };

    let state = Arc::new(WindowFrameState::new(
        effective.clone(),
        app.clone(),
        evidence,
    ));
    app.manage(Arc::clone(&state));
    let dpi = window_dpi(&window).unwrap_or(96);
    state.set_window(window.clone(), dpi);

    let summary = effective.summary(dpi);
    log::info!("[qio] window-frame 建窗完成 {summary}");
    if let Some(reason) = effective.forced_native_reason.as_deref() {
        log::warn!("[qio] window-frame 生效模式被强制为 native：{reason}");
    }
    write_init(
        &state,
        serde_json::json!({
            "phase": "create",
            "frame": effective.frame.as_str(),
            "requestedFrame": requested.frame.as_str(),
            "build": effective.windows_build,
            "dpi": dpi,
            "summary": summary,
            "forcedNative": effective.forced_native_reason,
            "cssFailure": css_failure,
            "decorations": effective.decorations(),
            "transparent": effective.transparent(),
            "shadow": effective.shadow(),
            "passthrough": effective.passthrough.as_str(),
            "initialSize": [INITIAL_WIDTH, INITIAL_HEIGHT],
            "minContentCss": [config::CONTENT_MIN_CSS_WIDTH, config::CONTENT_MIN_CSS_HEIGHT],
            "qioWindowFrameEnv": std::env::var("QIO_WINDOW_FRAME").ok(),
            "qioPassthroughEnv": std::env::var("QIO_PASSTHROUGH").ok(),
            "testFailCss": effective.test_fail_css,
            "title": "QIO",
            "overlayClass": overlay::OVERLAY_CLASS_NAME,
        }),
    );
    Ok((state, window))
}

/// init.jsonl 写入（自动补 ts）。
fn write_init(state: &Arc<WindowFrameState>, mut value: serde_json::Value) {
    if let Some(evidence) = state.evidence() {
        if let Some(object) = value.as_object_mut() {
            object.insert("ts".to_string(), serde_json::json!(state::now_ms()));
        }
        evidence.init(&value);
    }
}

pub fn init(app: &AppHandle, state: &Arc<WindowFrameState>, window: &WebviewWindow) {
    let outline_results = outline::apply(window, &state.cfg);
    let subclass_ok = wndproc::install(state);
    log::info!(
        "[qio] window-frame wndproc install ok={} hwnd=0x{:X}",
        subclass_ok,
        state.hwnd()
    );
    let overlay_enabled = overlay::enabled_for(&state.cfg);
    if overlay_enabled {
        overlay::ensure(state);
        seed_test_regions_if_any(state);
        overlay::request_sync(state);
    } else {
        log::info!(
            "[qio] window-frame overlay disabled（frame={}，spec §4.7：只有 css 模式创建 overlay）",
            state.cfg.frame.as_str()
        );
    }
    passthrough::apply_mode(state, state.cfg.passthrough);
    state::refresh_and_emit(state, true, "startup");
    spawn_show_fallback(app.clone());

    let outline_json: Vec<serde_json::Value> = outline_results
        .iter()
        .map(|attr| {
            serde_json::json!({
                "name": attr.name,
                "value": attr.value,
                "hresult": attr.hresult_text(),
                "readback": attr.readback,
                "readbackHresult": attr.readback_hresult,
            })
        })
        .collect();
    let test_regions = std::env::var("QIO_WINDOW_TEST_REGIONS")
        .map(|v| !v.trim().is_empty())
        .unwrap_or(false);
    write_init(
        state,
        serde_json::json!({
            "phase": "init",
            "frame": state.cfg.frame.as_str(),
            "dpi": state.dpi(),
            "hwnd": format!("0x{:X}", state.hwnd()),
            "overlayClass": overlay::OVERLAY_CLASS_NAME,
            "steps": {
                "outline": outline_json,
                "wndproc": subclass_ok,
                "overlayEnsure": overlay_enabled,
                "overlayCount": overlay::active_count(),
                "pollMode": state.passthrough().as_str(),
                "testRegionsInjected": test_regions && overlay_enabled,
            },
            "ok": true,
        }),
    );
    log::info!(
        "[qio] window-frame 初始化完成 frame={} passthrough={} hwnd=0x{:X} dpi={}",
        state.cfg.frame.as_str(),
        state.passthrough().as_str(),
        state.hwnd(),
        state.dpi()
    );
}

pub fn on_page_ready(app: &AppHandle, window: &WebviewWindow) {
    let Some(state) = app.try_state::<Arc<WindowFrameState>>() else {
        log::warn!("[qio] window-frame 页面就绪时状态尚未注册，忽略本次显示握手");
        return;
    };
    if state.page_ready.swap(true, Ordering::SeqCst) {
        return;
    }
    log::info!("[qio] window-frame 页面加载完成（PageLoadEvent::Finished）");
    state::refresh_and_emit(&state, true, "page-ready");
    if state.cfg.show_on_load {
        show_main_window_frame(window, &state, "page-ready");
    }
    state::emit_frame_ready(&state);
    overlay::request_sync(&state);
}

pub fn show_main_window_frame(
    window: &WebviewWindow,
    state: &Arc<WindowFrameState>,
    reason: &'static str,
) {
    let first_show = !state.shown.swap(true, Ordering::SeqCst);
    let _ = window.unminimize();
    let mut show_ok = true;
    match window.show() {
        Ok(()) => log::info!("[qio] window-frame 显示窗口（reason={reason} firstShow={first_show}）"),
        Err(err) => {
            show_ok = false;
            log::warn!("[qio] window-frame 显示窗口失败（reason={reason}）：{err}");
        }
    }
    let _ = window.set_focus();
    if first_show {
        write_init(
            state,
            serde_json::json!({
                "phase": "shown",
                "reason": reason,
                "ok": show_ok,
                "dpi": state.dpi(),
            }),
        );
    }
    state::queue_refresh(state, "window-shown");
    overlay::request_sync(state);
}

pub fn on_window_hidden(state: &Arc<WindowFrameState>, reason: &'static str) {
    log::info!("[qio] window-frame 窗口隐藏（{reason}）：恢复可交互并暂停穿透");
    passthrough::ensure_interactive(state, reason);
    state::queue_refresh(state, "window-hide");
}

pub fn on_window_shown(state: &Arc<WindowFrameState>, reason: &'static str) {
    state.shown.store(true, Ordering::SeqCst);
    passthrough::ensure_interactive(state, reason);
    overlay::request_sync(state);
    state::queue_refresh(state, "window-show");
}

pub fn shutdown(app: &AppHandle) {
    let Some(state) = app.try_state::<Arc<WindowFrameState>>() else {
        return;
    };
    // RunEvent::ExitRequested 与 RunEvent::Exit 会各来一次：清理必须幂等，日志只留一条。
    if state.shutdown_done.swap(true, Ordering::SeqCst) {
        return;
    }
    log::info!(
        "[qio] window-frame 退出清理：top-level NCHITTEST count={}（0=WebView2 子窗吃掉命中）",
        state.nchittest_count.load(Ordering::Relaxed)
    );
    passthrough::stop_poll(&state);
    overlay::destroy_all();
}

fn build_window(app: &AppHandle, cfg: &FrameConfig) -> tauri::Result<WebviewWindow> {
    let url = match cfg.ui_query.as_deref() {
        Some(query) => {
            let query = query.trim_start_matches('?');
            if query.is_empty() {
                WebviewUrl::App("index.html".into())
            } else {
                WebviewUrl::App(format!("index.html?{query}").into())
            }
        }
        None => WebviewUrl::App("index.html".into()),
    };

    let mut builder = WebviewWindowBuilder::new(app, "main", url)
        .title("QIO")
        .inner_size(INITIAL_WIDTH, INITIAL_HEIGHT)
        .min_inner_size(
            config::CONTENT_MIN_CSS_WIDTH,
            config::CONTENT_MIN_CSS_HEIGHT,
        )
        .decorations(cfg.decorations())
        .transparent(cfg.transparent())
        .shadow(cfg.shadow())
        .visible(false);

    if !cfg.transparent() {
        builder = builder.background_color(tauri::webview::Color(23, 16, 21, 255));
    }

    let page_app = app.clone();
    builder = builder.on_page_load(move |window, payload| {
        if matches!(payload.event(), PageLoadEvent::Finished) {
            on_page_ready(&page_app, &window);
        }
    });
    builder.build()
}

fn spawn_show_fallback(app: AppHandle) {
    std::thread::Builder::new()
        .name("qio-window-show-fallback".to_string())
        .spawn(move || {
            std::thread::sleep(SHOW_FALLBACK_DELAY);
            let Some(state) = app.try_state::<Arc<WindowFrameState>>() else {
                return;
            };
            if state.shown.load(Ordering::SeqCst) {
                return;
            }
            if let Some(window) = state.window() {
                log::warn!(
                    "[qio] window-frame 页面 2s 内未完成加载，兜底显示窗口（避免用户看到双击没反应）"
                );
                show_main_window_frame(&window, &state, "show-fallback");
            }
        })
        .ok();
}

fn window_dpi(window: &WebviewWindow) -> Option<u32> {
    #[cfg(windows)]
    {
        use windows_sys::Win32::Foundation::HWND;
        use windows_sys::Win32::UI::HiDpi::GetDpiForWindow;
        let hwnd = window.hwnd().ok()?.0 as HWND;
        let dpi = unsafe { GetDpiForWindow(hwnd) };
        if dpi > 0 {
            return Some(dpi);
        }
    }
    let _ = window;
    None
}

fn dwm_composition_enabled() -> bool {
    #[cfg(windows)]
    {
        use windows_sys::core::BOOL;
        use windows_sys::Win32::Graphics::Dwm::DwmIsCompositionEnabled;
        let mut enabled: BOOL = 0;
        let hr = unsafe { DwmIsCompositionEnabled(&mut enabled) };
        if hr < 0 {
            log::warn!("[qio] window-frame DwmIsCompositionEnabled 失败（hr=0x{hr:08X}），按关闭处理");
            return false;
        }
        return enabled != 0;
    }
    #[cfg(not(windows))]
    {
        true
    }
}
/// 诊断/测试钩子：QIO_WINDOW_TEST_REGIONS=<JSON> 直接注入一份 hit regions。
///
/// 仅用于「壳已编译、前端还没接 window frame 上报」时独立验证 overlay（T5 harness 冒烟）；
/// 产品运行不会设置该变量。JSON 形状见证据目录 README-window-frame-probe.md §4。
fn seed_test_regions_if_any(state: &Arc<WindowFrameState>) {
    let Ok(raw) = std::env::var("QIO_WINDOW_TEST_REGIONS") else {
        return;
    };
    if raw.trim().is_empty() {
        return;
    }
    match serde_json::from_str::<state::HitRegions>(&raw) {
        Ok(regions) => {
            let regions = regions.sanitize();
            if let Some(evidence) = state.evidence() {
                if let Ok(value) = serde_json::to_value(&regions) {
                    evidence.hit_regions(&serde_json::json!({
                        "ts": state::now_ms(),
                        "source": "QIO_WINDOW_TEST_REGIONS",
                        "regions": value,
                    }));
                }
            }
            *state.regions.lock().unwrap() = regions;
            log::warn!(
                "[qio] window-frame 使用 QIO_WINDOW_TEST_REGIONS 注入了测试命中布局（仅诊断用）"
            );
            state::refresh_and_emit(state, true, "test-regions");
        }
        Err(err) => log::warn!("[qio] window-frame QIO_WINDOW_TEST_REGIONS 解析失败：{err}"),
    }
}

/// 验收/诊断证据目录：QIO_WINDOW_EVIDENCE=1 时写到 <QIO_DATA_DIR>/logs/window-frame。
///
/// 产品常态不置该变量（不写盘）；验收脚本与 T5 用它收集窗口层证据。
pub fn evidence_dir_from_env(data_dir: &std::path::Path) -> Option<PathBuf> {
    let enabled = std::env::var("QIO_WINDOW_EVIDENCE")
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);
    if !enabled {
        return None;
    }
    Some(data_dir.join("logs").join("window-frame"))
}