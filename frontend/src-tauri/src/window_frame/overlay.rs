//! 原生透明 overlay 子窗：把「真实非客户区命中」从 WebView2 手里抢回来。
//!
//! 为什么需要它（2026-11-12 spike 真机实测 + 0.3.2 spec §2.4）：页面在 WebView2 子窗里、
//! 子窗覆盖整个客户区，顶层窗口**永远收不到** WM_NCHITTEST。所以 max 按钮（HTMAXBUTTON，
//! Win11 Snap Layouts 的系统级入口）、标题带拖动（HTCAPTION）、八方向缩放（HTLEFT..）都
//! 只能通过在目标区域叠一个「永不绘制」的原生子窗来实现。
//!
//! 样式约束（社区 5 个插件收敛出的同一套）：
//! * WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS，**无扩展样式**（WS_EX_LAYERED 会丢命中，
//!   WS_EX_TRANSPARENT 会让窗口命中穿透，二者都让方案失效）；
//! * 背景刷 NULL_BRUSH、WM_PAINT 空实现 —— 不绘制才能让下面的 WebView2 内容透出来；
//! * 创建/布局必须在 tauri 主线程（子窗口只能由父窗口所属线程创建）。
//!
//! 交互约定（spec §2.4/§3.4/§3.5）：
//! * max 按钮：按下 SetCapture、抬起 toggle（Rust 侧唯一触发，DOM 收不到该矩形）；
//! * 标题带：拖拽区 − maxButton − min/close − interactive（设置/权限/异常/互动挂载点）；
//! * resize：可见内容边**内侧** 8 物理 px 的 8 条 strip；
//! * press 期间 defer 重排；每次 sync 重新 HWND_TOP（WebView2 会把自己提到 overlay 之上）。

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use tauri::Emitter;
use windows_sys::Win32::Foundation::{HWND, LPARAM, LRESULT, POINT, WPARAM};
use windows_sys::Win32::Graphics::Gdi::{
    BeginPaint, EndPaint, GetStockObject, HBRUSH, NULL_BRUSH, PAINTSTRUCT,
};
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
    GetDoubleClickTime, ReleaseCapture, SetCapture, TrackMouseEvent, TME_LEAVE, TME_NONCLIENT,
    TRACKMOUSEEVENT,
};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DefWindowProcW, DestroyWindow, GetCursorPos, GetWindowLongPtrW, LoadCursorW,
    PostMessageW, RegisterClassExW, SetCursor, SetWindowLongPtrW, SetWindowPos, ShowWindow,
    GWLP_USERDATA, HWND_TOP, IDC_ARROW, IDC_SIZENESW, IDC_SIZENS, IDC_SIZENWSE, IDC_SIZEWE,
    SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW, SW_HIDE, WM_ERASEBKGND,
    WM_LBUTTONDBLCLK, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEMOVE, WM_NCDESTROY, WM_NCHITTEST,
    WM_NCLBUTTONDBLCLK, WM_NCLBUTTONDOWN, WM_NCLBUTTONUP, WM_NCMOUSELEAVE, WM_NCMOUSEMOVE,
    WM_PAINT, WM_SETCURSOR, WNDCLASSEXW, WS_CHILD, WS_CLIPSIBLINGS, WS_VISIBLE,
};

use super::config::{FrameMode, RESIZE_BORDER_PHYSICAL};
use super::state::{
    self, now_ms, RegionRect, WindowFrameState, EVENT_CAPTION_HOVER,
};

/// overlay 子窗类名（spec 定稿：QioWindowOverlay；passthrough 的子窗过滤用同一个常量）。
pub const OVERLAY_CLASS_NAME: &str = "QioWindowOverlay";

// windows-sys 0.61 把 WM_MOUSELEAVE 放在 Win32_UI_Controls（未开该 feature），值稳定。
const WM_MOUSELEAVE_MSG: u32 = 0x02A3;
const WM_CAPTURECHANGED_MSG: u32 = 0x0215;
const WM_MOUSEACTIVATE_MSG: u32 = 0x0021;

const KIND_HIDDEN: isize = 0;
const KIND_MAX: isize = 1;
const KIND_TITLEBAR: isize = 2;
const KIND_RESIZE_BASE: isize = 0x100;

static OVERLAY_STATE: OnceLock<Arc<WindowFrameState>> = OnceLock::new();
static CLASS_REGISTERED: OnceLock<bool> = OnceLock::new();
static CLASS_WIDE: OnceLock<Vec<u16>> = OnceLock::new();
static SLOTS: OnceLock<Mutex<Vec<OverlaySlot>>> = OnceLock::new();
static MAX_PRESSED: AtomicBool = AtomicBool::new(false);
static MAX_HOVER: AtomicBool = AtomicBool::new(false);
static LAST_LAYOUT: OnceLock<Mutex<Option<String>>> = OnceLock::new();
static LAST_MAX_RECT: OnceLock<Mutex<Option<RegionRect>>> = OnceLock::new();
static TB_PRESSED: AtomicBool = AtomicBool::new(false);
static TB_DRAGGING: AtomicBool = AtomicBool::new(false);
static TB_FORWARDED: AtomicBool = AtomicBool::new(false);
static TB_PRESS_POINT: OnceLock<Mutex<(i32, i32)>> = OnceLock::new();
static TB_LAST_CLICK: OnceLock<Mutex<Option<(i32, i32, std::time::Instant)>>> = OnceLock::new();

