//! 窗口层共享状态、几何探测、窗口状态事件与诊断证据。
//!
//! 规格：docs/.qio032-part3.md §3.2/§3.3/§4.6。设计要点：
//! * 所有几何一律 **物理像素**（rect/workArea/padding/radius），前端按 dpr 换算；
//! * 事件去重（ts 不参与比较），几何/状态/DPI/穿透模式变化才 emit；
//! * 证据 JSONL 只在 QIO_WINDOW_EVIDENCE=1 时写（产品常态不写盘），验收脚本据此复核。

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU8, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, WebviewWindow};
use windows_sys::Win32::Foundation::{HWND, RECT};
use windows_sys::Win32::Graphics::Gdi::{
    GetMonitorInfoW, MonitorFromWindow, MONITORINFO, MONITOR_DEFAULTTONEAREST,
};
use windows_sys::Win32::UI::HiDpi::GetDpiForWindow;
use windows_sys::Win32::UI::WindowsAndMessaging::{GetClientRect, GetWindowRect, IsZoomed};

use super::config::{
    FrameConfig, PassthroughMode, RESIZE_BORDER_PHYSICAL,
};

pub const EVENT_WINDOW_STATE: &str = "qio://window-state";
pub const EVENT_CAPTION_HOVER: &str = "qio://caption-hover";
pub const EVENT_FRAME_READY: &str = "qio://frame-ready";

const EVIDENCE_WINDOW_STATE: &str = "window-state.jsonl";
const EVIDENCE_INIT: &str = "init.jsonl";
const EVIDENCE_HIT_REGIONS: &str = "hit-regions.jsonl";
const EVIDENCE_HIT_TEST: &str = "hit-test.log";
const EVIDENCE_OVERLAY_LAYOUT: &str = "overlay-layout.jsonl";
const EVIDENCE_PASSTHROUGH: &str = "passthrough.jsonl";
const EVIDENCE_CAPTION_HOVER: &str = "caption-hover.jsonl";

pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

// ---------------------------------------------------------------------------
// 诊断证据（QIO_WINDOW_EVIDENCE=1 时启用）
// ---------------------------------------------------------------------------

/// 证据写入器：目录由 main 传入（<QIO_DATA_DIR>/logs/window-frame）。
#[derive(Clone)]
pub struct Evidence {
    dir: PathBuf,
}

impl Evidence {
    pub fn new(dir: PathBuf) -> std::io::Result<Self> {
        std::fs::create_dir_all(&dir)?;
        Ok(Self { dir })
    }

    pub fn json_line(&self, file: &str, value: &serde_json::Value) {
        self.append(file, &value.to_string());
    }

    pub fn line(&self, file: &str, text: impl AsRef<str>) {
        self.append(file, text.as_ref());
    }

    pub fn window_state(&self, value: &serde_json::Value) {
        self.json_line(EVIDENCE_WINDOW_STATE, value);
    }

    /// 启动/初始化/显示/失败记录（harness 用 init.jsonl 判定 mode 与各步结果）。
    pub fn init(&self, value: &serde_json::Value) {
        self.json_line(EVIDENCE_INIT, value);
    }

    pub fn hit_regions(&self, value: &serde_json::Value) {
        self.json_line(EVIDENCE_HIT_REGIONS, value);
    }

    pub fn hit_test(&self, text: impl AsRef<str>) {
        self.line(EVIDENCE_HIT_TEST, text);
    }

    pub fn overlay_layout(&self, value: &serde_json::Value) {
        self.json_line(EVIDENCE_OVERLAY_LAYOUT, value);
    }

    pub fn passthrough_toggle(&self, value: &serde_json::Value) {
        // 主文件按 Lead 验收契约命名 passthrough.jsonl；spec part3 的
        // passthrough-toggle.jsonl 同时保留，两个读取方都能工作。
        self.json_line(EVIDENCE_PASSTHROUGH, value);
        self.json_line("passthrough-toggle.jsonl", value);
    }

    pub fn caption_hover(&self, value: &serde_json::Value) {
        self.json_line(EVIDENCE_CAPTION_HOVER, value);
    }

