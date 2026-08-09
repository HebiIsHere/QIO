# QIO 设计系统实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把已确认的 QIO 前端设计规范（双主题色板、三声部字体、透明球 SDF 融合环星球、输入/下拉等组件规范）落地为可运行的 Vue 3 前端。

**Architecture:** 设计令牌（`tokens.css`）→ 组件原语（`base.css` + 可复用 Vue 组件）→ 星球 3D（纯函数 SDF 数学 + Three.js 着色器，接入现有 `usePlanetScene.ts`）→ 页面换装（对话页/设置页/星球页）。每个阶段独立可测、频繁提交。

**Tech Stack:** Vue 3 + TS + Vite + Pinia + three.js r0.185；测试 Vitest + jsdom + @vue/test-utils；设计规范见 `docs/superpowers/specs/2026-08-09-qio-frontend-design.md`。

**参考原型（本机）**：`qio/.superpowers/brainstorm/vs-1786250923/content/planet3d.html`（星球 3D 主原型）、`ring-fusion-example.html`（环融合实现参考）、`input-spec.html`（输入/下拉组件规范）、`conversation-mockup.html`、`settings-mockup.html`。

---

## 文件结构

| 文件 | 责任 |
|---|---|
| `frontend/src/styles/tokens.css`（改） | 双主题设计令牌（暗紫晶/净白） |
| `frontend/src/styles/base.css`（建） | reset + 三声部字体 + 组件原语类（input/select/btn/card/badge/switch） |
| `frontend/src/planet/sdfRings.ts`（建） | 纯函数：球面 SDF（smin/levelField/ringAlpha 像素距离）——可单测 |
| `frontend/src/planet/planetShader.ts`（建） | ShaderMaterial 源码与 uniform 组装 |
| `frontend/src/planet/topicData.ts`（建） | 话题点/簇生成（权重、最小间距、聚簇） |
| `frontend/src/components/ui/QInput.vue`、`QSelect.vue`、`QButton.vue`、`QCard.vue`、`QBadge.vue`、`QSwitch.vue`（建） | 组件库 |
| `frontend/src/composables/usePlanetScene.ts`（改） | 接入透明球 + SDF 环 + 新交互（聚焦 2.25/环波/半球剔除） |
| `frontend/src/components/StatusBar.vue`、`Composer.vue`、`MessageItem.vue`、`PlanetDock.vue` 等（改） | 换装新组件 |
| `frontend/src/views/ConversationView.vue`、`SettingsView.vue`、`PlanetView.vue`（改） | 页面换装 |
| `frontend/vitest.config.ts`、`frontend/src/planet/__tests__/sdfRings.test.ts`、`frontend/src/components/ui/__tests__/QSelect.test.ts`（建） | 测试 |
| `AGENTS.md`（建，仓库根） | 引用设计规范，约束 AI 工具 |
| `frontend/package.json`（改） | 加 `test` 脚本 |

---

## Task 0: 前端测试基建

**Files:**
- Create: `frontend/vitest.config.ts`
- Create: `frontend/src/vitest.setup.ts`
- Modify: `frontend/package.json`

- [ ] **Step 1: 安装依赖**

Run: `cd frontend && npm i -D vitest jsdom @vue/test-utils`
Expected: 安装成功，package.json devDependencies 出现三包。

- [ ] **Step 2: 写 vitest 配置**

Create `frontend/vitest.config.ts`:
```ts
import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/vitest.setup.ts"],
    include: ["src/**/*.test.ts"],
  },
});
```

Create `frontend/src/vitest.setup.ts`:
```ts
// 空 setup：后续组件测试需要的全局 polyfill 放这里
```

Modify `frontend/package.json` scripts:
```json
"test": "vitest run"
```

- [ ] **Step 3: 加冒烟测试并跑通**

Create `frontend/src/smoke.test.ts`:
```ts
import { describe, expect, it } from "vitest";
describe("smoke", () => { it("1+1=2", () => expect(1 + 1).toBe(2)); });
```
Run: `npm test`
Expected: PASS（1 passed）。