#[derive(Clone, Copy, Debug)]
struct OverlaySlot {
    hwnd: isize,
    kind: isize,
    rect: RegionRect,
}

fn class_wide() -> &'static [u16] {
    CLASS_WIDE.get_or_init(|| {
        OVERLAY_CLASS_NAME
            .encode_utf16()
            .chain(std::iter::once(0))
            .collect()
    })
}

fn slots() -> &'static Mutex<Vec<OverlaySlot>> {
    SLOTS.get_or_init(|| Mutex::new(Vec::new()))
}

fn last_layout() -> &'static Mutex<Option<String>> {
    LAST_LAYOUT.get_or_init(|| Mutex::new(None))
}

fn last_max_rect() -> &'static Mutex<Option<RegionRect>> {
    LAST_MAX_RECT.get_or_init(|| Mutex::new(None))
}

fn tb_press_point() -> &'static Mutex<(i32, i32)> {
    TB_PRESS_POINT.get_or_init(|| Mutex::new((0, 0)))
}

fn tb_last_click() -> &'static Mutex<Option<(i32, i32, std::time::Instant)>> {
    TB_LAST_CLICK.get_or_init(|| Mutex::new(None))
}

fn ht_name(code: i32) -> &'static str {
    match code {
        2 => "HTCAPTION",
        9 => "HTMAXBUTTON",
        10 => "HTLEFT",
        11 => "HTRIGHT",
        12 => "HTTOP",
        13 => "HTTOPLEFT",
        14 => "HTTOPRIGHT",
        15 => "HTBOTTOM",
        16 => "HTBOTTOMLEFT",
        17 => "HTBOTTOMRIGHT",
        _ => "HT_UNKNOWN",
    }
}

fn kind_hit_code(kind: isize) -> Option<i32> {
    match kind {
        KIND_MAX => Some(9),
        KIND_TITLEBAR => Some(2),
        k if k >= KIND_RESIZE_BASE => Some((k - KIND_RESIZE_BASE) as i32),
        _ => None,
    }
}

fn resize_kind(code: i32) -> isize {
    KIND_RESIZE_BASE + code as isize
}

fn cursor_screen_pos() -> (i32, i32) {
    let mut cursor = POINT { x: 0, y: 0 };
    unsafe {
        GetCursorPos(&mut cursor);
    }
    (cursor.x, cursor.y)
}

/// overlay 是否在该模式下启用（spec §4.7：只有 css 模式创建 overlay；dwm 是 0.3.1 等价对照，
/// 命中/拖动/缩放全部走原路径）。
pub fn enabled_for(cfg: &super::config::FrameConfig) -> bool {
    cfg.transparent()
}

/// 当前存活的 overlay 子窗数量（验收证据用：dwm/native 必须为 0）。
pub fn active_count() -> usize {
    slots().lock().map(|slots| slots.len()).unwrap_or(0)
}

/// 注册 overlay 窗口类（进程内一次）并记录共享状态。
pub fn ensure(state: &Arc<WindowFrameState>) {
    let first_time = !CLASS_REGISTERED.get().copied().unwrap_or(false);
    let _ = OVERLAY_STATE.set(Arc::clone(state));
    let _ = CLASS_REGISTERED.get_or_init(|| {
        let class_name = class_wide();
        let instance = unsafe { GetModuleHandleW(std::ptr::null()) };
        let null_brush = unsafe { GetStockObject(NULL_BRUSH) };
        let mut wc: WNDCLASSEXW = unsafe { std::mem::zeroed() };
        wc.cbSize = std::mem::size_of::<WNDCLASSEXW>() as u32;
        wc.style = 0;
        wc.lpfnWndProc = Some(overlay_proc);
        wc.cbClsExtra = 0;
        wc.cbWndExtra = 0;
        wc.hInstance = instance;
        wc.hIcon = std::ptr::null_mut();
        wc.hCursor = std::ptr::null_mut();
        wc.hbrBackground = null_brush as HBRUSH;
        wc.lpszMenuName = std::ptr::null();
        wc.lpszClassName = class_name.as_ptr();
        wc.hIconSm = std::ptr::null_mut();
        unsafe { RegisterClassExW(&wc) != 0 }
    });
    if first_time {
        log::info!("[qio] window-frame overlay 类已注册：{OVERLAY_CLASS_NAME}");
    }
}