    fn append(&self, file: &str, text: &str) {
        use std::io::Write;
        if let Ok(mut handle) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.dir.join(file))
        {
            let _ = writeln!(handle, "{text}");
        }
    }
}

/// 限速的 hit-test 日志（同决策最多 1s 一条，决策变化立即写）。
pub struct HitLogger {
    last: Option<String>,
    last_logged: Option<Instant>,
}

impl HitLogger {
    fn new() -> Self {
        Self {
            last: None,
            last_logged: None,
        }
    }
}

// ---------------------------------------------------------------------------
// 共享状态
// ---------------------------------------------------------------------------

pub struct Inner {
    pub hwnd: isize,
    pub window: Option<WebviewWindow>,
    pub dpi: u32,
    pub subclass_installed: bool,
}

pub struct WindowFrameState {
    pub cfg: FrameConfig,
    pub app: AppHandle,
    pub evidence: Option<Evidence>,
    pub inner: Mutex<Inner>,
    pub regions: Mutex<HitRegions>,
    pub last_emit: Mutex<Option<WindowStatePayload>>,
    pub hit_logger: Mutex<HitLogger>,
    /// 当前穿透模式（none/poll），运行期可通过诊断命令切换。
    passthrough_mode: AtomicU8,
    pub poll_transparent: AtomicBool,
    pub poll_stop: AtomicBool,
    pub poll_handle: Mutex<Option<std::thread::JoinHandle<()>>>,
    pub in_size_move: AtomicBool,
    pub refresh_queued: AtomicBool,
    pub overlay_sync_queued: AtomicBool,
    /// 0.3.2 不接 click-forward，仅保留互斥位/扩展点（poll_tick 会检查）。
    pub forwarding: AtomicBool,
    pub page_ready: AtomicBool,
    pub shown: AtomicBool,
    pub nchittest_count: AtomicU64,
    pub overlay_max_clicks: AtomicU64,
    /// 退出清理只执行一次（ExitRequested + Exit 都会进来）。
    pub shutdown_done: AtomicBool,
}

impl WindowFrameState {
    pub fn new(cfg: FrameConfig, app: AppHandle, evidence: Option<Evidence>) -> Self {
        let mode = cfg.passthrough;
        Self {
            cfg,
            app,
            evidence,
            inner: Mutex::new(Inner {
                hwnd: 0,
                window: None,
                dpi: 96,
                subclass_installed: false,
            }),
            regions: Mutex::new(HitRegions::default()),
            last_emit: Mutex::new(None),
            hit_logger: Mutex::new(HitLogger::new()),
            passthrough_mode: AtomicU8::new(passthrough_code(mode)),
            poll_transparent: AtomicBool::new(false),
            poll_stop: AtomicBool::new(false),
            poll_handle: Mutex::new(None),
            in_size_move: AtomicBool::new(false),
            refresh_queued: AtomicBool::new(false),
            overlay_sync_queued: AtomicBool::new(false),
            forwarding: AtomicBool::new(false),
            page_ready: AtomicBool::new(false),
            shown: AtomicBool::new(false),
            nchittest_count: AtomicU64::new(0),
            overlay_max_clicks: AtomicU64::new(0),
            shutdown_done: AtomicBool::new(false),
        }
    }

    pub fn hwnd(&self) -> isize {
        self.inner.lock().unwrap().hwnd
    }

    pub fn window(&self) -> Option<WebviewWindow> {
        self.inner.lock().unwrap().window.clone()
    }

    pub fn set_window(&self, window: WebviewWindow, dpi: u32) {
        let mut inner = self.inner.lock().unwrap();
        inner.hwnd = window.hwnd().map(|h| h.0 as isize).unwrap_or(0);
        inner.window = Some(window);
        inner.dpi = dpi;
    }

    pub fn dpi(&self) -> u32 {
        let hwnd = self.hwnd();
        if hwnd != 0 {
            let dpi = unsafe { GetDpiForWindow(hwnd as HWND) };
            if dpi > 0 {
                return dpi;
            }
        }
        self.inner.lock().unwrap().dpi.max(96)
    }

    pub fn passthrough(&self) -> PassthroughMode {
        passthrough_from_code(self.passthrough_mode.load(Ordering::SeqCst))
    }