- [ ] **Step 4: Commit**
```bash
git add frontend/vitest.config.ts frontend/src/vitest.setup.ts frontend/src/smoke.test.ts frontend/package.json
git commit -m "test: 前端 Vitest 基建"
```

---

## Task 1: tokens.css 双主题令牌

**Files:**
- Modify: `frontend/src/styles/tokens.css`（整体替换）

- [ ] **Step 1: 替换 tokens.css 为正式令牌**

```css
/* QIO 设计令牌 v2（2026-08-09 规范）——全应用唯一颜色来源 */
:root {
  /* 暗色「暗紫晶」默认 */
  --bg-base:#171015; --bg-surface:#1e161c; --bg-elevated:#261d24; --bg-inset:#130d12;
  --border-subtle:#302430; --border-strong:#463846;
  --text-strong:#f5eef2; --text-primary:#e6dbe2; --text-secondary:#a393a0; --text-muted:#6f636c;
  --accent:#c51b7d; --accent-hover:#dd3399; --link:#e878bd; --on-accent:#ffeef8;
  --accent-soft:rgba(197,27,125,.16);
  --success:#5fbf8f; --danger:#e06a6a; --warning:#d9b25c;
  --serif:Georgia,'Songti SC','SimSun',serif;
  --sans:system-ui,'PingFang SC','Microsoft YaHei',sans-serif;
  --mono:Consolas,'Cascadia Mono',monospace;
}
:root[data-theme="light"] {
  --bg-base:#fcfcfb; --bg-surface:#ffffff; --bg-elevated:#ffffff; --bg-inset:#f3f3f1;
  --border-subtle:#e7e7e4; --border-strong:#cbcbc6;
  --text-strong:#201f1d; --text-primary:#30302d; --text-secondary:#656460; --text-muted:#94938e;
  --accent:#c51b7d; --accent-hover:#dd3399; --link:#b0136a; --on-accent:#ffeef8;
  --accent-soft:rgba(197,27,125,.12);
  --success:#2f9e6e; --danger:#d14f4f; --warning:#b0892f;
}
```

- [ ] **Step 2: 验证令牌存在（测试）**

Create `frontend/src/styles/tokens.test.ts`:
```ts
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
const css = readFileSync(new URL("./tokens.css", import.meta.url), "utf8");
const required = ["--bg-base", "--bg-elevated", "--text-strong", "--accent", "--serif", "--mono", "data-theme=\"light\""];
describe("tokens.css", () => {
  it("包含必需令牌与亮色主题", () => {
    for (const tok of required) expect(css).toContain(tok);
  });
  it("暗色为默认", () => expect(css.indexOf("data-theme")).toBeGreaterThan(css.indexOf(":root {")));
});
```
Run: `npm test`
Expected: PASS。

- [ ] **Step 3: Commit**
```bash
git add frontend/src/styles/tokens.css frontend/src/styles/tokens.test.ts
git commit -m "feat: 双主题设计令牌 tokens.css v2"
```

---

## Task 2: base.css 组件原语

**Files:**
- Create: `frontend/src/styles/base.css`
- Modify: `frontend/src/main.ts`（import base.css）

- [ ] **Step 1: 写 base.css（reset + 字体 + 组件类）**