/// 把一个洞从矩形里切出去（最多留下 4 块），用于 titlebar 段扣除交互控件。
fn subtract_rect(base: RegionRect, hole: &RegionRect) -> [Option<RegionRect>; 4] {
    let base_right = base.x + base.w;
    let base_bottom = base.y + base.h;
    let hole_right = hole.x + hole.w;
    let hole_bottom = hole.y + hole.h;
    if hole_right <= base.x || hole.x >= base_right || hole_bottom <= base.y || hole.y >= base_bottom
    {
        return [Some(base), None, None, None];
    }
    let mut out: [Option<RegionRect>; 4] = [None, None, None, None];
    if hole.y > base.y {
        out[0] = Some(RegionRect {
            x: base.x,
            y: base.y,
            w: base.w,
            h: hole.y - base.y,
        });
    }
    if hole_bottom < base_bottom {
        out[1] = Some(RegionRect {
            x: base.x,
            y: hole_bottom,
            w: base.w,
            h: base_bottom - hole_bottom,
        });
    }
    let mid_top = hole.y.max(base.y);
    let mid_bottom = hole_bottom.min(base_bottom);
    if mid_bottom > mid_top {
        if hole.x > base.x {
            out[2] = Some(RegionRect {
                x: base.x,
                y: mid_top,
                w: hole.x - base.x,
                h: mid_bottom - mid_top,
            });
        }
        if hole_right < base_right {
            out[3] = Some(RegionRect {
                x: hole_right,
                y: mid_top,
                w: base_right - hole_right,
                h: mid_bottom - mid_top,
            });
        }
    }
    out
}

fn clamp_rect(rect: RegionRect, content: &state::RectF) -> Option<RegionRect> {
    let left = rect.x.max(content.left);
    let top = rect.y.max(content.top);
    let right = (rect.x + rect.w).min(content.right);
    let bottom = (rect.y + rect.h).min(content.bottom);
    if right - left < 1.0 || bottom - top < 1.0 {
        None
    } else {
        Some(RegionRect {
            x: left,
            y: top,
            w: right - left,
            h: bottom - top,
        })
    }
}

/// 可见内容边内侧 band 宽的 8 方向缩放环（不与 padding 带冲突）。
fn resize_ring(content: &state::RectF, band: f64) -> Vec<(i32, RegionRect)> {
    let w = content.width();
    let h = content.height();
    if w < 8.0 || h < 8.0 {
        return Vec::new();
    }
    let band = band.min(w / 2.0).min(h / 2.0).max(0.0);
    if band < 1.0 {
        return Vec::new();
    }
    let (left, top, right, bottom) = (content.left, content.top, content.right, content.bottom);
    let (inner_left, inner_top) = (left + band, top + band);
    let (inner_right, inner_bottom) = (right - band, bottom - band);
    let mut out = Vec::new();
    let mut push = |code: i32, x: f64, y: f64, w: f64, h: f64| {
        if w >= 1.0 && h >= 1.0 {
            out.push((
                code,
                RegionRect {
                    x,
                    y,
                    w,
                    h,
                },
            ));
        }
    };
    push(13, left, top, band, band);
    push(14, right - band, top, band, band);
    push(16, left, bottom - band, band, band);
    push(17, right - band, bottom - band, band, band);
    push(10, left, inner_top, band, inner_bottom - inner_top);
    push(11, right - band, inner_top, band, inner_bottom - inner_top);
    push(12, inner_left, top, inner_right - inner_left, band);
    push(15, inner_left, bottom - band, inner_right - inner_left, band);
    drop(push);
    out
}

