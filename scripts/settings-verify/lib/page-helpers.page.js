/**
 * 页面内探针（注入到被验收页面）。这里只做「读事实 / 点可见控件」，
 * 不依赖具体类名：优先 data-settings-* 钩子，其次仓库既有类名与中文标签文本。
 *
 * 为什么按「中文标签文本」定位控件：基线 8 个 tab 与本轮 5 类的 DOM 结构会变，
 * 但「工作区根目录」「默认返回条数」「免密钥联网搜索」这些用户可见名称是迁移清单
 * 里明确保留的，按标签找比按 nth-child 稳。定位失败如实返回 found:false，
 * 由上层记为「未接通 / 无法验证」，不假装通过。
 */
window.__stg = (() => {
  const norm = (value) => String(value == null ? "" : value).replace(/[\s\u3000]+/g, "");

  function visible(el) {
    if (!el || !(el instanceof Element)) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const style = getComputedStyle(el);
    return style.visibility !== "hidden" && style.display !== "none" && style.opacity !== "0";
  }

  function root() {
    return document.querySelector("[data-settings-page], .qio-settings, [data-settings-root], .settings, main[aria-label='设置']");
  }

  function desc(el) {
    if (!el || !(el instanceof Element)) return null;
    const cls = typeof el.className === "string" ? el.className : (el.getAttribute("class") || "");
    const shortClass = cls.split(/\s+/).filter(Boolean).slice(0, 4).join(".");
    const text = norm(el.innerText || el.textContent || "").slice(0, 28);
    return {
      tag: el.tagName.toLowerCase(),
      cls: shortClass,
      id: el.id || "",
      role: el.getAttribute("role") || "",
      aria: el.getAttribute("aria-label") || "",
      type: el.getAttribute("type") || "",
      text,
    };
  }

  function rectOf(el) {
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height, right: rect.right, bottom: rect.bottom, top: rect.top, left: rect.left };
  }

  /** 装饰性覆盖层（自绘滚动条等）不参与圆角/横向滚动的界面组件判定。 */
  function isOrnament(el) {
    if (!el || !(el instanceof Element)) return false;
    const cls = typeof el.className === "string" ? el.className : "";
    if (/qio-scroll-(host|rail|thumb)|sr-only/i.test(cls)) return true;
    if (el.getAttribute("aria-hidden") === "true" && !el.innerText) return true;
    return false;
  }

  function ownText(el) {
    let out = "";
    for (const node of el.childNodes) if (node.nodeType === 3) out += node.nodeValue;
    return norm(out);
  }

  function allElements() {
    const base = root();
    if (!base) return [];
    return [base, ...base.querySelectorAll("*")];
  }

  /**
   * 标签候选按「像不像标签」排序，避免把说明文字里的提及当成控件标签：
   * 基线「联网搜索」的说明里就有「自建 SearXNG」，只按包含匹配会把描述段落当成标签，
   * 于是把输入写进了同一节里的另一个数字框（真实踩过）。
   */
  function labelCandidates(label) {
    const want = norm(label);
    if (!want) return [];
    const scored = [];
    for (const el of allElements()) {
      if (!visible(el)) continue;
      const own = ownText(el);
      const full = norm(el.innerText || el.textContent || "");
      const hit = own.includes(want) ? 0 : full.includes(want) ? 1 : -1;
      if (hit < 0 || full.length > 160) continue;
      let score = hit === 0 ? -100 : 0;
      const labelLike = ".t, .set-row__title, .label, [data-settings-label], dt, label, legend, th, .qio-field-label, .title, .set-section__head h2, h2, h3, h4, summary, .setting-title, .field-label";
      const proseLike = ".desc, .d, p, .hint, .mode-hint, .avail, .note";
      if (el.matches(labelLike)) score -= 70;
      if (el.matches(proseLike)) score += 90;
      if (full.length <= 24) score -= 20;
      score += full.length / 10;
      scored.push({ el, score, text: full });
    }
    scored.sort((a, b) => a.score - b.score);
    return scored;
  }

  function labelElement(label) {
    const list = labelCandidates(label);
    return list.length ? list[0].el : null;
  }

  const CONTROL_SELECTOR = "input:not([type=hidden]), textarea, select, [role=switch], [role=combobox], [role=slider], .qio-select, .qio-number, .qio-switch, button";
  const ROW_SELECTOR = "[data-settings-row], .set-row, .pref, .setting-row, .qio-field, .form-row, .field, .row, .control-row";

  function rowFor(labelEl) {
    let node = labelEl;
    for (let i = 0; i < 9 && node; i += 1) {
      if (node.matches && node.matches(ROW_SELECTOR)) {
        const controls = [...node.querySelectorAll(CONTROL_SELECTOR)].filter(visible);
        if (controls.length) return node;
      }
      node = node.parentElement;
    }
    node = labelEl;
    for (let i = 0; i < 9 && node; i += 1) {
      const controls = [...node.querySelectorAll(CONTROL_SELECTOR)].filter(visible);
      if (controls.length) return node;
      node = node.parentElement;
    }
    return labelEl ? labelEl.parentElement : null;
  }

  function controlKind(el) {
    if (!el) return "none";
    if (el.matches("input")) {
      if (el.type === "checkbox") return "checkbox";
      if (el.type === "number") return "native-number";
      if (el.type === "password") return "password";
      return "input";
    }
    if (el.matches("textarea")) return "textarea";
    if (el.matches("select")) return "native-select";
    if (el.matches("[role=switch], .qio-switch")) return "switch";
    if (el.matches("[role=combobox], .qio-select")) return "select";
    if (el.matches("summary")) return "summary";
    if (el.matches("button")) return "button";
    return "other";
  }

  function controlValue(el) {
    if (!el) return null;
    if (el.matches("input, textarea, select")) return el.value;
    if (el.matches("[role=switch]")) return el.getAttribute("aria-checked");
    const inner = el.querySelector("input, [data-value], .value, .qio-select-value");
    if (inner && "value" in inner) return inner.value;
    return norm(el.innerText || el.textContent || "");
  }

  function findControl(label) {
    const candidates = labelCandidates(label);
    if (!candidates.length) return { found: false, reason: "label-not-found", label };
    let labelEl = null;
    let row = null;
    let controls = [];
    for (const candidate of candidates) {
      const candidateRow = rowFor(candidate.el);
      if (!candidateRow) continue;
      const candidateControls = [...candidateRow.querySelectorAll(CONTROL_SELECTOR)].filter(visible);
      if (!candidateControls.length) continue;
      labelEl = candidate.el;
      row = candidateRow;
      controls = candidateControls;
      break;
    }
    if (!labelEl) return { found: false, reason: "control-not-found", label, candidates: candidates.slice(0, 5).map((item) => item.text) };
    const rank = (kind) => {
      const map = { input: 0, textarea: 1, "native-number": 0, password: 0, switch: 2, "native-select": 3, select: 3, button: 4 };
      return kind in map ? map[kind] : 9;
    };
    controls.sort((a, b) => rank(controlKind(a)) - rank(controlKind(b)));
    const el = controls[0];
    window.__stg._last = el;
    return {
      found: true,
      kind: controlKind(el),
      value: controlValue(el),
      checked: el.getAttribute("aria-checked"),
      disabled: !!(el.disabled || el.getAttribute("aria-disabled") === "true"),
      rect: rectOf(el),
      row: desc(row),
      control: desc(el),
      labelEl: desc(labelEl),
    };
  }

  /** 往「密码/Key 类」输入框写值（录屏用：只写假 Key，不提交）。 */
  function fillPasswordLike(value) {
    const list = [...document.querySelectorAll("input[type=password], input[aria-label*='Key'], input[placeholder*='Key'], input[placeholder*='key']")].filter(visible);
    if (!list.length) return { ok: false, reason: "no-input" };
    const el = list[0];
    el.focus();
    fireInput(el, value);
    return { ok: true, id: el.getAttribute("aria-label") || el.className };
  }

  function fireInput(el, value) {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, String(value));
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function setControl(label, value) {
    const info = findControl(label);
    if (!info.found) return info;
    const el = window.__stg._last;
    if (el.matches("input, textarea")) {
      el.focus();
      fireInput(el, value);
      el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
      el.dispatchEvent(new KeyboardEvent("keyup", { key: "Enter", bubbles: true }));
      el.blur();
      return { found: true, kind: info.kind, set: true, value: el.value, control: desc(el) };
    }
    if (el.matches("select")) {
      el.value = value;
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { found: true, kind: "native-select", set: true, value: el.value };
    }
    return { found: true, kind: info.kind, set: false, reason: "unsupported-control", control: desc(el) };
  }

  function menuOptions() {
    return [...document.querySelectorAll("[role=option], .qio-select-menu .opt, .qio-select-menu [role=option], [data-qio-option], .qio-combo .opt")].filter(visible);
  }

  function clickOptionText(text) {
    const want = norm(text);
    const options = menuOptions();
    const hit = options.find((el) => norm(el.innerText || el.textContent || "") === want) ||
      options.find((el) => norm(el.innerText || el.textContent || "").includes(want));
    if (!hit) return { ok: false, reason: "option-not-found", seen: options.slice(0, 12).map((el) => norm(el.innerText || el.textContent || "")) };
    hit.click();
    return { ok: true, text: norm(hit.innerText || hit.textContent || "") };
  }

  function openSelect(label) {
    const info = findControl(label);
    if (!info.found) return info;
    const el = window.__stg._last;
    if (el.matches("[role=combobox], .qio-select")) el.click();
    else if (el.matches("button")) el.click();
    else return { found: true, opened: false, reason: "not-a-select", kind: info.kind };
    return { found: true, opened: true, kind: info.kind };
  }

  function clickLabel(label, opts) {
    const want = norm(label);
    const exact = !!(opts && opts.exact);
    const tags = "button, a, [role=switch], [role=tab], summary, [role=button], .qio-btn, .qio-switch, .tab, label, [data-settings-action]";
    const candidates = [...document.querySelectorAll(tags)].filter(visible).map((el) => ({
      el,
      text: norm(el.innerText || el.textContent || ""),
      aria: norm(el.getAttribute("aria-label") || ""),
    }));
    let hit = null;
    for (const item of candidates) {
      const hitText = exact ? item.text === want : item.text.includes(want);
      const hitAria = exact ? item.aria === want : item.aria.includes(want);
      if (hitText || hitAria) {
        if (!hit || item.text.length < hit.text.length) hit = item;
      }
    }
    if (!hit) return { ok: false, reason: "not-found", label };
    hit.el.scrollIntoView({ block: "center" });
    hit.el.click();
    return { ok: true, label, control: desc(hit.el) };
  }

  function clickSwitch(label) {
    const info = findControl(label);
    if (!info.found) return info;
    const el = window.__stg._last;
    if (!el.matches("[role=switch], .qio-switch")) return { found: true, ok: false, reason: "not-a-switch", kind: info.kind };
    el.click();
    return { found: true, ok: true, before: info.checked, control: desc(el) };
  }

  function categories() {
    const nav = document.querySelector("[data-settings-nav], nav[role=tablist], .settings-nav, .settings-tabs, .nav");
    if (!nav) return [];
    // 优先语义 tab（导航里还可能有「返回 / 搜索」图标按钮，按 button 全收会把它们当成分类）
    let items = [...nav.querySelectorAll("[data-settings-nav-tab], [role=tab]")].filter(visible);
    if (!items.length) {
      items = [...nav.querySelectorAll("[data-settings-category], button, a")].filter(visible)
        .filter((el) => norm(el.innerText || el.textContent || "").length > 0);
    }
    return items.map((el) => ({
      label: norm(el.innerText || el.textContent || ""),
      id: el.getAttribute("data-settings-category") || el.getAttribute("data-category") || "",
      active: el.getAttribute("aria-selected") === "true" || el.classList.contains("active") || el.getAttribute("aria-current") === "page",
      rect: rectOf(el),
      el: desc(el),
    }));
  }

  function activeCategory() {
    const hit = categories().find((item) => item.active);
    return hit ? hit.label : "";
  }

  function navInfo() {
    const nav = document.querySelector("[data-settings-nav], nav[role=tablist], .settings-nav, .settings-tabs, .nav");
    const content = document.querySelector("[data-settings-content], .set-panels, [data-testid=settings-panels], .panels, .settings-content, .settings-panels");
    if (!nav) return { found: false };
    const navRect = nav.getBoundingClientRect();
    const contentRect = content ? content.getBoundingClientRect() : null;
    const style = getComputedStyle(nav);
    let mode = "unknown";
    if (contentRect) {
      if (navRect.right <= contentRect.left + 6) mode = "side";
      else if (navRect.bottom <= contentRect.top + 6) mode = "top";
      else mode = "overlap";
    }
    const rootEl = root();
    return {
      found: true,
      mode,
      layoutAttr: rootEl ? rootEl.getAttribute("data-nav-layout") : null,
      navAttr: nav.getAttribute("data-settings-nav"),
      direction: style.flexDirection,
      wrap: style.flexWrap,
      navRect: { x: navRect.x, y: navRect.y, width: navRect.width, height: navRect.height, right: navRect.right, bottom: navRect.bottom },
      contentRect: contentRect ? { x: contentRect.x, y: contentRect.y, width: contentRect.width, height: contentRect.height, right: contentRect.right, top: contentRect.top } : null,
      itemCount: categories().length,
    };
  }

  function panels() {
    const base = root();
    if (!base) return [];
    return [...base.querySelectorAll(".panel, [data-settings-panel], [role=tabpanel], .set-section, [data-settings-section], .settings-section")].filter(visible);
  }

  function signature() {
    const base = root();
    if (!base) return { source: "no-root", items: [] };
    const keys = [...base.querySelectorAll("[data-settings-key]")].map((el) => norm(el.getAttribute("data-settings-key")));
    if (keys.length) return { source: "data-settings-key", items: keys };
    const items = [...base.querySelectorAll("[data-settings-label], .pref .txt .t, .sec > h2, .sec h3, .settings-group > h2, .settings-section > h2, summary, .qio-field-label, dt")]
      .map((el) => norm(el.innerText || el.textContent || "")).filter(Boolean);
    return { source: "fallback-labels", items };
  }

  function nativeControls() {
    const base = root();
    if (!base) return { total: 0, visible: 0, samples: [] };
    const list = [...base.querySelectorAll("select, input[type=number], input[type=checkbox], input[type=radio]")];
    return { total: list.length, visible: list.filter(visible).length, samples: list.filter(visible).slice(0, 12).map(desc) };
  }

  function horizontalScroll() {
    const base = root();
    const out = { hard: [], designed: [], document: null };
    if (base) {
      for (const el of [base, ...base.querySelectorAll("*")]) {
        if (!visible(el)) continue;
        if (isOrnament(el)) continue;
        // 表单控件内部的文字横向滚动是输入框自身行为（cursor 跟随），不是布局溢出。
        if (el.matches("input, textarea, select, [contenteditable=true]")) continue;
        if (el.clientWidth < 12) continue;
        if (el.scrollWidth <= el.clientWidth + 1) continue;
        const style = getComputedStyle(el);
        const entry = { el: desc(el), scrollWidth: el.scrollWidth, clientWidth: el.clientWidth, overflowX: style.overflowX, overflowY: style.overflowY };
        if (style.overflowX === "auto" || style.overflowX === "scroll") out.designed.push(entry);
        else out.hard.push(entry);
      }
    }
    const de = document.documentElement;
    out.document = { scrollWidth: de.scrollWidth, clientWidth: de.clientWidth, innerWidth: window.innerWidth, ok: de.scrollWidth <= de.clientWidth + 1 };
    return out;
  }

  function topBar() {
    const base = root();
    const chrome = document.querySelector(".window-chrome");
    const chromeRect = chrome ? chrome.getBoundingClientRect() : null;
    const rootRect = base ? base.getBoundingClientRect() : null;
    const fixedInside = [];
    if (base) {
      for (const el of base.querySelectorAll("header, [role=banner], [data-settings-titlebar]")) {
        const style = getComputedStyle(el);
        if (style.position === "fixed" || style.position === "sticky") fixedInside.push({ el: desc(el), position: style.position });
      }
    }
    return {
      chromeCount: document.querySelectorAll(".window-chrome").length,
      chromeInsideRoot: base ? base.querySelectorAll(".window-chrome").length : 0,
      chrome: chrome ? { rect: { x: chromeRect.x, y: chromeRect.y, width: chromeRect.width, height: chromeRect.height, bottom: chromeRect.bottom }, position: getComputedStyle(chrome).position, zIndex: getComputedStyle(chrome).zIndex } : null,
      root: rootRect ? { x: rootRect.x, y: rootRect.y, width: rootRect.width, height: rootRect.height, top: rootRect.top } : null,
      overlap: !!(chromeRect && rootRect && rootRect.top < chromeRect.bottom - 1),
      fixedInside,
    };
  }

  function focusInfo() {
    const el = document.activeElement;
    if (!el || el === document.body) return { has: false };
    let indicator = null;
    let node = el;
    for (let i = 0; i < 4 && node; i += 1) {
      const style = getComputedStyle(node);
      const outline = style.outlineStyle !== "none" && parseFloat(style.outlineWidth) > 0;
      const shadow = style.boxShadow !== "none" && style.boxShadow !== "";
      if (outline) { indicator = { kind: "outline", width: style.outlineWidth, color: style.outlineColor, level: i }; break; }
      if (shadow) { indicator = { kind: "box-shadow", value: style.boxShadow.slice(0, 60), level: i }; break; }
      node = node.parentElement;
    }
    const rect = el.getBoundingClientRect();
    return { has: true, el: desc(el), indicator, rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height } };
  }

  function animations(minDurationMs) {
    const out = [];
    const min = Number(minDurationMs == null ? 20 : minDurationMs);
    for (const animation of document.getAnimations()) {
      if (animation.playState !== "running") continue;
      let keyframes = [];
      try { keyframes = animation.effect.getKeyframes ? animation.effect.getKeyframes() : []; } catch (error) { keyframes = []; }
      const props = new Set();
      for (const frame of keyframes) for (const key of Object.keys(frame)) props.add(key);
      const timing = animation.effect && animation.effect.getTiming ? animation.effect.getTiming() : {};
      const duration = typeof timing.duration === "number" ? timing.duration : Number(timing.duration) || 0;
      const moving = [...props].some((key) => /transform|translate|scale|rotate|top|left|width|height|margin|inset/i.test(key));
      if (!moving) continue;
      if (duration <= min) continue;
      out.push({ props: [...props], duration, target: desc(animation.effect && animation.effect.target), name: animation.animationName || animation.transitionProperty || "" });
    }
    return out;
  }

  function radiusAudit(tokenPx) {
    const base = root();
    const out = { token: tokenPx, total: 0, ok: 0, pills: 0, zero: 0, edgeAttached: 0, edgeSamples: [], violations: [], pillSamples: [] };
    if (!base) return out;
    const corners = (style) => [style.borderTopLeftRadius, style.borderTopRightRadius, style.borderBottomRightRadius, style.borderBottomLeftRadius];
    for (const el of base.querySelectorAll("*")) {
      if (!visible(el)) continue;
      if (isOrnament(el)) continue;
      const style = getComputedStyle(el);
      const radii = corners(style);
      const values = radii.map((value) => parseFloat(value) || 0);
      const percents = radii.map((value) => /%$/.test(value.trim()));
      if (values.every((value) => value === 0)) { out.zero += 1; continue; }
      const hasPaint = (style.backgroundColor && !/^rgba?\(0, 0, 0, 0\)$|^transparent$/.test(style.backgroundColor)) || parseFloat(style.borderTopWidth) > 0 || (style.boxShadow && style.boxShadow !== "none");
      if (!hasPaint) continue;
      out.total += 1;
      const rect = el.getBoundingClientRect();
      const equal = values.every((value) => Math.abs(value - values[0]) < 0.6) && percents.every((flag) => flag === percents[0]);
      const isPill =
        (percents[0] && values[0] >= 49.5) ||
        (equal && !percents[0] && values[0] >= rect.height / 2 - 0.5 && values[0] > 6);
      const tokenLike = values.map((value, index) => (percents[index] ? false : Math.abs(value - tokenPx) < 0.6));
      // 贴边形状（一侧直角 + 其余角 14px，例如左侧竖排标签）单独记，不与「非令牌半径」混为一谈
      const edgeAttached = values.some((value) => value === 0) && values.every((value, index) => value === 0 || tokenLike[index]);
      if (equal && !percents[0] && Math.abs(values[0] - tokenPx) < 0.6) out.ok += 1;
      else if (isPill) {
        out.pills += 1;
        if (out.pillSamples.length < 12) out.pillSamples.push(desc(el));
      } else if (edgeAttached) {
        out.edgeAttached += 1;
        if (out.edgeSamples.length < 10) out.edgeSamples.push(desc(el));
      } else if (out.violations.length < 40) {
        out.violations.push({ radius: radii.join(" "), w: Math.round(rect.width), h: Math.round(rect.height), el: desc(el) });
      }
    }
    return out;
  }

  function expandAdvanced() {
    const base = root();
    if (!base) return { opened: 0, candidates: [] };
    const candidates = [...base.querySelectorAll("button, [role=button], summary, [data-settings-advanced]")]
      .filter(visible)
      // 分类导航本身也是 button（基线「高级」就正好叫这个名字）：绝不能把导航当折叠组点掉
      .filter((el) => !el.closest("nav, [role=tablist], [data-settings-nav], .settings-nav, .settings-tabs"))
      .filter((el) => {
        const text = norm(el.innerText || el.textContent || "");
        return text === "高级选项" || text === "高级" || text.includes("高级选项") || text.includes("高级参数") || text.includes("更多设置");
      });
    let opened = 0;
    for (const el of candidates) {
      if (el.getAttribute("aria-expanded") === "true") continue;
      el.scrollIntoView({ block: "center" });
      el.click();
      opened += 1;
    }
    return { opened, candidates: candidates.map(desc) };
  }

  function searchInput() {
    const base = root() || document;
    return base.querySelector("[data-settings-search] input, [data-settings-search-input], input[type=search], input[aria-label*='搜索'], input[placeholder*='搜索']");
  }

  function openSearch() {
    const existing = searchInput();
    if (existing && visible(existing)) { existing.focus(); return { found: true, alreadyOpen: true, el: desc(existing) }; }
    const base = root() || document;
    const triggers = [...base.querySelectorAll("button, [role=button], a, [data-settings-search-toggle]")]
      .filter(visible)
      .filter((el) => norm(el.innerText || el.textContent || "").includes("搜索") || norm(el.getAttribute("aria-label") || "").includes("搜索"));
    if (!triggers.length) return { found: false, reason: "search-trigger-not-found" };
    triggers[0].click();
    return { found: true, trigger: desc(triggers[0]) };
  }

  function search(query) {
    const input = searchInput();
    if (!input) return { ok: false, reason: "search-input-not-found" };
    input.focus();
    fireInput(input, query);
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    return { ok: true, value: input.value };
  }

  /** 搜索命中项：优先显式 data-search-hit；没有就退回结果容器里的选项/列表项。 */
  function searchHitElements() {
    const base = root() || document;
    const explicit = [...base.querySelectorAll("[data-search-hit], [data-settings-search-hit], .search-hit, .search-result")].filter(visible);
    if (explicit.length) return explicit;
    const scopes = [...base.querySelectorAll("[data-settings-search], .set-search, [data-testid=settings-search-panel], .settings-search, .search-panel, [role=listbox]")];
    const pools = scopes.length ? scopes : [base];
    const out = [];
    for (const scope of pools) for (const el of scope.querySelectorAll("[role=option], li")) if (visible(el)) out.push(el);
    return out;
  }

  function searchResults() {
    return searchHitElements().slice(0, 20).map((el) => {
      const text = norm(el.innerText || el.textContent || "");
      return { text, rect: rectOf(el), el: desc(el) };
    }).filter((item) => item.text);
  }

  function clickSearchResult(index) {
    const candidates = searchHitElements();
    const el = candidates[index || 0];
    if (!el) return { ok: false, reason: "no-clickable" };
    el.scrollIntoView({ block: "center" });
    el.click();
    return { ok: true, clicked: desc(el) };
  }

  function highlighted() {
    const base = root() || document;
    const selector = "[data-settings-highlight], .is-highlighted, .is-revealed, .settings-highlight, .search-target, .flash, .pulse, [data-highlight]";
    return [...base.querySelectorAll(selector)].filter(visible).slice(0, 6).map(desc);
  }

  function scrollPositions() {
    const base = root();
    if (!base) return { rootScrollTop: null, windowScrollY: window.scrollY };
    return { rootScrollTop: base.scrollTop, rootScrollHeight: base.scrollHeight, rootClientHeight: base.clientHeight, windowScrollY: window.scrollY };
  }

  function scrollToTop() {
    const base = root();
    if (base) base.scrollTop = 0;
    window.scrollTo(0, 0);
    return scrollPositions();
  }

  /** 纵向滚动审计：主滚动视口应当只有设置根一个；代码/许可全文这种限高查看器单独归为 bounded。 */
  function scrollAudit() {
    const base = root();
    const out = { main: null, nested: [], bounded: [], document: null };
    if (base) {
      for (const el of [base, ...base.querySelectorAll("*")]) {
        if (!visible(el)) continue;
        if (el.scrollHeight <= el.clientHeight + 1) continue;
        const style = getComputedStyle(el);
        if (!(style.overflowY === "auto" || style.overflowY === "scroll")) continue;
        const entry = { el: desc(el), scrollHeight: el.scrollHeight, clientHeight: el.clientHeight, overflowY: style.overflowY };
        if (el === base) out.main = entry;
        else if (el.matches("pre, textarea, [data-allow-scroll]")) out.bounded.push(entry);
        else out.nested.push(entry);
      }
    }
    out.rootOverflowY = base ? getComputedStyle(base).overflowY : null;
    const de = document.documentElement;
    out.document = {
      scrollHeight: de.scrollHeight,
      clientHeight: de.clientHeight,
      scrollWidth: de.scrollWidth,
      clientWidth: de.clientWidth,
      horizontalOk: de.scrollWidth <= de.clientWidth + 1,
    };
    return out;
  }

  /**
   * 同一分区里重复出现的整段说明/标题：内容重复属于用户可见缺陷，
   * 不能靠肉眼在 40 多张截图里发现（真实案例：权限组标题与说明各出现两次）。
   */
  function duplicateTexts() {
    const base = root();
    if (!base) return [];
    const counts = new Map();
    for (const el of base.querySelectorAll(".set-section__head h2, .set-section__desc, .set-search__hint, h2, h3, .panel__title, .section__title, .group__title")) {
      if (!visible(el)) continue;
      const text = norm(el.innerText || el.textContent || "");
      if (text.length < 10 || text.length > 400) continue;
      counts.set(text, (counts.get(text) || 0) + 1);
    }
    return [...counts.entries()].filter(([, count]) => count >= 2).map(([text, count]) => ({ text: text.slice(0, 120), count }));
  }

  /** 原生 confirm/alert/prompt 的调用计数（由 Page.addScriptToEvaluateOnNewDocument 注入的守卫提供）。 */
  function dialogCalls() {
    return window.__qioDialogCalls || null;
  }

  function pageText() {
    return (document.body.innerText || "").slice(0, 20000);
  }

  return {
    norm, visible, root, desc, rectOf, isOrnament, ownText, allElements,
    labelElement, rowFor, findControl, setControl, fireInput, fillPasswordLike, menuOptions, clickOptionText, openSelect,
    clickLabel, clickSwitch,
    categories, activeCategory, navInfo, panels,
    signature, nativeControls, horizontalScroll, topBar, focusInfo, animations, radiusAudit,
    expandAdvanced, searchInput, openSearch, search, searchResults, clickSearchResult, highlighted,
    scrollPositions, scrollToTop, scrollAudit, dialogCalls, duplicateTexts, pageText,
    _last: null,
  };
})();
true;
