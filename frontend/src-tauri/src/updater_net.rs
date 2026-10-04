//! 更新代理探测（壳侧）：按优先级读取并验证，整次探测有总预算。
//!
//! 三条硬约束（修复提示词 §1）：
//!
//! 1. **优先级**：环境变量 → `%APPDATA%\qio\updater-proxy.txt` → Windows 系统代理。
//!    高优先级**可用**时不再启动低优先级查询 —— `reg.exe` 在本机报过 0xC0000142 并挂住
//!    启动流程 1 分 42 秒，能不查就不查。
//! 2. **总预算**：系统命令、域名解析、多地址尝试与收尾共用同一个 deadline（[`PROBE_TOTAL_BUDGET`]），
//!    超预算就按「直连」收场，绝不无限等。
//! 3. **不改进程级代理环境变量**：`std::env::set_var` 是进程全局的，会追溯改掉别人正在进行的
//!    连接方式。一次更新的代理由 `qio_refresh_updater_proxy` 返回给前端，前端再按**操作**把它
//!    交给 updater 插件（JS `check({ proxy })` → `Update` 对象带着它进下载与安装）。
//!
//! 另外：解析只在**一个**受控解析线程上跑。同一时刻最多一次解析在飞；上一次没回来之前
//! 不再接新任务，超时也不新起线程 —— 不留越积越多的后台任务。

use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::sync::mpsc::{channel, Receiver, RecvTimeoutError, Sender};
use std::sync::{Mutex, MutexGuard, OnceLock};
use std::time::{Duration, Instant};

/// 整次代理探测的总预算：系统命令 + DNS + 多地址尝试 + 清理都在里面。
pub const PROBE_TOTAL_BUDGET: Duration = Duration::from_secs(5);
/// 单个候选的探测上限（也是该候选全部地址尝试的上限）。
pub const PROBE_PER_CANDIDATE_BUDGET: Duration = Duration::from_millis(1_200);
/// 单次 TCP 连接上限。
const CONNECT_TIMEOUT: Duration = Duration::from_millis(500);
/// 留给一次域名解析的最小预算：比这更少就直接判不可达，不浪费一次解析。
const DNS_MIN_BUDGET: Duration = Duration::from_millis(50);
/// 读系统代理（`reg query`）的单次上限。
///
/// 为什么必须有超时：`Command::output()` 会一直等子进程退出。2026-09-24 实测 ——
/// `reg.exe` 报 0xC0000142（DLL 初始化失败）并弹出"必须先点掉"的模态框，子进程
/// 因此不退出，外壳启动流程被钉住 1 分 42 秒，用户看到的就是一直白屏。
pub const SYSTEM_PROXY_QUERY_TIMEOUT: Duration = Duration::from_secs(3);

/// 代理配置的来源（进日志与返回值，便于排查"这次为什么走/不走代理"）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProxySource {
    Env,
    File,
    System,
    /// 明确不使用代理（直连）
    Direct,
}

impl ProxySource {
    pub fn label(self) -> &'static str {
        match self {
            ProxySource::Env => "环境变量",
            ProxySource::File => "updater-proxy.txt",
            ProxySource::System => "Windows 系统代理",
            ProxySource::Direct => "直连",
        }
    }
}

/// 一次探测的结论：**本次更新操作**要用哪个代理（`None` = 明确直连）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProxyPlan {
    pub proxy: Option<String>,
    pub source: ProxySource,
    pub detail: String,
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    // 解析线程/命令线程的 panic 不该让整次探测失去结论：中毒也继续用里面的值。
    mutex.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

// ---------------------------------------------------------------------------
// 有界 DNS 解析：全进程最多一个在飞的解析任务
// ---------------------------------------------------------------------------

/// 解析实现：测试可以换成假实现（默认走系统解析器）。
pub type ResolveFn = fn(&str) -> Vec<SocketAddr>;

fn default_resolve(host: &str) -> Vec<SocketAddr> {
    match (host, 0u16).to_socket_addrs() {
        // 端口 0 只是借 `(host, port)` 这个实现来取地址；端口在调用方补。
        Ok(addrs) => addrs.map(|addr| SocketAddr::new(addr.ip(), 0)).collect(),
        Err(_) => Vec::new(),
    }
}