```css
@import "./tokens.css";
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
html,body{height:100%;}
body{background:var(--bg-base);color:var(--text-primary);font-family:var(--sans);transition:background .4s,color .4s;}
h1,h2,h3,.serif{font-family:var(--serif);}
.mono{font-family:var(--mono);}

/* ---- 输入 ---- */
.qio-input{display:block;width:100%;height:38px;background:var(--bg-inset);border:1px solid var(--border-subtle);
  border-radius:10px;padding:10px 14px;font-size:14px;line-height:1.5;color:var(--text-strong);outline:none;
  font-family:var(--sans);transition:border-color .18s,box-shadow .18s,background .18s;}
.qio-input::placeholder{color:var(--text-muted);}
.qio-input:hover{border-color:var(--border-strong);}
.qio-input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft);background:var(--bg-surface);}
.qio-input:disabled{opacity:.5;cursor:not-allowed;}
.qio-input.err{border-color:var(--danger);}
.qio-input.mono{font-family:var(--mono);font-size:13px;letter-spacing:.02em;}
textarea.qio-input{height:auto;min-height:88px;resize:vertical;}

/* ---- 按钮 ---- */
.qio-btn{height:34px;padding:0 16px;border-radius:10px;border:1px solid var(--border-subtle);
  background:var(--bg-inset);color:var(--text-secondary);font-size:13px;cursor:pointer;font-family:var(--sans);
  transition:all .18s;}
.qio-btn:hover{border-color:var(--border-strong);color:var(--text-strong);}
.qio-btn.primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent);}
.qio-btn.primary:hover{background:var(--accent-hover);}
.qio-btn.danger{color:var(--danger);border-color:var(--danger);}

/* ---- 卡片 ---- */
.qio-card{background:var(--bg-elevated);border:1px solid var(--border-subtle);border-radius:14px;padding:16px 18px;}

/* ---- 徽标 ---- */
.qio-badge{font-family:var(--mono);font-size:10px;padding:2px 9px;border-radius:20px;border:1px solid var(--border-strong);color:var(--text-secondary);}
.qio-badge.link{color:var(--link);border-color:var(--link);}

/* ---- 开关 ---- */
.qio-switch{width:38px;height:22px;border-radius:20px;background:var(--border-strong);position:relative;cursor:pointer;border:none;transition:background .2s;}
.qio-switch.on{background:var(--accent);}
.qio-switch::after{content:'';position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;background:#fff;transition:left .2s;}
.qio-switch.on::after{left:19px;}
```

- [ ] **Step 2: main.ts 引入**

Modify `frontend/src/main.ts`：在现有样式导入后加 `import "./styles/base.css";`

- [ ] **Step 3: 构建验证**

Run: `npm run build`
Expected: vue-tsc 无错、vite build 成功。

- [ ] **Step 4: Commit**
```bash
git add frontend/src/styles/base.css frontend/src/main.ts
git commit -m "feat: base.css 组件原语 + 三声部字体"
```

---

## Task 3: QInput.vue 组件

**Files:**
- Create: `frontend/src/components/ui/QInput.vue`
- Create: `frontend/src/components/ui/__tests__/QInput.test.ts`

- [ ] **Step 1: 写失败测试**

```ts
import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import QInput from "../QInput.vue";
describe("QInput", () => {
  it("渲染原生 input 并透传 placeholder", () => {
    const w = mount(QInput, { props: { placeholder: "test" } });
    expect(w.find("input").attributes("placeholder")).toBe("test");
  });
  it("error 时挂 err 类", () => {
    const w = mount(QInput, { props: { error: true } });
    expect(w.find("input").classes()).toContain("err");
  });
  it("mono 变体挂 mono 类", () => {
    const w = mount(QInput, { props: { mono: true } });
    expect(w.find("input").classes()).toContain("mono");
  });
});
```
Run: `npm test` → 预期 FAIL（找不到组件）。

- [ ] **Step 2: 实现 QInput.vue**

```vue
<script setup lang="ts">
defineProps<{ modelValue?: string; placeholder?: string; error?: boolean; mono?: boolean; disabled?: boolean; type?: string }>();
defineEmits<{ "update:modelValue": [string] }>();
</script>
<template>
  <input
    class="qio-input" :class="{ err: error, mono }" :type="type ?? 'text'"
    :value="modelValue" :placeholder="placeholder" :disabled="disabled"
    @input="$emit('update:modelValue', ($event.target as HTMLInputElement).value)"
  />
</template>
```