/// 重排 overlay。必须在 tauri 主线程执行（子窗口创建/移动的线程约束）。
///
/// 输入：前端上报的 HitRegions（物理像素、客户区坐标，含 interactive 洞）。
/// 输出：max（1 段）+ titlebar（扣洞后的 N 段）+ resize（8 段，仅 normal 状态）。
pub fn sync(state: &Arc<WindowFrameState>) {
    // spec §4.7：dwm/native 不创建 overlay（dwm 必须与 0.3.1 等价：原生命令路径/tao 缩放）。
    if !enabled_for(&state.cfg) {
        return;
    }
    ensure(state);
    if MAX_PRESSED.load(Ordering::SeqCst) || TB_PRESSED.load(Ordering::SeqCst) {
        log::debug!("[qio] window-frame overlay sync deferred（按压手势进行中）");
        return;
    }
    let hwnd = state.hwnd();
    if hwnd == 0 || !state.cfg.frameless() {
        return;
    }
    let geo = state::geometry(state);
    let regions = state.regions.lock().unwrap().clone();
    let client = state::client_rect(hwnd);
    let (padding, _radius) = state::effective_padding_radius(state, &geo, &regions);
    let content = state::content_rect(client, padding);
    if content.width() <= 4.0 || content.height() <= 4.0 {
        return;
    }

    let mut desired: Vec<(isize, RegionRect)> = Vec::new();
    if state.cfg.frame != FrameMode::Native {
        // 1) 最大化按钮 → HTMAXBUTTON（系统 Snap Layouts 飞出 / 点击 toggle 由 WndProc 处理）
        if let Some(max_button) = regions.max_button {
            if let Some(rect) = clamp_rect(max_button, &content) {
                desired.push((KIND_MAX, rect));
            }
        }
        // 2) 标题带 → HTCAPTION（拖拽区扣除 max/otherButtons/interactive）
        if let Some(titlebar) = regions.titlebar {
            let mut pieces = vec![titlebar];
            let mut holes: Vec<RegionRect> = Vec::new();
            if let Some(max_button) = regions.max_button {
                holes.push(max_button);
            }
            holes.extend(regions.other_buttons.iter().copied());
            // QIO 新增：设置入口/权限模式/异常入口/互动挂载点必须保持 DOM 可点。
            holes.extend(regions.interactive.iter().copied());
            for hole in holes {
                let mut next = Vec::new();
                for piece in pieces {
                    for part in subtract_rect(piece, &hole).into_iter().flatten() {
                        next.push(part);
                    }
                }
                pieces = next;
            }
            for piece in pieces {
                if let Some(rect) = clamp_rect(piece, &content) {
                    desired.push((KIND_TITLEBAR, rect));
                }
            }
        }
        // 3) 可见内容边内侧的八方向缩放条（最大化/贴靠时不做）
        if geo.state == "normal" {
            let band = regions
                .resize_border
                .filter(|v| *v > 0.0)
                .unwrap_or(RESIZE_BORDER_PHYSICAL);
            for (code, rect) in resize_ring(&content, band) {
                desired.push((resize_kind(code), rect));
            }
        }
    }

    let max_rect_now = desired
        .iter()
        .find(|(kind, _)| *kind == KIND_MAX)
        .map(|(_, rect)| *rect);
    let max_rect_changed = {
        let mut last = last_max_rect().lock().unwrap();
        let changed = *last != max_rect_now;
        if changed {
            *last = max_rect_now;
        }
        changed
    };
    if max_rect_changed && MAX_HOVER.load(Ordering::SeqCst) {
        emit_caption_hover(state, false);
    }

    let mut slots = slots().lock().unwrap();
    while slots.len() < desired.len() {
        match create_overlay(hwnd) {
            Some(overlay_hwnd) => slots.push(OverlaySlot {
                hwnd: overlay_hwnd,
                kind: KIND_HIDDEN,
                rect: RegionRect {
                    x: 0.0,
                    y: 0.0,
                    w: 0.0,
                    h: 0.0,
                },
            }),
            None => {
                log::warn!("[qio] window-frame overlay 创建失败（降级为无 overlay 命中）");
                break;
            }
        }
    }
    for (index, (kind, rect)) in desired.iter().copied().enumerate() {
        if index >= slots.len() {
            break;
        }
        let slot = &mut slots[index];
        if slot.kind == kind && slot.rect == rect {
            // WebView2 会在移动/聚焦时把自己的子窗提到最上，这里每次 sync 重新压顶。
            unsafe {
                SetWindowPos(
                    slot.hwnd as HWND,
                    HWND_TOP,
                    0,
                    0,
                    0,
                    0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
                );
            }
            continue;
        }
        unsafe {
            SetWindowLongPtrW(slot.hwnd as HWND, GWLP_USERDATA, kind);
            SetWindowPos(
                slot.hwnd as HWND,
                HWND_TOP,
                rect.x.round() as i32,
                rect.y.round() as i32,
                rect.w.round() as i32,
                rect.h.round() as i32,
                SWP_NOACTIVATE | SWP_SHOWWINDOW,
            );
        }
        slot.kind = kind;
        slot.rect = rect;
    }
    for slot in slots.iter_mut().skip(desired.len()) {
        if slot.kind != KIND_HIDDEN {
            unsafe {
                ShowWindow(slot.hwnd as HWND, SW_HIDE);
            }
            slot.kind = KIND_HIDDEN;
        }
    }
    drop(slots);

    let layout_json: Vec<serde_json::Value> = desired
        .iter()
        .map(|(kind, rect)| {
            let name = match *kind {
                KIND_MAX => "maxButton",
                KIND_TITLEBAR => "titlebar",
                k if k >= KIND_RESIZE_BASE => match (k - KIND_RESIZE_BASE) as i32 {
                    10 => "resize:HTLEFT",
                    11 => "resize:HTRIGHT",
                    12 => "resize:HTTOP",
                    15 => "resize:HTBOTTOM",
                    13 => "resize:HTTOPLEFT",
                    14 => "resize:HTTOPRIGHT",
                    16 => "resize:HTBOTTOMLEFT",
                    17 => "resize:HTBOTTOMRIGHT",
                    _ => "resize:other",
                },
                _ => "unknown",
            };
            serde_json::json!({ "kind": name, "x": rect.x, "y": rect.y, "w": rect.w, "h": rect.h })
        })
        .collect();
    let changed = {
        let digest = serde_json::to_string(&layout_json).unwrap_or_default();
        let mut last = last_layout().lock().unwrap();
        if last.as_deref() == Some(digest.as_str()) {
            false
        } else {
            *last = Some(digest);
            true
        }
    };
    if changed {
        if let Some(evidence) = state.evidence() {
            evidence.overlay_layout(&serde_json::json!({
                "ts": now_ms(),
                "frame": state.cfg.frame.as_str(),
                "state": geo.state,
                "padding": padding,
                "overlays": layout_json,
            }));
        }
        log::debug!(
            "[qio] window-frame overlay sync: {} 段（frame={} state={} pad={}）",
            desired.len(),
            state.cfg.frame.as_str(),
            geo.state,
            padding
        );
    }
}