    pub fn set_passthrough(&self, mode: PassthroughMode) {
        self.passthrough_mode
            .store(passthrough_code(mode), Ordering::SeqCst);
    }

    pub fn log_hit_decision(&self, decision: &str, detail: impl AsRef<str>) {
        let mut logger = self.hit_logger.lock().unwrap();
        let changed = logger.last.as_deref() != Some(decision);
        let due = logger
            .last_logged
            .map(|at| at.elapsed() >= std::time::Duration::from_millis(1000))
            .unwrap_or(true);
        if changed || due {
            logger.last = Some(decision.to_string());
            logger.last_logged = Some(Instant::now());
            if let Some(evidence) = &self.evidence {
                evidence.hit_test(format!(
                    "{} decision={decision} {}",
                    now_ms(),
                    detail.as_ref()
                ));
            }
            log::debug!("[qio] window-frame hit-test decision={decision} {}", detail.as_ref());
        }
    }

    pub fn evidence(&self) -> Option<&Evidence> {
        self.evidence.as_ref()
    }
}

fn passthrough_code(mode: PassthroughMode) -> u8 {
    match mode {
        PassthroughMode::None => 0,
        PassthroughMode::Poll => 1,
    }
}

fn passthrough_from_code(code: u8) -> PassthroughMode {
    match code {
        1 => PassthroughMode::Poll,
        _ => PassthroughMode::None,
    }
}

// ---------------------------------------------------------------------------
// 前端上报的命中布局（qio_set_hit_regions）
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, Debug, Default, PartialEq, Deserialize, Serialize)]
pub struct RegionRect {
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
}

impl RegionRect {
    pub fn is_finite(&self) -> bool {
        self.x.is_finite()
            && self.y.is_finite()
            && self.w.is_finite()
            && self.h.is_finite()
            && self.w >= 0.0
            && self.h >= 0.0
    }
}

/// 物理像素、窗口客户区坐标（camelCase 与前端一致）。
#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct HitRegions {
    #[serde(default)]
    pub dpr: Option<f64>,
    #[serde(default)]
    pub frame_padding: Option<f64>,
    #[serde(default)]
    pub radius: Option<f64>,
    #[serde(default)]
    pub resize_border: Option<f64>,
    #[serde(default)]
    pub state: Option<String>,
    #[serde(default)]
    pub titlebar: Option<RegionRect>,
    #[serde(default)]
    pub max_button: Option<RegionRect>,
    #[serde(default)]
    pub other_buttons: Vec<RegionRect>,
    #[serde(default)]
    pub interactive: Vec<RegionRect>,
    #[serde(default)]
    pub corner_radius: Option<f64>,
}

impl HitRegions {
    /// 去掉非法/非有限矩形，避免前端算错把 overlay 铺满整窗。
    pub fn sanitize(mut self) -> Self {
        self.titlebar = self.titlebar.filter(|r| r.is_finite());
        self.max_button = self.max_button.filter(|r| r.is_finite());
        self.other_buttons.retain(|r| r.is_finite());
        self.interactive.retain(|r| r.is_finite());
        if let Some(pad) = self.frame_padding {
            if !pad.is_finite() || pad < 0.0 {
                self.frame_padding = None;
            }
        }
        if let Some(radius) = self.radius {
            if !radius.is_finite() || radius < 0.0 {
                self.radius = None;
            }
        }
        if let Some(border) = self.resize_border {
            if !border.is_finite() || border < 0.0 {
                self.resize_border = None;
            }
        }
        self
    }
}

// ---------------------------------------------------------------------------
// 几何
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct RectI {
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
}