- [ ] **Step 3: 跑测试**
Run: `npm test` → 预期 PASS。

- [ ] **Step 4: Commit**
```bash
git add frontend/src/components/ui/QInput.vue frontend/src/components/ui/__tests__/QInput.test.ts
git commit -m "feat: QInput 输入组件"
```

---

## Task 4: QSelect.vue 自定义下拉

**Files:**
- Create: `frontend/src/components/ui/QSelect.vue`
- Create: `frontend/src/components/ui/__tests__/QSelect.test.ts`

- [ ] **Step 1: 写失败测试**

```ts
import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import QSelect from "../QSelect.vue";
const opts = [{ value: "a", label: "A" }, { value: "b", label: "B" }];
describe("QSelect", () => {
  it("默认显示选中项", () => {
    const w = mount(QSelect, { props: { options: opts, modelValue: "b" } });
    expect(w.find(".qio-select-val").text()).toBe("B");
  });
  it("点击展开，点选项后发事件并关闭", async () => {
    const w = mount(QSelect, { props: { options: opts, modelValue: "a" } });
    await w.find(".qio-select").trigger("click");
    expect(w.find(".qio-select").classes()).toContain("open");
    const b = w.findAll(".opt").find(o => o.text() === "B")!;
    await b.trigger("click");
    expect(w.emitted("update:modelValue")?.[0]).toEqual(["b"]);
    expect(w.find(".qio-select").classes()).not.toContain("open");
  });
});
```
Run: `npm test` → 预期 FAIL。

- [ ] **Step 2: 实现 QSelect.vue**

```vue
<script setup lang="ts">
import { ref, computed } from "vue";
const props = defineProps<{ options: { value: string; label: string }[]; modelValue?: string }>();
const emit = defineEmits<{ "update:modelValue": [string] }>();
const open = ref(false);
const val = computed(() => props.options.find(o => o.value === props.modelValue)?.label ?? "");
function pick(v: string) { emit("update:modelValue", v); open.value = false; }
</script>
<template>
  <div class="qio-select" tabindex="0" :class="{ open }" @click="open = !open" @keydown.esc="open = false"
       @keydown.enter.prevent="open = !open" @keydown.space.prevent="open = !open">
    <span class="qio-select-val">{{ val }}</span>
    <svg class="chev" width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M4 6l4 4 4-4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
    <div class="qio-select-menu">
      <div v-for="o in options" :key="o.value" class="opt" :class="{ sel: o.value === modelValue }" @click.stop="pick(o.value)">{{ o.label }}</div>
    </div>
  </div>
</template>
<style scoped>
.qio-select{position:relative;display:flex;align-items:center;justify-content:space-between;gap:8px;height:38px;
  background:var(--bg-inset);border:1px solid var(--border-subtle);border-radius:10px;padding:0 14px;font-size:14px;
  color:var(--text-strong);cursor:pointer;outline:none;user-select:none;transition:border-color .18s,box-shadow .18s,background .18s;}
.qio-select:hover{border-color:var(--border-strong);}
.qio-select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft);background:var(--bg-surface);}
.qio-select .chev{color:var(--text-muted);transition:transform .2s;flex-shrink:0;}
.qio-select.open .chev{transform:rotate(180deg);color:var(--accent);}
.qio-select-menu{position:absolute;top:calc(100% + 6px);left:0;right:0;z-index:30;background:var(--bg-elevated);
  border:1px solid var(--border-subtle);border-radius:10px;box-shadow:0 8px 28px rgba(0,0,0,.35);padding:5px;display:none;min-width:max-content;}
.qio-select.open .qio-select-menu{display:block;}
.qio-select-menu .opt{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 12px;border-radius:8px;
  font-size:13px;color:var(--text-primary);cursor:pointer;white-space:nowrap;}
.qio-select-menu .opt:hover{background:var(--accent-soft);color:var(--text-strong);}
.qio-select-menu .opt.sel{color:var(--link);font-weight:600;}
.qio-select-menu .opt.sel::after{content:'✓';font-family:var(--mono);color:var(--accent);}
</style>
```

