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