struct ResolverInner {
    job_tx: Sender<(u64, String)>,
    done_rx: Receiver<(u64, Vec<SocketAddr>)>,
    /// 在飞任务的 id（None = 空闲）。超时后仍保持 Some，直到结果回来。
    inflight: Option<u64>,
    next_id: u64,
}

/// 受控解析器：同一时刻**最多一次**解析在飞。
///
/// 超时不等于放弃任务 —— 结果回来之前不再接受新任务（[`Self::resolve`] 返回 `None`），
/// 所以后台解析线程最多只有一个，不会越积越多。
pub struct BoundedResolver {
    inner: Mutex<ResolverInner>,
}

impl BoundedResolver {
    pub fn new() -> Self {
        Self::with_resolver(default_resolve)
    }

    pub fn with_resolver(resolve: ResolveFn) -> Self {
        let (job_tx, job_rx) = channel::<(u64, String)>();
        let (done_tx, done_rx) = channel::<(u64, Vec<SocketAddr>)>();
        std::thread::Builder::new()
            .name("qio-dns-resolve".to_string())
            .spawn(move || {
                while let Ok((id, host)) = job_rx.recv() {
                    let addrs = resolve(&host);
                    if done_tx.send((id, addrs)).is_err() {
                        break;
                    }
                }
            })
            .expect("拉起解析线程失败");
        Self {
            inner: Mutex::new(ResolverInner {
                job_tx,
                done_rx,
                inflight: None,
                next_id: 0,
            }),
        }
    }

    /// 在 `budget` 内解析主机。
    ///
    /// * 正常：`Some(地址列表)`；
    /// * 超时 / 上一次解析还没回来 / 解析失败：`None`（调用方按「不可达」处理）。
    pub fn resolve(&self, host: &str, budget: Duration) -> Option<Vec<SocketAddr>> {
        if host.trim().is_empty() || budget < DNS_MIN_BUDGET {
            return None;
        }
        let mut inner = lock(&self.inner);
        // 上一次超时任务的结果可能已经回来了：先收掉，才能腾出唯一的在飞名额。
        while let Ok((id, _)) = inner.done_rx.try_recv() {
            if inner.inflight == Some(id) {
                inner.inflight = None;
            }
        }
        if inner.inflight.is_some() {
            log::warn!("[qio] 上一次域名解析还没回来，本次按「不可达」处理（不起新任务）");
            return None;
        }
        let id = inner.next_id;
        inner.next_id += 1;
        if inner.job_tx.send((id, host.to_string())).is_err() {
            return None;
        }
        inner.inflight = Some(id);
        match inner.done_rx.recv_timeout(budget) {
            Ok((got_id, addrs)) => {
                inner.inflight = None;
                if got_id == id {
                    if addrs.is_empty() {
                        None
                    } else {
                        Some(addrs)
                    }
                } else {
                    None
                }
            }
            Err(RecvTimeoutError::Timeout) => None,
            Err(RecvTimeoutError::Disconnected) => {
                inner.inflight = None;
                None
            }
        }
    }
}

impl Default for BoundedResolver {
    fn default() -> Self {
        Self::new()
    }
}

/// 生产环境的唯一解析器：整个进程只有一个解析线程（可能停在一个卡住的 DNS 上），
/// 但绝不会因为多次探测而累积出第二个。
pub fn shared_resolver() -> &'static BoundedResolver {
    static SHARED: OnceLock<BoundedResolver> = OnceLock::new();
    SHARED.get_or_init(BoundedResolver::new)
}

// ---------------------------------------------------------------------------
// 纯逻辑：候选优先级 + 总预算
// ---------------------------------------------------------------------------