impl RectI {
    pub fn from_win32(rect: &RECT) -> Self {
        Self {
            x: rect.left,
            y: rect.top,
            w: rect.right - rect.left,
            h: rect.bottom - rect.top,
        }
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct RectF {
    pub left: f64,
    pub top: f64,
    pub right: f64,
    pub bottom: f64,
}

impl RectF {
    pub fn width(&self) -> f64 {
        self.right - self.left
    }

    pub fn height(&self) -> f64 {
        self.bottom - self.top
    }

    pub fn contains(&self, x: f64, y: f64) -> bool {
        x >= self.left && x < self.right && y >= self.top && y < self.bottom
    }

    /// 圆角矩形内判定（供 poll 穿透用）：先判矩形，再扣掉四角圆弧外区域。
    pub fn contains_rounded(&self, x: f64, y: f64, radius: f64) -> bool {
        if !self.contains(x, y) {
            return false;
        }
        let max_r = (self.width() / 2.0).min(self.height() / 2.0).max(0.0);
        let r = radius.max(0.0).min(max_r);
        if r <= 0.0 {
            return true;
        }
        let corner_x = if x < self.left + r {
            Some(self.left + r)
        } else if x > self.right - r {
            Some(self.right - r)
        } else {
            None
        };
        let corner_y = if y < self.top + r {
            Some(self.top + r)
        } else if y > self.bottom - r {
            Some(self.bottom - r)
        } else {
            None
        };
        match (corner_x, corner_y) {
            (Some(cx), Some(cy)) => {
                let dx = x - cx;
                let dy = y - cy;
                dx * dx + dy * dy <= r * r
            }
            _ => true,
        }
    }
}

#[derive(Clone, Debug)]
pub struct Geometry {
    pub rect: RectI,
    pub work_area: RectI,
    pub dpi: u32,
    pub scale_factor: f64,
    pub state: String,
    pub zone: Option<String>,
}

pub fn client_rect(hwnd: isize) -> RectI {
    if hwnd == 0 {
        return RectI::default();
    }
    let mut rect: RECT = unsafe { std::mem::zeroed() };
    if unsafe { GetClientRect(hwnd as HWND, &mut rect) } != 0 {
        RectI::from_win32(&rect)
    } else {
        RectI::default()
    }
}

pub fn geometry(state: &WindowFrameState) -> Geometry {
    let hwnd = state.hwnd();
    let mut rect = RectI::default();
    let mut work_area = RectI::default();
    let mut dpi = 96u32;
    let mut maximized = false;
    if hwnd != 0 {
        unsafe {
            let mut win_rect: RECT = std::mem::zeroed();
            if GetWindowRect(hwnd as HWND, &mut win_rect) != 0 {
                rect = RectI::from_win32(&win_rect);
            }
            let win_dpi = GetDpiForWindow(hwnd as HWND);
            if win_dpi > 0 {
                dpi = win_dpi;
            }
            let monitor = MonitorFromWindow(hwnd as HWND, MONITOR_DEFAULTTONEAREST);
            if !monitor.is_null() {
                let mut info: MONITORINFO = std::mem::zeroed();
                info.cbSize = std::mem::size_of::<MONITORINFO>() as u32;
                if GetMonitorInfoW(monitor, &mut info) != 0 {
                    work_area = RectI::from_win32(&info.rcWork);
                }
            }
            maximized = IsZoomed(hwnd as HWND) != 0;
        }
    }
    let zone = if maximized { None } else { detect_zone(rect, work_area) };
    let state_name = if maximized {
        "maximized"
    } else if zone.is_some() {
        "snapped"
    } else {
        "normal"
    };
    Geometry {
        rect,
        work_area,
        dpi,
        scale_factor: dpi as f64 / 96.0,
        state: state_name.to_string(),
        zone: zone.map(|z| z.to_string()),
    }
}

/// Windows 11 贴靠分区识别：窗口 rect 与工作区比较，容差 2 物理 px。
pub fn detect_zone(rect: RectI, work: RectI) -> Option<&'static str> {
    if work.w <= 0 || work.h <= 0 || rect.w <= 0 || rect.h <= 0 {
        return None;
    }
    const TOL: i32 = 2;
    let near = |a: i32, b: i32| (a - b).abs() <= TOL;
    let half_w = work.w / 2;
    let half_h = work.h / 2;
    let third_w = work.w / 3;

    let left_aligned = near(rect.x, work.x);
    let right_aligned = near(rect.x + rect.w, work.x + work.w);
    let top_aligned = near(rect.y, work.y);
    let bottom_aligned = near(rect.y + rect.h, work.y + work.h);
    let full_w = near(rect.w, work.w);
    let full_h = near(rect.h, work.h);

    if top_aligned && near(rect.h, half_h) {
        if left_aligned && near(rect.w, half_w) {
            return Some("tl");
        }
        if right_aligned && near(rect.w, half_w) {
            return Some("tr");
        }
    }
    if bottom_aligned && near(rect.h, half_h) {
        if left_aligned && near(rect.w, half_w) {
            return Some("bl");
        }
        if right_aligned && near(rect.w, half_w) {
            return Some("br");
        }
    }
    if full_h {
        if left_aligned && near(rect.w, half_w) {
            return Some("left-half");
        }
        if right_aligned && near(rect.w, half_w) {
            return Some("right-half");
        }
        if left_aligned && near(rect.w, third_w) {
            return Some("left-third");
        }
        if near(rect.x, work.x + third_w) && near(rect.w, third_w) {
            return Some("center-third");
        }
        if right_aligned && near(rect.w, third_w) {
            return Some("right-third");
        }
    }
    if full_w && top_aligned && near(rect.h, half_h) {
        return Some("top-half");
    }
    if full_w && bottom_aligned && near(rect.h, half_h) {
        return Some("bottom-half");
    }
    None
}

/// 生效的 padding/radius（物理像素）：最大化/贴靠时归零，与前端 CSS 归零对齐。
pub fn effective_padding_radius(
    state: &WindowFrameState,
    geo: &Geometry,
    regions: &HitRegions,
) -> (f64, f64) {
    if geo.state != "normal" {
        return (0.0, 0.0);
    }
    let scale = geo.scale_factor;
    let padding = regions
        .frame_padding
        .unwrap_or_else(|| state.cfg.default_padding_css() * scale)
        .max(0.0);
    let radius = regions
        .radius
        .or(regions.corner_radius)
        .unwrap_or_else(|| state.cfg.default_radius_css() * scale)
        .max(0.0);
    (padding, radius)
}

pub fn content_rect(client: RectI, padding: f64) -> RectF {
    RectF {
        left: padding,
        top: padding,
        right: client.w as f64 - padding,
        bottom: client.h as f64 - padding,
    }
}

pub fn resize_border(_geo: &Geometry, regions: &HitRegions) -> f64 {
    regions
        .resize_border
        .filter(|v| *v > 0.0)
        .unwrap_or(RESIZE_BORDER_PHYSICAL)
        .max(0.0)
}

// ---------------------------------------------------------------------------
// 事件 payload
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct WindowStatePayload {
    pub state: String,
    pub zone: Option<String>,
    pub rect: RectI,
    pub work_area: RectI,
    pub dpi: u32,
    pub scale_factor: f64,
    pub padding: f64,
    pub radius: f64,
    pub frame: String,
    pub passthrough: String,
    pub titlebar: String,
    pub ts: u64,
}

impl WindowStatePayload {
    pub fn eq_ignoring_ts(&self, other: &Self) -> bool {
        self.state == other.state
            && self.zone == other.zone
            && self.rect == other.rect
            && self.work_area == other.work_area
            && self.dpi == other.dpi
            && (self.scale_factor - other.scale_factor).abs() < f64::EPSILON
            && (self.padding - other.padding).abs() < 0.001
            && (self.radius - other.radius).abs() < 0.001
            && self.frame == other.frame
            && self.passthrough == other.passthrough
            && self.titlebar == other.titlebar
    }
}

/// qio_get_window_frame / qio://frame-ready 的 payload（spec §3.3）。
///
/// 规格字段是 {frame,titlebar,pad,radius,resizeBorder,dpi,state,passthrough}；
/// 这里额外附带几何超集（padding/scaleFactor/dpr/zone/rect/workArea/ts），
/// 前端只读 spec 字段也完全兼容。
#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct FrameRuntimePayload {
    pub frame: String,
    pub titlebar: String,
    pub pad: f64,
    pub radius: f64,
    pub resize_border: f64,
    pub dpi: u32,
    pub state: String,
    pub passthrough: String,
    pub padding: f64,
    pub scale_factor: f64,
    pub dpr: f64,
    pub zone: Option<String>,
    pub rect: RectI,
    pub work_area: RectI,
    pub ts: u64,
}

