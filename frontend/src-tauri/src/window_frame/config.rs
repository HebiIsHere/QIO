//! 窗口层启动配置：三模式选择、穿透模式、Windows 版本门与测试钩子。
//!
//! 规格来源：docs/.qio032-part1.md §0/§3.1、docs/.qio032-part3.md §3.1/§4.7。
//! 运行期不切换模式；所有选择只在启动时读一次并写一行 INFO 日志，便于验收复盘。

use std::env;

/// 窗口四周透明留白（CSS px，与前端 --window-frame-pad 联动）。
pub const FRAME_PADDING_CSS: f64 = 28.0;
/// 可见内容区圆角（CSS px，与前端 --window-radius 联动）。
pub const FRAME_RADIUS_CSS: f64 = 20.0;
/// 可见内容边内侧的缩放命中带宽度（物理 px，overlay 用）。
pub const RESIZE_BORDER_PHYSICAL: f64 = 8.0;
/// 可见内容区最小尺寸（CSS px）：窗口 minTrack = 内容 min × dpr + 2×padding 物理。
pub const CONTENT_MIN_CSS_WIDTH: f64 = 800.0;
pub const CONTENT_MIN_CSS_HEIGHT: f64 = 600.0;
/// Windows 11 首个带系统圆角的版本（Build 22000）。
pub const WINDOWS_11_MIN_BUILD: u32 = 22000;

/// 窗口框架模式（与 qio://window-state 的 frame 字段同一套取值）。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum FrameMode {
    /// 透明窗 + CSS 自绘 20px 圆角/阴影 + overlay 命中 + poll 穿透（0.3.2 默认）。
    Css,
    /// QIO 0.3.1 等价对照：不透明、系统圆角 + 系统阴影（回归用）。
    Dwm,
    /// 完整系统窗框（Win10 默认 / css 初始化失败回退）。
    Native,
}

impl FrameMode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Css => "css",
            Self::Dwm => "dwm",
            Self::Native => "native",
        }
    }

    fn parse(raw: Option<String>) -> Option<Self> {
        match raw.map(|v| v.trim().to_ascii_lowercase()).as_deref() {
            Some("css") => Some(Self::Css),
            Some("dwm") => Some(Self::Dwm),
            Some("native") => Some(Self::Native),
            _ => None,
        }
    }
}

/// 透明留白带的跨进程点击穿透方式。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum PassthroughMode {
    /// 关闭轮询：留白带点击被 WebView2 吃掉（与 0.3.1 一致）。
    None,
    /// 8ms 轮询光标，在可见圆角内容矩形之外给窗口加
    /// WS_EX_LAYERED|WS_EX_TRANSPARENT（并递归 WebView2 子窗），实现真实穿透。
    Poll,
}

impl PassthroughMode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::Poll => "poll",
        }
    }

    fn parse(raw: Option<String>) -> Option<Self> {
        match raw.map(|v| v.trim().to_ascii_lowercase()).as_deref() {
            Some("none") => Some(Self::None),
            Some("poll") => Some(Self::Poll),
            _ => None,
        }
    }
}

/// 生效的窗口层配置（一次启动内不可变）。
#[derive(Clone, Debug)]
pub struct FrameConfig {
    pub frame: FrameMode,
    pub passthrough: PassthroughMode,
    pub show_on_load: bool,
    pub ui_query: Option<String>,
    pub windows_build: u32,
    /// 强制 native 的原因（Win10 / css 初始化失败 / 测试钩子）；None 表示按环境变量选择。
    pub forced_native_reason: Option<String>,
    pub test_fail_css: bool,
}