/// 按优先级逐个验证候选；**高优先级可用就不再问低优先级**。
///
/// `system_lookup` 是惰性求值的（只有前面的候选都不行、且预算还在时才调用），
/// 这正是「已有可用高优先级配置时不再启动低优先级系统查询」的落点。
pub fn plan_proxy<G, F>(
    deadline: Instant,
    candidates: Vec<(ProxySource, String)>,
    system_lookup: G,
    probe: F,
) -> ProxyPlan
where
    G: FnOnce(Instant) -> Option<String>,
    F: Fn(&str, Instant) -> bool,
{
    for (source, proxy) in candidates {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return direct_plan("预算用尽，未再尝试候选代理");
        }
        let candidate_deadline = Instant::now() + remaining.min(PROBE_PER_CANDIDATE_BUDGET);
        if probe(&proxy, candidate_deadline) {
            log::info!("[qio] 更新走代理（来源：{}）：{proxy}", source.label());
            return ProxyPlan {
                proxy: Some(proxy),
                source,
                detail: format!("{} 的代理可用", source.label()),
            };
        }
        log::warn!("[qio] 代理不可达，跳过（来源：{}）：{proxy}", source.label());
    }

    // 走到这里才允许碰系统代理（`reg query` 可能很慢，甚至挂住）
    if Instant::now() >= deadline {
        return direct_plan("预算用尽，未查询系统代理");
    }
    if let Some(proxy) = system_lookup(deadline) {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if !remaining.is_zero() {
            let candidate_deadline = Instant::now() + remaining.min(PROBE_PER_CANDIDATE_BUDGET);
            if probe(&proxy, candidate_deadline) {
                log::info!("[qio] 更新走代理（来源：Windows 系统代理）：{proxy}");
                return ProxyPlan {
                    proxy: Some(proxy),
                    source: ProxySource::System,
                    detail: "Windows 系统代理可用".to_string(),
                };
            }
            log::warn!("[qio] 系统代理不可达，按直连处理：{proxy}");
        }
    }
    direct_plan("没有可用代理")
}

fn direct_plan(reason: &str) -> ProxyPlan {
    log::info!("[qio] 本次更新不使用代理（直连）：{reason}");
    ProxyPlan {
        proxy: None,
        source: ProxySource::Direct,
        detail: reason.to_string(),
    }
}

// ---------------------------------------------------------------------------
// 候选来源
// ---------------------------------------------------------------------------