pub fn snapshot(state: &WindowFrameState) -> WindowStatePayload {
    let geo = geometry(state);
    let regions = state.regions.lock().unwrap().clone();
    let (padding, radius) = effective_padding_radius(state, &geo, &regions);
    WindowStatePayload {
        state: geo.state.clone(),
        zone: geo.zone.clone(),
        rect: geo.rect,
        work_area: geo.work_area,
        dpi: geo.dpi,
        scale_factor: geo.scale_factor,
        padding,
        radius,
        frame: state.cfg.frame.as_str().to_string(),
        passthrough: state.passthrough().as_str().to_string(),
        titlebar: "native".to_string(),
        ts: now_ms(),
    }
}

pub fn runtime_payload(state: &WindowFrameState) -> FrameRuntimePayload {
    let geo = geometry(state);
    let regions = state.regions.lock().unwrap().clone();
    let (padding, radius) = effective_padding_radius(state, &geo, &regions);
    FrameRuntimePayload {
        frame: state.cfg.frame.as_str().to_string(),
        titlebar: "native".to_string(),
        pad: padding,
        radius,
        resize_border: resize_border(&geo, &regions),
        dpi: geo.dpi,
        state: geo.state.clone(),
        passthrough: state.passthrough().as_str().to_string(),
        padding,
        scale_factor: geo.scale_factor,
        dpr: geo.scale_factor,
        zone: geo.zone.clone(),
        rect: geo.rect,
        work_area: geo.work_area,
        ts: now_ms(),
    }
}