impl FrameConfig {
    /// 读取环境变量 + 系统版本，决定本次启动的生效模式。
    ///
    /// 规则（spec §3.1）：
    /// * QIO_WINDOW_FRAME = css(默认)/dwm/native；
    /// * WindowsBuild < 22000 → 强制 native（Win10 无系统圆角，且透明窗合成行为不同）；
    /// * QIO_PASSTHROUGH = none/poll（默认 poll，仅 css 模式生效，其余强制 none）；
    /// * QIO_WINDOW_TEST_FAIL_CSS=1（仅测试）→ 模拟 css 初始化失败，走 native 回退。
    pub fn from_env() -> Self {
        let windows_build = windows_build_number();
        let requested = FrameMode::parse(env_value("QIO_WINDOW_FRAME"));
        let mut forced_reason: Option<String> = None;
        let mut frame = requested.unwrap_or(FrameMode::Css);
        if windows_build < WINDOWS_11_MIN_BUILD && frame == FrameMode::Css {
            forced_reason = Some(format!(
                "windows-build-{windows_build}-below-{WINDOWS_11_MIN_BUILD}"
            ));
            frame = FrameMode::Native;
        }
        let test_fail_css = env_value("QIO_WINDOW_TEST_FAIL_CSS")
            .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
            .unwrap_or(false);
        let passthrough = if frame == FrameMode::Css {
            PassthroughMode::parse(env_value("QIO_PASSTHROUGH")).unwrap_or(PassthroughMode::Poll)
        } else {
            PassthroughMode::None
        };
        Self {
            frame,
            passthrough,
            show_on_load: true,
            ui_query: env_value("QIO_UI_QUERY"),
            windows_build,
            forced_native_reason: forced_reason,
            test_fail_css,
        }
    }

    pub fn decorations(&self) -> bool {
        self.frame == FrameMode::Native
    }

    pub fn transparent(&self) -> bool {
        self.frame == FrameMode::Css
    }

    /// 0.3.2：只有 css 模式不要系统阴影（阴影由网页 CSS 画）；dwm/native 保留系统阴影。
    pub fn shadow(&self) -> bool {
        self.frame != FrameMode::Css
    }

    /// 是否无系统装饰（overlay 命中 / poll 穿透只在这种窗口上才有意义）。
    pub fn frameless(&self) -> bool {
        self.frame != FrameMode::Native
    }

    pub fn default_padding_css(&self) -> f64 {
        if self.transparent() {
            FRAME_PADDING_CSS
        } else {
            0.0
        }
    }

    pub fn default_radius_css(&self) -> f64 {
        if self.transparent() {
            FRAME_RADIUS_CSS
        } else {
            0.0
        }
    }

    /// 把「css 初始化失败」落成 native 回退配置（保留 Windows 版本门原因）。
    pub fn fallback_to_native(&self, reason: &str) -> Self {
        let mut next = self.clone();
        next.frame = FrameMode::Native;
        next.passthrough = PassthroughMode::None;
        next.forced_native_reason = Some(reason.to_string());
        next
    }

    /// 启动 INFO 日志用的单行摘要（spec §4.6 要求）。
    pub fn summary(&self, dpi: u32) -> String {
        let scale = dpi as f64 / 96.0;
        format!(
            "mode={} passthrough={} padCss={:.0} radiusCss={:.0} resizeBorderPx={:.0} dpi={} build={} framePadPx={:.0} radiusPx={:.0} showOnLoad={} forcedNative={}",
            self.frame.as_str(),
            self.passthrough.as_str(),
            self.default_padding_css(),
            self.default_radius_css(),
            RESIZE_BORDER_PHYSICAL,
            dpi,
            self.windows_build,
            self.default_padding_css() * scale,
            self.default_radius_css() * scale,
            self.show_on_load,
            self.forced_native_reason.as_deref().unwrap_or("-"),
        )
    }
}

fn env_value(name: &str) -> Option<String> {
    env::var(name)
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
}