- [ ] **Step 3: 跑测试**
Run: `npm test` → 预期 PASS。

- [ ] **Step 4: 补基础样式到 base.css**

若 `base.css` 未含 `.qio-select`，把上面 `<style scoped>` 中的类复制到 `base.css`（组件原语共享），scoped 保留或移除均可。

- [ ] **Step 5: Commit**
```bash
git add frontend/src/components/ui/QSelect.vue frontend/src/components/ui/__tests__/QSelect.test.ts frontend/src/styles/base.css
git commit -m "feat: QSelect 自定义下拉组件"
```

---

## Task 5: sdfRings.ts 纯函数（球面 SDF）

**Files:**
- Create: `frontend/src/planet/sdfRings.ts`
- Create: `frontend/src/planet/__tests__/sdfRings.test.ts`

- [ ] **Step 1: 写失败测试**

```ts
import { describe, expect, it } from "vitest";
import { smin, levelField } from "../sdfRings";
import * as THREE from "three";
describe("sdfRings", () => {
  it("smin 在远距离取最小值", () => {
    expect(smin(0.1, 0.9, 0.16)).toBeCloseTo(0.1, 5);
  });
  it("smin 在近距离产生融合（小于两者）", () => {
    expect(smin(0.01, 0.01, 0.16)).toBeLessThan(0.01);
  });
  it("levelField：单话题在环上为 0，远离为正", () => {
    const topics = [{ pos: new THREE.Vector3(0, 0, 1), w: 1 }];
    const onRing = new THREE.Vector3(Math.sin(0.14), 0, Math.cos(0.14)).normalize();
    expect(Math.abs(levelField(onRing, topics, 0.14, 0.16))).toBeLessThan(1e-3);
    const far = new THREE.Vector3(0, 1, 0).normalize();
    expect(levelField(far, topics, 0.14, 0.16)).toBeGreaterThan(0.5);
  });
});
```
Run: `npm test` → 预期 FAIL（模块不存在）。

- [ ] **Step 2: 实现 sdfRings.ts**

```ts
import * as THREE from "three";

export function smin(a: number, b: number, k: number): number {
  const h = Math.max(0, Math.min(1, 0.5 + 0.5 * (b - a) / k));
  return b * (1 - h) + a * h - k * h * (1 - h);
}

export interface TopicRingSource { pos: THREE.Vector3; w: number; }

export function levelField(p: THREE.Vector3, topics: TopicRingSource[], rL: number, k: number): number {
  let f = 1e9;
  for (const t of topics) {
    const ang = Math.acos(Math.max(-1, Math.min(1, p.dot(t.pos))));
    f = smin(f, ang - t.w * rL, k);
  }
  return f;
}
```

- [ ] **Step 3: 跑测试**
Run: `npm test` → 预期 PASS。

- [ ] **Step 4: Commit**
```bash
git add frontend/src/planet/sdfRings.ts frontend/src/planet/__tests__/sdfRings.test.ts
git commit -m "feat: 球面 SDF 融合环纯函数"
```

---

## Task 6: 星球场景接入（透明球 + SDF 环 + 新交互）

**Files:**
- Create: `frontend/src/planet/planetShader.ts`
- Create: `frontend/src/planet/topicData.ts`
- Modify: `frontend/src/composables/usePlanetScene.ts`

- [ ] **Step 1: topicData.ts（聚簇 + 权重 + 最小间距）**