/// 环境变量里的代理（用户或别的程序显式设置的，优先级最高）。
pub fn env_proxy() -> Option<String> {
    std::env::var("HTTPS_PROXY")
        .or_else(|_| std::env::var("https_proxy"))
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

/// `%APPDATA%\qio\updater-proxy.txt` 里的代理（用户为 QIO 单独写的）。
pub fn file_proxy(raw: Option<String>) -> Option<String> {
    raw.map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

/// 生产入口：读环境变量与用户文件，按优先级 + 总预算决定这次更新用哪个代理。
///
/// 返回值交给前端（`qio_refresh_updater_proxy`），前端再按**操作**带给 updater 插件；
/// 这里不改任何进程级环境变量。
pub fn resolve_updater_proxy(proxy_file: Option<String>) -> ProxyPlan {
    let deadline = Instant::now() + PROBE_TOTAL_BUDGET;
    let mut candidates: Vec<(ProxySource, String)> = Vec::new();
    if let Some(proxy) = env_proxy() {
        candidates.push((ProxySource::Env, proxy));
    }
    if let Some(proxy) = file_proxy(proxy_file) {
        candidates.push((ProxySource::File, proxy));
    }
    let resolver = shared_resolver();
    plan_proxy(
        deadline,
        candidates,
        |system_deadline| system_proxy(system_deadline),
        |proxy, probe_deadline| probe_proxy(proxy, probe_deadline, resolver),
    )
}

/// 读 Windows 的「系统代理」设置（浏览器读的就是这一份）。
///
/// 为什么需要它：更新用的 HTTP 客户端只认环境变量与系统设置；而绝大多数 VPN
/// （Clash / v2rayN / Shadowsocks 等）在"系统代理模式"下只写这份设置。
/// 用 `reg query` 而不是引入注册表库：少一个依赖，输出格式稳定，解析失败就当"没有"。
/// PAC（自动配置脚本）不在这里处理：无法在不解释脚本的前提下判断该走哪个代理。
pub fn system_proxy(deadline: Instant) -> Option<String> {
    #[cfg(not(windows))]
    {
        let _ = deadline;
        None
    }
    #[cfg(windows)]
    {
        const KEY: &str = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings";
        let budget = deadline
            .saturating_duration_since(Instant::now())
            .min(SYSTEM_PROXY_QUERY_TIMEOUT);
        if budget.is_zero() {
            log::warn!("[qio] 系统代理查询的预算已用尽，跳过");
            return None;
        }
        let query = |name: &str| -> Option<String> {
            let mut cmd = std::process::Command::new("reg");
            cmd.args(["query", KEY, "/v", name]);
            let left = deadline
                .saturating_duration_since(Instant::now())
                .min(SYSTEM_PROXY_QUERY_TIMEOUT);
            if left.is_zero() {
                return None;
            }
            let out = qio_core::ownership::run_command_with_timeout(&mut cmd, left)?;
            let text = String::from_utf8_lossy(&out.stdout).to_string();
            let line = text
                .lines()
                .find(|line| line.contains("REG_SZ") || line.contains("REG_DWORD"))?;
            let value = line
                .split_once("REG_SZ")
                .map(|(_, value)| value)
                .or_else(|| line.split_once("REG_DWORD").map(|(_, value)| value))?;
            Some(value.trim().to_string())
        };
        let enabled = query("ProxyEnable")
            .map(|value| value.trim_start_matches("0x").trim() == "1" || value == "1")
            .unwrap_or(false);
        if !enabled {
            log::info!("[qio] 系统代理未启用");
            return None;
        }
        let raw = query("ProxyServer")?;
        pick_proxy_from_windows_value(&raw)
    }
}

/// Windows 的 ProxyServer 可能是 `host:port`，也可能是
/// `http=h:p;https=h:p;socks=h:p`。我们要发的是 HTTPS 请求，取值优先 https → http → 单个地址。
/// socks 需要客户端支持 socks，这里不当作可用（宁可直连，也不给一个用不了的值）。
pub fn pick_proxy_from_windows_value(raw: &str) -> Option<String> {
    let raw = raw.trim();
    if raw.is_empty() {
        return None;
    }
    let mut https = None;
    let mut http = None;
    let mut single = None;
    for part in raw.split(';') {
        let part = part.trim();
        if part.is_empty() {
            continue;
        }
        match part.split_once('=') {
            Some((scheme, value)) => match scheme.trim().to_ascii_lowercase().as_str() {
                "https" => https = Some(value.trim().to_string()),
                "http" => http = Some(value.trim().to_string()),
                "socks" | "socks5" => {}
                _ => {}
            },
            None => single = Some(part.to_string()),
        }
    }
    let chosen = https.or(http).or(single)?;
    if chosen.is_empty() {
        return None;
    }
    if chosen.contains("://") {
        Some(chosen)
    } else {
        Some(format!("http://{chosen}"))
    }
}

// ---------------------------------------------------------------------------
// 可达性验证：DNS（有界）+ 多地址尝试（有界）
// ---------------------------------------------------------------------------

/// 把代理地址拆成 `(host, port)`；拆不出可用主机就返回 None。
///
/// 空主机名不是地址（`:80` 会被系统解析成任意地址，不能当可达）。
pub fn split_proxy_host_port(proxy: &str) -> Option<(String, u16)> {
    let trimmed = proxy.trim();
    let without_scheme = trimmed
        .split_once("://")
        .map(|(_, rest)| rest)
        .unwrap_or(trimmed)
        .trim_end_matches('/');
    let host_port = without_scheme.split('/').next().unwrap_or(without_scheme);
    if host_port.is_empty() || host_port.starts_with(':') {
        return None;
    }
    let default_port = if trimmed.starts_with("https://") { 443 } else { 80 };
    // IPv6 字面量：只认 [::1]:port 这种带方括号的写法，简单可靠
    if let Some(rest) = host_port.strip_prefix('[') {
        let (host, tail) = rest.split_once(']')?;
        let port = tail
            .strip_prefix(':')
            .and_then(|value| value.parse::<u16>().ok())
            .unwrap_or(default_port);
        if host.is_empty() {
            return None;
        }
        return Some((host.to_string(), port));
    }
    let (host, port) = match host_port.rsplit_once(':') {
        Some((host, port)) => (host, port.parse::<u16>().ok()?),
        None => (host_port, default_port),
    };
    if host.is_empty() {
        return None;
    }
    Some((host.to_string(), port))
}

/// 用之前先确认这个代理地址真的有人监听（在 `deadline` 内）。
///
/// 动机（2026-09-22 真实故障）：系统里留着一个指向"已经关掉的代理"的地址时，
/// 客户端不会自己发现，只会一直等 —— 表现就是"卡住"或"网络错误"。
/// 这里用一次极短的 TCP 连接判断可达性：不可达就不用它，改为直连。
/// 域名解析走 [`BoundedResolver`]（有界、不积压），多地址逐个尝试，全部受 deadline 约束。
pub fn probe_proxy(proxy: &str, deadline: Instant, resolver: &BoundedResolver) -> bool {
    let Some((host, port)) = split_proxy_host_port(proxy) else {
        return false;
    };
    let remaining = deadline.saturating_duration_since(Instant::now());
    if remaining < DNS_MIN_BUDGET {
        return false;
    }
    let Some(resolved) = resolver.resolve(&host, remaining) else {
        return false;
    };
    let addrs: Vec<SocketAddr> = resolved
        .into_iter()
        .map(|addr| SocketAddr::new(addr.ip(), port))
        .collect();
    for addr in addrs {
        let left = deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            return false;
        }
        if TcpStream::connect_timeout(&addr, left.min(CONNECT_TIMEOUT)).is_ok() {
            return true;
        }
    }
    false
}

// ---------------------------------------------------------------------------
// 为什么不用「进程级代理环境变量」传递这次操作的代理
// ---------------------------------------------------------------------------
//
// 旧实现每次检查更新前 `std::env::set_var("HTTPS_PROXY", ...)`。那是进程全局的：
// 两个并发操作会互相改掉对方的连接方式，**已经开始**的操作也会被追溯影响。
// 现在：`qio_refresh_updater_proxy` 把结论返回给前端，前端按**操作**把它交给 updater 插件
// （JS `check({ proxy })` → `Update` 对象带着它进下载与安装）。壳这边只在启动时写入一次
// `NO_PROXY=*`（[`install_direct_fallback_guard`]），保证「直连」结论真的生效。

/// 让「直连」这个结论真的生效：把 `NO_PROXY` 固定成 `*`。
///
/// 为什么需要它：tauri-plugin-updater 默认开启 reqwest 的 `system-proxy`，而
/// `hyper-util` 的 `Matcher::from_system()` 在 Windows 上会**直接读系统代理设置**
/// （`#[cfg(all(feature = "client-proxy-system", windows))] win::with_system(&mut builder)`）。
/// 也就是说：只清环境变量并不能阻止客户端去连一个已经死掉的系统代理。
/// `NO_PROXY=*` 会让 `Matcher::intercept()` 对任何主机都返回「不代理」，于是
/// 「探测结论 = 直连」时请求真的走直连。
///
/// 这不是「按操作改进程级代理变量」：它是一次性写入的**常量**，不参与每次操作的决策；
/// 需要走代理时，代理地址是按操作显式传给插件的（`check({ proxy })`，
/// 而 `reqwest::ClientBuilder::proxy()` 会把 `auto_sys_proxy` 关掉，与这个常量无关）。
/// 后端子进程会显式清掉这两个变量（见 `backend_launch`），不把壳的更新策略泄漏给后端。
pub fn install_direct_fallback_guard() {
    std::env::set_var("NO_PROXY", "*");
    std::env::set_var("no_proxy", "*");
    log::info!(
        "[qio] 已把 NO_PROXY 固定为 *：壳按操作显式决定代理，不让底层客户端自动挑系统代理"
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reachable_marker() -> impl Fn(&str, Instant) -> bool {
        |proxy: &str, _deadline: Instant| proxy.contains("good")
    }

    #[test]
    fn a_usable_high_priority_proxy_skips_the_system_query() {
        // 修复提示词 §1：已有可用高优先级配置就不再启动低优先级系统查询。
        let calls = std::sync::atomic::AtomicUsize::new(0);
        let plan = plan_proxy(
            Instant::now() + PROBE_TOTAL_BUDGET,
            vec![(ProxySource::Env, "http://good-proxy:7890".to_string())],
            |_| {
                calls.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                Some("http://127.0.0.1:17011".to_string())
            },
            reachable_marker(),
        );
        assert_eq!(plan.source, ProxySource::Env);
        assert_eq!(plan.proxy.as_deref(), Some("http://good-proxy:7890"));
        assert_eq!(
            calls.load(std::sync::atomic::Ordering::SeqCst),
            0,
            "高优先级可用时不该去查系统代理"
        );
    }

    #[test]
    fn an_unreachable_high_priority_proxy_falls_through_to_the_system_query() {
        let plan = plan_proxy(
            Instant::now() + PROBE_TOTAL_BUDGET,
            vec![(ProxySource::Env, "http://127.0.0.1:17011".to_string())],
            |_| Some("http://good-system-proxy:7890".to_string()),
            reachable_marker(),
        );
        assert_eq!(plan.source, ProxySource::System);
        assert_eq!(plan.proxy.as_deref(), Some("http://good-system-proxy:7890"));
    }

    #[test]
    fn exhausted_budget_becomes_direct_without_any_more_work() {
        // deadline 已经过去：一个候选都不试，也绝不启动系统查询。
        let probed = std::sync::atomic::AtomicUsize::new(0);
        let plan = plan_proxy(
            Instant::now(),
            vec![(ProxySource::Env, "http://good-proxy:7890".to_string())],
            |_| panic!("预算用尽时不该查询系统代理"),
            |_proxy: &str, _deadline: Instant| {
                probed.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                true
            },
        );
        assert_eq!(plan.source, ProxySource::Direct);
        assert!(plan.proxy.is_none());
        assert_eq!(probed.load(std::sync::atomic::Ordering::SeqCst), 0);
    }

    #[test]
    fn no_candidate_at_all_means_direct() {
        let plan = plan_proxy(
            Instant::now() + PROBE_TOTAL_BUDGET,
            Vec::new(),
            |_| None,
            reachable_marker(),
        );
        assert_eq!(plan.source, ProxySource::Direct);
        assert!(plan.proxy.is_none());
    }

    #[test]
    fn file_proxy_is_trimmed_and_empty_values_are_ignored() {
        assert_eq!(file_proxy(Some("  http://a:1 \n".to_string())).as_deref(), Some("http://a:1"));
        assert_eq!(file_proxy(Some("   ".to_string())), None);
        assert_eq!(file_proxy(None), None);
    }

    #[test]
    fn proxy_address_is_split_into_host_and_port() {
        assert_eq!(
            split_proxy_host_port("http://127.0.0.1:17011"),
            Some(("127.0.0.1".to_string(), 17011))
        );
        assert_eq!(
            split_proxy_host_port("https://proxy.example.com"),
            Some(("proxy.example.com".to_string(), 443))
        );
        assert_eq!(
            split_proxy_host_port("proxy.example.com"),
            Some(("proxy.example.com".to_string(), 80))
        );
        assert_eq!(
            split_proxy_host_port("http://[::1]:8080"),
            Some(("::1".to_string(), 8080))
        );
        // 空主机名不是地址（":80" 会被系统解析成任意地址）
        assert_eq!(split_proxy_host_port("http://"), None);
        assert_eq!(split_proxy_host_port("http://:8080"), None);
        assert_eq!(split_proxy_host_port("   "), None);
    }

    #[test]
    fn picks_https_then_http_from_windows_proxy_value() {
        // 多段形式：优先 https
        assert_eq!(
            pick_proxy_from_windows_value("http=127.0.0.1:17011;https=127.0.0.1:17012").as_deref(),
            Some("http://127.0.0.1:17012")
        );
        // 只有 http 段
        assert_eq!(
            pick_proxy_from_windows_value("http=127.0.0.1:7890").as_deref(),
            Some("http://127.0.0.1:7890")
        );
        // 单地址形式（不带协议）自动补 http://
        assert_eq!(
            pick_proxy_from_windows_value("127.0.0.1:8080").as_deref(),
            Some("http://127.0.0.1:8080")
        );
        // 已带协议则原样保留
        assert_eq!(
            pick_proxy_from_windows_value("http://127.0.0.1:8080").as_deref(),
            Some("http://127.0.0.1:8080")
        );
    }

    #[test]
    fn ignores_socks_only_and_empty_values() {
        // 只给了 socks：不当作可用（客户端不支持时不猜），也不会拼出一个错地址
        assert_eq!(pick_proxy_from_windows_value("socks=127.0.0.1:1080"), None);
        assert_eq!(pick_proxy_from_windows_value("   "), None);
    }

    #[test]
    fn reachability_detects_dead_port() {
        // 本机上没人监听的端口：必须判为不可达，否则会"一直等"
        let resolver = BoundedResolver::new();
        let deadline = Instant::now() + PROBE_PER_CANDIDATE_BUDGET;
        assert!(!probe_proxy("http://127.0.0.1:17011", deadline, &resolver));
        // 语法都不成立的地址同样不可达（不会走到解析）
        assert!(!probe_proxy("http://", deadline, &resolver));
    }

    #[test]
    fn reachability_accepts_a_listening_port() {
        // 自己起一个监听端口：探测必须判为可达（不是"永远不可达"）
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let resolver = BoundedResolver::new();
        let deadline = Instant::now() + PROBE_PER_CANDIDATE_BUDGET;
        assert!(probe_proxy(
            &format!("http://127.0.0.1:{port}"),
            deadline,
            &resolver
        ));
    }

    #[test]
    fn resolver_does_not_stack_tasks_when_one_is_still_in_flight() {
        fn slow(_host: &str) -> Vec<SocketAddr> {
            std::thread::sleep(Duration::from_millis(400));
            vec![SocketAddr::from(([127, 0, 0, 1], 0))]
        }
        let resolver = BoundedResolver::with_resolver(slow);

        // 第一次：预算比解析还短 → 超时（任务仍在飞）
        assert!(resolver
            .resolve("example.invalid", Duration::from_millis(60))
            .is_none());

        // 第二次（立刻）：不许再起一个任务，必须快速返回 None
        let started = Instant::now();
        assert!(resolver
            .resolve("example.invalid", Duration::from_millis(500))
            .is_none());
        assert!(
            started.elapsed() < Duration::from_millis(250),
            "第二次解析不该等待一个新的（或旧的）任务：{:?}",
            started.elapsed()
        );

        // 旧任务回来之后：名额释放，下一次解析正常成功（最多只有一个解析线程）
        std::thread::sleep(Duration::from_millis(450));
        assert!(resolver
            .resolve("localhost", Duration::from_millis(500))
            .is_some());
    }

    #[test]
    fn resolver_returns_addresses_for_numeric_hosts() {
        let resolver = BoundedResolver::new();
        let addrs = resolver
            .resolve("127.0.0.1", Duration::from_millis(500))
            .expect("数字地址不该需要真的查 DNS");
        assert!(addrs.iter().any(|addr| addr.ip().to_string() == "127.0.0.1"));
    }

    #[test]
    fn direct_plans_carry_a_reason_for_the_log() {
        // 「直连」不是一个空洞的默认值：日志里要能看出为什么（预算用尽 / 都不可达）
        let plan = plan_proxy(
            Instant::now(),
            Vec::new(),
            |_| panic!("预算用尽时不该查询系统代理"),
            |_proxy: &str, _deadline: Instant| true,
        );
        assert_eq!(plan.source, ProxySource::Direct);
        assert!(plan.detail.contains("预算用尽"), "结论要带上原因：{}", plan.detail);
    }
}