/// 状态去重后 emit；force 时无条件发。
pub fn refresh_and_emit(state: &Arc<WindowFrameState>, force: bool, reason: &str) -> WindowStatePayload {
    let payload = snapshot(state);
    let changed = {
        let mut last = state.last_emit.lock().unwrap();
        let changed = match last.as_ref() {
            Some(prev) => !prev.eq_ignoring_ts(&payload),
            None => true,
        };
        if changed || force {
            *last = Some(payload.clone());
        }
        changed
    };
    if changed || force {
        if let Some(evidence) = state.evidence() {
            if let Ok(value) = serde_json::to_value(&payload) {
                evidence.window_state(&value);
            }
        }
        match state.app.emit(EVENT_WINDOW_STATE, &payload) {
            Ok(()) => log::debug!(
                "[qio] window-frame state reason={reason} state={} zone={:?} rect={}x{}+{}+{} pad={} radius={} passthrough={}",
                payload.state,
                payload.zone,
                payload.rect.w,
                payload.rect.h,
                payload.rect.x,
                payload.rect.y,
                payload.padding,
                payload.radius,
                payload.passthrough
            ),
            Err(err) => log::warn!("[qio] window-frame 事件发送失败（{EVENT_WINDOW_STATE}）：{err}"),
        }
    }
    payload
}

/// 去抖地把状态刷新调度到 tauri 主线程（可从 wndproc 调用）。
pub fn queue_refresh(state: &Arc<WindowFrameState>, reason: &'static str) {
    if state.refresh_queued.swap(true, Ordering::SeqCst) {
        return;
    }
    let state_task = Arc::clone(state);
    let app = state.app.clone();
    let state_fallback = Arc::clone(state);
    if app
        .run_on_main_thread(move || {
            state_task.refresh_queued.store(false, Ordering::SeqCst);
            refresh_and_emit(&state_task, false, reason);
        })
        .is_err()
    {
        state_fallback.refresh_queued.store(false, Ordering::SeqCst);
    }
}