fn create_overlay(parent: isize) -> Option<isize> {
    let class_name = class_wide();
    let instance = unsafe { GetModuleHandleW(std::ptr::null()) };
    let style = WS_CHILD | WS_CLIPSIBLINGS | WS_VISIBLE;
    let hwnd = unsafe {
        CreateWindowExW(
            0,
            class_name.as_ptr(),
            std::ptr::null(),
            style,
            0,
            0,
            0,
            0,
            parent as HWND,
            std::ptr::null_mut(),
            instance,
            std::ptr::null(),
        )
    };
    if hwnd.is_null() {
        None
    } else {
        Some(hwnd as isize)
    }
}

/// 销毁全部 overlay（主窗 WM_NCDESTROY / RunEvent::Exit 幂等调用）。
pub fn destroy_all() {
    let mut slots = slots().lock().unwrap();
    for slot in slots.iter() {
        if slot.hwnd != 0 {
            unsafe {
                DestroyWindow(slot.hwnd as HWND);
            }
        }
    }
    slots.clear();
}

/// 去抖地把重排调度到 tauri 主线程。
pub fn request_sync(state: &Arc<WindowFrameState>) {
    if state.overlay_sync_queued.swap(true, Ordering::SeqCst) {
        return;
    }
    let state_task = Arc::clone(state);
    let app = state.app.clone();
    let state_fallback = Arc::clone(state);
    if app
        .run_on_main_thread(move || {
            state_task.overlay_sync_queued.store(false, Ordering::SeqCst);
            sync(&state_task);
        })
        .is_err()
    {
        state_fallback
            .overlay_sync_queued
            .store(false, Ordering::SeqCst);
    }
}

fn emit_caption_hover(state: &Arc<WindowFrameState>, hover: bool) {
    let previous = MAX_HOVER.swap(hover, Ordering::SeqCst);
    if previous == hover {
        return;
    }
    let payload = serde_json::json!({
        "target": "max",
        "hover": hover,
        "ts": now_ms(),
    });
    if let Err(err) = state.app.emit(EVENT_CAPTION_HOVER, &payload) {
        log::warn!("[qio] window-frame caption-hover 事件发送失败：{err}");
    }
    if let Some(evidence) = state.evidence() {
        evidence.caption_hover(&payload);
    }
    log::debug!("[qio] window-frame caption-hover max hover={hover}");
}

fn queue_toggle_maximize(state: &Arc<WindowFrameState>, source: &'static str) {
    state.overlay_max_clicks.fetch_add(1, Ordering::Relaxed);
    log::info!(
        "[qio] window-frame overlay HTMAXBUTTON {source} -> toggle maximize（Rust 侧唯一触发）"
    );
    let state_task = Arc::clone(state);
    let app = state.app.clone();
    let state_fallback = Arc::clone(state);
    if app
        .run_on_main_thread(move || {
            let Some(window) = state_task.window() else {
                log::warn!("[qio] window-frame overlay toggle: 找不到主窗口");
                return;
            };
            let hwnd = state_task.hwnd();
            let zoomed = hwnd != 0
                && unsafe {
                    windows_sys::Win32::UI::WindowsAndMessaging::IsZoomed(hwnd as HWND) != 0
                };
            let result = if zoomed {
                window.unmaximize()
            } else {
                window.maximize()
            };
            if let Err(err) = result {
                log::warn!("[qio] window-frame overlay toggle maximize 失败：{err}");
            }
            state::refresh_and_emit(&state_task, true, "overlay-max-click");
            request_sync(&state_task);
        })
        .is_err()
    {
        state_fallback
            .overlay_max_clicks
            .fetch_add(0, Ordering::Relaxed);
    }
}