```ts
import * as THREE from "three";
export interface TopicData { id: string; ci: number; pos: THREE.Vector3; w: number; }
export const LEVEL_R = [0.14, 0.22, 0.34];
export const LEVEL_A = [0.62, 0.44, 0.28];
export const LEVEL_HW = [5.0, 3.25, 2.0]; // 像素半宽：内粗外细
export const MIN_SEP = 0.095;

export function buildTopics(clusters: { center: THREE.Vector3; n: number }[]): TopicData[] {
  const out: TopicData[] = [];
  let seed = 20260809;
  const rnd = () => (seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296;
  const u = new THREE.Vector3(), v = new THREE.Vector3();
  const basis = (c: THREE.Vector3) => {
    u.crossVectors(c, new THREE.Vector3(0, 1, 0)); if (u.lengthSq() < 1e-6) u.set(1, 0, 0); u.normalize();
    v.crossVectors(c, u).normalize();
  };
  clusters.forEach((c, ci) => {
    for (let k = 0; k < c.n; k++) {
      let pos: THREE.Vector3 | null = null;
      for (let tries = 0; tries < 40 && !pos; tries++) {
        const ang = 0.12 + rnd() * 0.10, ph = rnd() * Math.PI * 2;
        basis(c.center);
        const p = new THREE.Vector3().addScaledVector(c.center, Math.cos(ang))
          .addScaledVector(u, Math.sin(ang) * Math.cos(ph))
          .addScaledVector(v, Math.sin(ang) * Math.sin(ph)).normalize();
        let ok = true;
        for (const t of out) if (t.ci === ci && Math.acos(Math.max(-1, Math.min(1, p.dot(t.pos)))) < MIN_SEP) { ok = false; break; }
        if (ok) pos = p;
      }
      out.push({ id: `${ci}-${k}`, ci, pos: pos ?? c.center.clone(), w: 0.85 + rnd() * 0.45 });
    }
  });
  return out;
}
```

- [ ] **Step 2: planetShader.ts（着色器源码 + uniforms 组装）**

从原型 `planet3d.html` 提取 ShaderMaterial（片段着色器：`acos(dot)` + `smin` + `fwidth` 像素距离抗锯齿），导出：
```ts
import * as THREE from "three";
import type { TopicData } from "./topicData";

export const MAX_TOPICS = 16;
export const RING_FRAG = /* glsl */ `
  uniform vec3 uTopics[16]; uniform float uWeights[16]; uniform int uCount;
  uniform vec3 uRingColor; uniform float uR0,uR1,uR2; uniform float uA0,uA1,uA2;
  uniform float uDim0,uDim1,uDim2; uniform float uK; uniform float uHW0,uHW1,uHW2;
  varying vec3 vPos;
  float sminF(float a,float b,float k){ float h=clamp(0.5+0.5*(b-a)/k,0.,1.); return mix(b,a,h)-k*h*(1.-h); }
  float levelField(vec3 p,float rL){ float f=1e9; for(int i=0;i<16;i++){ if(i>=uCount) break;
    float ang=acos(clamp(dot(p,uTopics[i]),-1.,1.)); f=sminF(f,ang-uWeights[i]*rL,uK); } return f; }
  float ringA(float f,float hw){ float g=max(length(vec2(dFdx(f),dFdy(f))),1e-5);
    float dp=abs(f)/g; float aa=fwidth(f)/g; return 1.-smoothstep(hw-aa,hw,dp); }
  void main(){ vec3 p=normalize(vPos);
    float a=uA0*uDim0*ringA(levelField(p,uR0),uHW0)
          +uA1*uDim1*ringA(levelField(p,uR1),uHW1)
          +uA2*uDim2*ringA(levelField(p,uR2),uHW2);
    gl_FragColor=vec4(uRingColor,clamp(a,0.,1.)); }