/// 页面就绪后发一次 frame-ready（payload 与 qio_get_window_frame 一致）。
pub fn emit_frame_ready(state: &Arc<WindowFrameState>) {
    let payload = runtime_payload(state);
    if let Some(evidence) = state.evidence() {
        if let Ok(value) = serde_json::to_value(&payload) {
            evidence.json_line("frame-ready.jsonl", &value);
        }
    }
    match state.app.emit(EVENT_FRAME_READY, &payload) {
        Ok(()) => log::info!(
            "[qio] window-frame frame-ready frame={} dpi={} state={} passthrough={}",
            payload.frame,
            payload.dpi,
            payload.state,
            payload.passthrough
        ),
        Err(err) => log::warn!("[qio] window-frame 事件发送失败（{EVENT_FRAME_READY}）：{err}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rect(x: i32, y: i32, w: i32, h: i32) -> RectI {
        RectI { x, y, w, h }
    }

    #[test]
    fn zone_detection_covers_halves_quarters_and_thirds() {
        let work = rect(0, 0, 3120, 1984);
        assert_eq!(detect_zone(rect(0, 0, 1560, 1984), work), Some("left-half"));
        assert_eq!(detect_zone(rect(1560, 0, 1560, 1984), work), Some("right-half"));
        assert_eq!(detect_zone(rect(0, 0, 1560, 992), work), Some("tl"));
        assert_eq!(detect_zone(rect(1560, 992, 1560, 992), work), Some("br"));
        assert_eq!(detect_zone(rect(0, 0, 1040, 1984), work), Some("left-third"));
        assert_eq!(detect_zone(rect(0, 0, 3120, 992), work), Some("top-half"));
        assert_eq!(detect_zone(rect(200, 200, 1200, 800), work), None);
    }

    #[test]
    fn zone_detection_tolerance_is_two_physical_pixels() {
        let work = rect(0, 0, 3120, 1984);
        // 偏移 2px 仍算贴靠（左边对齐 + 高度贴合工作区）；偏移 3px 不算
        assert_eq!(detect_zone(rect(2, 0, 1558, 1984), work), Some("left-half"));
        assert_eq!(detect_zone(rect(3, 0, 1557, 1984), work), None);
    }

    #[test]
    fn rounded_rect_excludes_corner_cutouts() {
        let content = RectF { left: 0.0, top: 0.0, right: 100.0, bottom: 100.0 };
        assert!(content.contains_rounded(50.0, 50.0, 20.0));
        assert!(!content.contains_rounded(1.0, 1.0, 20.0));
        assert!(content.contains_rounded(20.1, 0.5, 20.0));
    }

    #[test]
    fn hit_regions_sanitize_drops_invalid_rects() {
        let regions = HitRegions {
            titlebar: Some(RegionRect { x: f64::NAN, y: 0.0, w: 10.0, h: 10.0 }),
            max_button: Some(RegionRect { x: 0.0, y: 0.0, w: 10.0, h: 10.0 }),
            other_buttons: vec![
                RegionRect { x: 0.0, y: 0.0, w: 10.0, h: 10.0 },
                RegionRect { x: 0.0, y: 0.0, w: -1.0, h: 10.0 },
            ],
            interactive: vec![RegionRect { x: 0.0, y: 0.0, w: 10.0, h: 10.0 }],
            frame_padding: Some(-5.0),
            ..Default::default()
        }
        .sanitize();
        assert!(regions.titlebar.is_none());
        assert!(regions.max_button.is_some());
        assert_eq!(regions.other_buttons.len(), 1);
        assert_eq!(regions.interactive.len(), 1);
        assert!(regions.frame_padding.is_none());
    }

    #[test]
    fn hit_regions_deserialize_camel_case_with_interactive() {
        let raw = r#"{"dpr":2,"framePadding":56,"radius":40,"resizeBorder":8,"state":"normal",
            "titlebar":{"x":56,"y":56,"w":2288,"h":104},
            "maxButton":{"x":2160,"y":56,"w":92,"h":102},
            "otherButtons":[{"x":2068,"y":56,"w":92,"h":102}],
            "interactive":[{"x":1900,"y":56,"w":160,"h":104}],
            "cornerRadius":40}"#;
        let regions: HitRegions = serde_json::from_str(raw).unwrap();
        assert_eq!(regions.frame_padding, Some(56.0));
        assert_eq!(regions.max_button.unwrap().x, 2160.0);
        assert_eq!(regions.other_buttons.len(), 1);
        assert_eq!(regions.interactive.len(), 1);
        assert_eq!(regions.interactive[0].w, 160.0);
    }

    #[test]
    fn effective_padding_zero_when_maximized_or_snapped() {
        // 纯函数部分：state 字符串由 geometry 决定，这里只锁 content_rect 行为
        let client = rect(0, 0, 3120, 1984);
        let content = content_rect(client, 0.0);
        assert_eq!(content.left, 0.0);
        assert_eq!(content.right, 3120.0);
    }
}