/// 把 HTCAPTION / HT* 的按下转发给顶层窗口（系统 move/resize loop 由系统接管）。
///
/// 与 tao 的 drag_window 同一手法：先 ReleaseCapture，再 PostMessage
/// WM_NCLBUTTONDOWN 给顶层窗口，DefWindowProc 进入系统拖动/缩放循环。
fn forward_nc_press(hit_code: usize, source: &'static str) {
    let Some(state) = OVERLAY_STATE.get() else {
        return;
    };
    let main_hwnd = state.hwnd();
    if main_hwnd == 0 {
        return;
    }
    unsafe {
        let (x, y) = cursor_screen_pos();
        let packed =
            ((x as i16 as u16 as i32) & 0xFFFF) | (((y as i16 as u16 as i32) & 0xFFFF) << 16);
        let _ = ReleaseCapture();
        PostMessageW(main_hwnd as HWND, WM_NCLBUTTONDOWN, hit_code, packed as isize);
    }
    log::debug!(
        "[qio] window-frame overlay 转发 WM_NCLBUTTONDOWN hit={} ({}) source={source}",
        hit_code,
        ht_name(hit_code as i32)
    );
}

fn titlebar_press(hwnd: HWND) {
    let (x, y) = cursor_screen_pos();
    *tb_press_point().lock().unwrap() = (x, y);
    TB_PRESSED.store(true, Ordering::SeqCst);
    TB_DRAGGING.store(false, Ordering::SeqCst);
    TB_FORWARDED.store(false, Ordering::SeqCst);
    unsafe {
        SetCapture(hwnd);
    }
}

fn titlebar_move() {
    if !TB_PRESSED.load(Ordering::SeqCst) || TB_DRAGGING.load(Ordering::SeqCst) {
        return;
    }
    let (px, py) = *tb_press_point().lock().unwrap();
    let (x, y) = cursor_screen_pos();
    let dx = (x - px) as f64;
    let dy = (y - py) as f64;
    if dx * dx + dy * dy >= 16.0 {
        TB_DRAGGING.store(true, Ordering::SeqCst);
        TB_FORWARDED.store(true, Ordering::SeqCst);
        TB_PRESSED.store(false, Ordering::SeqCst);
        let _ = unsafe { ReleaseCapture() };
        forward_nc_press(2, "titlebar-drag");
    }
}

/// 返回 true 表示本次抬起构成双击（标题带双击 = 最大化/还原）。
fn titlebar_release() -> bool {
    let pressed = TB_PRESSED.swap(false, Ordering::SeqCst);
    let dragging = TB_DRAGGING.swap(false, Ordering::SeqCst);
    let forwarded = TB_FORWARDED.swap(false, Ordering::SeqCst);
    if !pressed || dragging || forwarded {
        return false;
    }
    let (x, y) = cursor_screen_pos();
    let now = std::time::Instant::now();
    let double_click_ms = unsafe { GetDoubleClickTime() }.max(200) as u64;
    let mut last = tb_last_click().lock().unwrap();
    let is_double = match *last {
        Some((lx, ly, at)) => {
            now.duration_since(at) <= std::time::Duration::from_millis(double_click_ms)
                && (x - lx).abs() <= 4
                && (y - ly).abs() <= 4
        }
        None => false,
    };
    if is_double {
        *last = None;
        true
    } else {
        *last = Some((x, y, now));
        false
    }
}