`;

export function makeRingUniforms(topics: TopicData[], theme: "dark" | "light") {
  const colors = { dark: new THREE.Color(0xe878bd), light: new THREE.Color(0xb0136a) };
  const list: THREE.Vector3[] = [], w = new Float32Array(MAX_TOPICS);
  topics.forEach((t, i) => { list.push(t.pos); w[i] = t.w; });
  for (let i = topics.length; i < MAX_TOPICS; i++) { list.push(new THREE.Vector3(0, 1, 0)); w[i] = -100; }
  return {
    uTopics: { value: list }, uWeights: { value: w }, uCount: { value: topics.length },
    uRingColor: { value: colors[theme] },
    uR0: { value: LEVEL_R[0] }, uR1: { value: LEVEL_R[1] }, uR2: { value: LEVEL_R[2] },
    uA0: { value: LEVEL_A[0] }, uA1: { value: LEVEL_A[1] }, uA2: { value: LEVEL_A[2] },
    uDim0: { value: 1 }, uDim1: { value: 1 }, uDim2: { value: 1 },
    uK: { value: 0.16 }, uHW0: { value: LEVEL_HW[0] }, uHW1: { value: LEVEL_HW[1] }, uHW2: { value: LEVEL_HW[2] },
  };
}
```
（从 `sdfRings.ts` 导入 `LEVEL_R/LEVEL_A/LEVEL_HW`。）

- [ ] **Step 3: usePlanetScene.ts 接入**

在 `usePlanetScene` 中：
- 移除旧 PALETTE 与旧标记小球，改为：
  1. 透明球（`MeshBasicMaterial` opacity 0.05, depthWrite:false）+ 轮廓圆（跟随相机）+ 极淡经纬线；
  2. 环球（`SphereGeometry(1.006,128,96)` + 上述 ShaderMaterial，`renderOrder=2`，depthWrite:false）；
  3. 话题点（`CircleGeometry(0.028*w,48)` 圆片，`quaternion.setFromUnitVectors(+Z, pos)`，半球剔除）；
- 交互：
  - 点击点：相机 tween 到 `方向 * 2.25`（650–800ms `easeInOutCubic`）+ 星球 slerp 使点居中（补间期间 `dt*7`，结束置 null 恢复拖拽）+ 点放大 1.3×；
  - 环波：点击设 `waveStart`，每帧 `uDimL = 1 - 0.75*exp(-((dt - L*0.16)/0.10)^2)`（dt<1.2s）；
  - 自转：空闲 `rotation.y += dt*0.1`，聚焦暂停；
  - 计时用 `performance.now()`（禁止 THREE.Timer 未 update 用法）。

- [ ] **Step 4: 验证**

Run: `npm run dev`，打开页面：确认透明球 + 玫红融合环 + 圆点、点击聚焦 2.25 + 环波、拖拽正常、无报错（控制台无 error）。

- [ ] **Step 5: Commit**
```bash
git add frontend/src/planet frontend/src/composables/usePlanetScene.ts
git commit -m "feat: 星球接入透明球 + SDF 融合环 + 聚焦/环波交互"
```

---

## Task 7: 对话页换装

**Files:**
- Modify: `frontend/src/components/StatusBar.vue`、`Composer.vue`、`MessageItem.vue`、`MessageStream.vue`、`PlanetDock.vue`、`frontend/src/views/ConversationView.vue`

- [ ] **Step 1: 状态条/消息/工具卡换装**

按 mockup `conversation-mockup.html`：
- StatusBar：SSE 状态点 + 三态徽标 + budget + `tok` 等宽读数（`.qio-badge`/`.mono`）；
- MessageItem：用户气泡 `var(--accent)` 底 + `--on-accent` 文字；助手气泡 `var(--bg-elevated)` + 边框；时间戳 `.mono`；
- 工具卡折叠/展开、注入标签（玫红虚线胶囊）；
- 空状态：衬线「今天想聊点什么？」。

- [ ] **Step 2: Composer 换装**

话题名（`.serif`）+ 锚点（`.mono`）+ 斜杠提示；输入用 `QInput`（textarea 变体）；记忆滑块保留，读数 `.mono`。

- [ ] **Step 3: PlanetDock 换装**

SVG 微缩星球（透明球 + 三圈融合环 + 圆点，参考 mockup），点击展开全屏星球。