/// 系统 Build 号（RtlGetVersion，不受应用兼容性清单的版本谎报影响）。
///
/// 拿不到时返回 WINDOWS_11_MIN_BUILD（视为现代系统）：检测失败不应把 Win11 用户强制
/// 退回 native，日志里会明确写下「版本探测失败，按 >=22000 处理」。
pub fn windows_build_number() -> u32 {
    #[cfg(windows)]
    {
        use windows_sys::Win32::System::LibraryLoader::{GetModuleHandleW, GetProcAddress};

        #[repr(C)]
        struct RtlOsVersionInfoW {
            size: u32,
            major: u32,
            minor: u32,
            build: u32,
            platform_id: u32,
            csd_version: [u16; 128],
        }

        unsafe {
            let ntdll: Vec<u16> = "ntdll.dll\0".encode_utf16().collect();
            let module = GetModuleHandleW(ntdll.as_ptr());
            if module.is_null() {
                log::warn!("[qio] window-frame 版本探测失败（ntdll 未加载），按 >=22000 处理");
                return WINDOWS_11_MIN_BUILD;
            }
            let Some(proc_addr) = GetProcAddress(module, b"RtlGetVersion\0".as_ptr()) else {
                log::warn!("[qio] window-frame 版本探测失败（RtlGetVersion 缺失），按 >=22000 处理");
                return WINDOWS_11_MIN_BUILD;
            };
            let rtl_get_version: unsafe extern "system" fn(*mut RtlOsVersionInfoW) -> i32 =
                std::mem::transmute(proc_addr);
            let mut info: RtlOsVersionInfoW = std::mem::zeroed();
            info.size = std::mem::size_of::<RtlOsVersionInfoW>() as u32;
            if rtl_get_version(&mut info) == 0 {
                return info.build;
            }
            log::warn!("[qio] window-frame 版本探测失败（RtlGetVersion 返回非 0），按 >=22000 处理");
            WINDOWS_11_MIN_BUILD
        }
    }
    #[cfg(not(windows))]
    {
        WINDOWS_11_MIN_BUILD
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_mode_parse_is_total_and_case_insensitive() {
        assert_eq!(FrameMode::parse(Some("CSS".into())), Some(FrameMode::Css));
        assert_eq!(FrameMode::parse(Some(" dwm ".into())), Some(FrameMode::Dwm));
        assert_eq!(FrameMode::parse(Some("native".into())), Some(FrameMode::Native));
        assert_eq!(FrameMode::parse(Some("bogus".into())), None);
        assert_eq!(FrameMode::parse(None), None);
    }

    #[test]
    fn passthrough_parse_only_accepts_documented_values() {
        assert_eq!(PassthroughMode::parse(Some("none".into())), Some(PassthroughMode::None));
        assert_eq!(PassthroughMode::parse(Some("POLL".into())), Some(PassthroughMode::Poll));
        assert_eq!(PassthroughMode::parse(Some("ht".into())), None);
        assert_eq!(PassthroughMode::parse(None), None);
    }

    #[test]
    fn fallback_to_native_clears_passthrough_and_keeps_reason() {
        let cfg = FrameConfig {
            frame: FrameMode::Css,
            passthrough: PassthroughMode::Poll,
            show_on_load: true,
            ui_query: None,
            windows_build: 26200,
            forced_native_reason: None,
            test_fail_css: false,
        };
        let fallback = cfg.fallback_to_native("builder-error");
        assert_eq!(fallback.frame, FrameMode::Native);
        assert_eq!(fallback.passthrough, PassthroughMode::None);
        assert_eq!(fallback.forced_native_reason.as_deref(), Some("builder-error"));
        let again = fallback.fallback_to_native("again");
        assert_eq!(again.forced_native_reason.as_deref(), Some("again"));
    }

    #[test]
    fn css_defaults_are_physical_scaled() {
        let cfg = FrameConfig {
            frame: FrameMode::Css,
            passthrough: PassthroughMode::Poll,
            show_on_load: true,
            ui_query: None,
            windows_build: 26200,
            forced_native_reason: None,
            test_fail_css: false,
        };
        assert_eq!(cfg.default_padding_css(), FRAME_PADDING_CSS);
        assert_eq!(cfg.default_radius_css(), FRAME_RADIUS_CSS);
        let summary = cfg.summary(192);
        assert!(summary.contains("mode=css"));
        assert!(summary.contains("framePadPx=56"));
        assert!(summary.contains("radiusPx=40"));
        assert!(summary.contains("passthrough=poll"));
    }

    #[test]
    fn non_css_modes_have_no_padding_or_radius() {
        for frame in [FrameMode::Dwm, FrameMode::Native] {
            let cfg = FrameConfig {
                frame,
                passthrough: PassthroughMode::None,
                show_on_load: true,
                ui_query: None,
                windows_build: 26200,
                forced_native_reason: None,
                test_fail_css: false,
            };
            assert_eq!(cfg.default_padding_css(), 0.0);
            assert_eq!(cfg.default_radius_css(), 0.0);
            assert_eq!(cfg.decorations(), frame == FrameMode::Native);
            assert_eq!(cfg.transparent(), frame == FrameMode::Css);
            assert!(cfg.shadow());
        }
    }
}