unsafe extern "system" fn overlay_proc(
    hwnd: HWND,
    msg: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    let kind = unsafe { GetWindowLongPtrW(hwnd, GWLP_USERDATA) };
    let hit_code = kind_hit_code(kind);
    if matches!(
        msg,
        WM_LBUTTONDOWN
            | WM_LBUTTONUP
            | WM_LBUTTONDBLCLK
            | WM_NCLBUTTONDOWN
            | WM_NCLBUTTONUP
            | WM_NCLBUTTONDBLCLK
            | WM_CAPTURECHANGED_MSG
    ) {
        log::debug!(
            "[qio] window-frame overlay msg=0x{:04X} hwnd=0x{:X} kind=0x{:X}",
            msg,
            hwnd as isize,
            kind
        );
    }
    match msg {
        WM_NCHITTEST => {
            if let Some(code) = hit_code {
                if let Some(state) = OVERLAY_STATE.get() {
                    state.log_hit_decision(
                        &format!("OVERLAY_{}", ht_name(code)),
                        format!("overlay hwnd=0x{:X} kind=0x{:X}", hwnd as isize, kind),
                    );
                }
                return code as LRESULT;
            }
            DefWindowProcW(hwnd, msg, wparam, lparam)
        }
        WM_SETCURSOR => {
            let cursor_name = match kind {
                KIND_MAX | KIND_TITLEBAR => Some(IDC_ARROW),
                k if k >= KIND_RESIZE_BASE => match k - KIND_RESIZE_BASE {
                    10 | 11 => Some(IDC_SIZEWE),
                    12 | 15 => Some(IDC_SIZENS),
                    13 | 17 => Some(IDC_SIZENWSE),
                    14 | 16 => Some(IDC_SIZENESW),
                    _ => None,
                },
                _ => None,
            };
            if let Some(cursor_name) = cursor_name {
                let cursor = LoadCursorW(std::ptr::null_mut(), cursor_name);
                if !cursor.is_null() {
                    SetCursor(cursor);
                }
                return 1;
            }
            DefWindowProcW(hwnd, msg, wparam, lparam)
        }
        WM_NCLBUTTONDOWN | WM_LBUTTONDOWN => {
            let code = (wparam as u32 & 0xFFFF) as usize;
            let resolved = hit_code.unwrap_or(code as i32);
            match kind {
                KIND_MAX => {
                    MAX_PRESSED.store(true, Ordering::SeqCst);
                    // 捕获鼠标：即使光标移出按钮或窗口激活，抬起也回到本 overlay。
                    unsafe {
                        SetCapture(hwnd);
                    }
                    0
                }
                KIND_TITLEBAR => {
                    titlebar_press(hwnd);
                    0
                }
                k if k >= KIND_RESIZE_BASE => {
                    forward_nc_press(resolved as usize, "resize");
                    0
                }
                _ => DefWindowProcW(hwnd, msg, wparam, lparam),
            }
        }
        WM_NCLBUTTONUP | WM_LBUTTONUP => match kind {
            KIND_MAX => {
                let pressed = MAX_PRESSED.swap(false, Ordering::SeqCst);
                let _ = unsafe { ReleaseCapture() };
                if pressed {
                    if let Some(state) = OVERLAY_STATE.get() {
                        queue_toggle_maximize(state, "max-button-up");
                        request_sync(state);
                    }
                }
                0
            }
            KIND_TITLEBAR => {
                let _ = unsafe { ReleaseCapture() };
                if titlebar_release() {
                    if let Some(state) = OVERLAY_STATE.get() {
                        queue_toggle_maximize(state, "titlebar-double-click");
                        request_sync(state);
                    }
                } else if let Some(state) = OVERLAY_STATE.get() {
                    request_sync(state);
                }
                0
            }
            _ => DefWindowProcW(hwnd, msg, wparam, lparam),
        },
        WM_NCLBUTTONDBLCLK | WM_LBUTTONDBLCLK => match kind {
            KIND_MAX => {
                MAX_PRESSED.store(false, Ordering::SeqCst);
                let _ = unsafe { ReleaseCapture() };
                if let Some(state) = OVERLAY_STATE.get() {
                    queue_toggle_maximize(state, "max-button-dblclick");
                    request_sync(state);
                }
                0
            }
            KIND_TITLEBAR => {
                TB_PRESSED.store(false, Ordering::SeqCst);
                TB_DRAGGING.store(false, Ordering::SeqCst);
                *tb_last_click().lock().unwrap() = None;
                let _ = unsafe { ReleaseCapture() };
                if let Some(state) = OVERLAY_STATE.get() {
                    queue_toggle_maximize(state, "titlebar-double-click");
                    request_sync(state);
                }
                0
            }
            _ => DefWindowProcW(hwnd, msg, wparam, lparam),
        },
        WM_NCMOUSEMOVE | WM_MOUSEMOVE => {
            if kind == KIND_TITLEBAR {
                titlebar_move();
                return 0;
            }
            if kind == KIND_MAX {
                if let Some(state) = OVERLAY_STATE.get() {
                    emit_caption_hover(state, true);
                }
                let mut track: TRACKMOUSEEVENT = unsafe { std::mem::zeroed() };
                track.cbSize = std::mem::size_of::<TRACKMOUSEEVENT>() as u32;
                track.dwFlags = TME_LEAVE | TME_NONCLIENT;
                track.hwndTrack = hwnd;
                unsafe {
                    TrackMouseEvent(&mut track);
                }
            }
            0
        }
        WM_NCMOUSELEAVE | WM_MOUSELEAVE_MSG => {
            if kind == KIND_MAX {
                if let Some(state) = OVERLAY_STATE.get() {
                    emit_caption_hover(state, false);
                }
            }
            0
        }
        WM_CAPTURECHANGED_MSG => {
            // 覆盖「捕获丢失」：若 capture 被别的窗口拿走而按压状态还挂着，
            // 必须复位并重排，否则 overlay sync 会一直被 defer（点击再也回不来）。
            let new_capture = lparam as isize;
            if new_capture != hwnd as isize
                && (MAX_PRESSED.load(Ordering::SeqCst) || TB_PRESSED.load(Ordering::SeqCst))
            {
                let had_max = MAX_PRESSED.swap(false, Ordering::SeqCst);
                let had_titlebar = TB_PRESSED.swap(false, Ordering::SeqCst);
                TB_DRAGGING.store(false, Ordering::SeqCst);
                log::warn!(
                    "[qio] window-frame overlay 捕获丢失（maxPressed={had_max} titlebarPressed={had_titlebar} newCapture=0x{new_capture:X}）：复位按压状态并重排"
                );
                if let Some(state) = OVERLAY_STATE.get() {
                    request_sync(state);
                }
            }
            DefWindowProcW(hwnd, msg, wparam, lparam)
        }
        WM_MOUSEACTIVATE_MSG => DefWindowProcW(hwnd, msg, wparam, lparam),
        WM_ERASEBKGND => 1,
        WM_PAINT => {
            let mut paint: PAINTSTRUCT = unsafe { std::mem::zeroed() };
            unsafe {
                BeginPaint(hwnd, &mut paint);
                EndPaint(hwnd, &paint);
            }
            0
        }
        WM_NCDESTROY => DefWindowProcW(hwnd, msg, wparam, lparam),
        _ => DefWindowProcW(hwnd, msg, wparam, lparam),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn subtract_rect_splits_into_rectangles() {
        let base = RegionRect { x: 0.0, y: 0.0, w: 100.0, h: 50.0 };
        let hole = RegionRect { x: 40.0, y: 0.0, w: 20.0, h: 50.0 };
        let parts: Vec<_> = subtract_rect(base, &hole).into_iter().flatten().collect();
        assert_eq!(parts.len(), 2);
        let total: f64 = parts.iter().map(|r| r.w * r.h).sum();
        assert_eq!(total, 100.0 * 50.0 - 20.0 * 50.0);
    }

    #[test]
    fn subtract_rect_no_overlap_keeps_base() {
        let base = RegionRect { x: 0.0, y: 0.0, w: 10.0, h: 10.0 };
        let hole = RegionRect { x: 20.0, y: 20.0, w: 5.0, h: 5.0 };
        let parts: Vec<_> = subtract_rect(base, &hole).into_iter().flatten().collect();
        assert_eq!(parts.len(), 1);
        assert_eq!(parts[0], base);
    }

    #[test]
    fn resize_ring_has_eight_segments_inside_content() {
        let content = state::RectF { left: 56.0, top: 56.0, right: 2344.0, bottom: 1544.0 };
        let ring = resize_ring(&content, 8.0);
        assert_eq!(ring.len(), 8);
        for (_, rect) in &ring {
            assert!(rect.x >= content.left - 0.001);
            assert!(rect.y >= content.top - 0.001);
            assert!(rect.x + rect.w <= content.right + 0.001);
            assert!(rect.y + rect.h <= content.bottom + 0.001);
        }
        let codes: Vec<i32> = ring.iter().map(|(code, _)| *code).collect();
        for code in [10, 11, 12, 13, 14, 15, 16, 17] {
            assert!(codes.contains(&code), "missing hit code {code}");
        }
    }

    #[test]
    fn resize_ring_is_inset_from_content_edge() {
        let content = state::RectF { left: 0.0, top: 0.0, right: 100.0, bottom: 100.0 };
        let ring = resize_ring(&content, 8.0);
        let left = ring.iter().find(|(code, _)| *code == 10).unwrap().1;
        assert_eq!(left.x, 0.0);
        assert_eq!(left.w, 8.0);
        let right = ring.iter().find(|(code, _)| *code == 11).unwrap().1;
        assert_eq!(right.x, 92.0);
        assert_eq!(right.x + right.w, 100.0);
    }

    #[test]
    fn clamp_rect_clips_to_content_and_drops_empty() {
        let content = state::RectF { left: 10.0, top: 10.0, right: 110.0, bottom: 110.0 };
        let clipped = clamp_rect(
            RegionRect { x: 0.0, y: 0.0, w: 50.0, h: 50.0 },
            &content,
        )
        .unwrap();
        assert_eq!(clipped.x, 10.0);
        assert_eq!(clipped.y, 10.0);
        assert!(clamp_rect(
            RegionRect { x: 200.0, y: 200.0, w: 10.0, h: 10.0 },
            &content
        )
        .is_none());
    }

    #[test]
    fn overlay_is_css_only() {
        use crate::window_frame::config::{FrameConfig, FrameMode, PassthroughMode};
        let base = FrameConfig {
            frame: FrameMode::Css,
            passthrough: PassthroughMode::Poll,
            show_on_load: true,
            ui_query: None,
            windows_build: 26200,
            forced_native_reason: None,
            test_fail_css: false,
        };
        assert!(enabled_for(&base));
        let mut dwm = base.clone();
        dwm.frame = FrameMode::Dwm;
        dwm.passthrough = PassthroughMode::None;
        assert!(!enabled_for(&dwm));
        let mut native = base.clone();
        native.frame = FrameMode::Native;
        native.passthrough = PassthroughMode::None;
        assert!(!enabled_for(&native));
    }

    #[test]
    fn hittest_kind_mapping_is_stable() {
        assert_eq!(kind_hit_code(KIND_MAX), Some(9));
        assert_eq!(kind_hit_code(KIND_TITLEBAR), Some(2));
        assert_eq!(kind_hit_code(resize_kind(13)), Some(13));
        assert_eq!(kind_hit_code(KIND_HIDDEN), None);
    }
}