- [ ] **Step 4: 验证**
Run: `npm run dev`，对照 `conversation-mockup.html` 视觉一致（暗/亮）。

- [ ] **Step 5: Commit**
```bash
git add frontend/src/components frontend/src/views/ConversationView.vue
git commit -m "feat: 对话页按 v2 设计规范换装"
```

---

## Task 8: 设置页换装

**Files:**
- Modify: `frontend/src/views/SettingsView.vue`、相关凭据子组件

- [ ] **Step 1: 凭据/偏好换装**

按 mockup `settings-mockup.html`：
- Tab 切换（凭据/偏好，`.tab.active` 下划线玫红）；
- 凭据卡：`.qio-card` + 掩码密钥 `.mono` + 预算进度条 + 状态徽标 + 操作按钮（`QButton`）；
- 表单输入全部换 `QInput`/`QSelect`；
- 偏好：模型端点 `QSelect`、记忆强度/冷热阈值 `QInput.mono`、强制继续 `QSwitch`。

- [ ] **Step 2: 验证**
Run: `npm run dev`，对照 `settings-mockup.html` 视觉一致（暗/亮），自定义下拉交互可用。

- [ ] **Step 3: Commit**
```bash
git add frontend/src/views/SettingsView.vue
git commit -m "feat: 设置页按 v2 设计规范换装"
```

---

## Task 9: 星球页 PlanetView 联动

**Files:**
- Modify: `frontend/src/views/PlanetView.vue`

- [ ] **Step 1: 全屏星球接 usePlanetScene**

`PlanetView` 使用 `usePlanetScene` 渲染全屏星球（相机近景），与悬浮球共享单一 3D 场景（相机状态 `overview/planet/focus`）；关闭动画复用相机拉回。

- [ ] **Step 2: 验证**
Run: `npm run dev`：悬浮球点击 → 相机推进全屏；点击话题 → 聚焦 + 环波；关闭 → 拉回。

- [ ] **Step 3: Commit**
```bash
git add frontend/src/views/PlanetView.vue
git commit -m "feat: 星球页与悬浮球相机联动"
```

---

## Task 10: AGENTS.md + 最终验证

**Files:**
- Create: `AGENTS.md`（仓库根）

- [ ] **Step 1: 写项目级 AGENTS.md**

```markdown
# QIO 项目约定（AI 助手必读）

- 前端设计规范：`docs/superpowers/specs/2026-08-09-qio-frontend-design.md`
- 颜色一律用 `var(--*)`（`frontend/src/styles/tokens.css`），禁止硬编码色值。
- 字体三声部：标题/话题=衬线，正文=无衬线，数据/时间/密钥=等宽。
- 星球渲染：透明球 + SDF 融合环（内粗外细 10/6.5/4px），禁止另加额外水波；聚焦距离 2.25。
- 改了前端代码：IAB 需带新参数强制刷新；改了后端：重启 uvicorn（无热加载）。
```

- [ ] **Step 2: 全量验证**

Run: `cd frontend && npm test && npm run build`
Expected: 全部测试 PASS；vue-tsc 无错、build 成功。

- [ ] **Step 3: 最终提交**
```bash
git add AGENTS.md
git commit -m "docs: 项目级 AGENTS.md 引用 v2 设计规范"
```

---

## 自检记录

- **Spec 覆盖**：设计文档 §2 色板→Task1；§3 字体→Task2；§7 输入/下拉→Task3/4；§4 星球→Task5/6；§5 对话页→Task7；§6 设置页→Task8；§11 待办（AGENTS.md、接入 usePlanetScene）→Task9/10。无遗漏。
- **占位符**：无 TBD/TODO；所有代码步骤给出完整实现。
- **类型一致性**：`LEVEL_R/LEVEL_A/LEVEL_HW` 在 `topicData.ts` 定义并在 `planetShader.ts`/`usePlanetScene.ts` 复用；`smin/levelField` 签名在 Task5 定义并被测试引用一致。
