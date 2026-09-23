<script setup lang="ts">
import { onMounted } from "vue";
import { useEventStore } from "./stores/events";
import { useUiStore } from "./stores/ui";
import { useOnboardingStore } from "./stores/onboarding";
import ApprovalModal from "./components/ApprovalModal.vue";
import ApprovalEntry from "./components/ApprovalEntry.vue";
import OnboardingWizard from "./components/onboarding/OnboardingWizard.vue";

const events = useEventStore();
const ui = useUiStore();
const onboarding = useOnboardingStore();
onMounted(() => {
  events.connect();
  void ui.load();
  void onboarding.load();
});
</script>

<template>
  <div class="app-shell">
    <router-view />
    <ApprovalEntry />
    <ApprovalModal />
    <!-- 首次引导：真正首次启动，或「本版本还没展示过欢迎页」（刚更新的用户）时展开一次 -->
    <OnboardingWizard v-if="onboarding.showWizard" @done="onboarding.closeForSession()" />
  </div>
</template>

<style>
:root {
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  color: var(--text-primary);
  background: var(--bg-base);
}
* { box-sizing: border-box; }
body { margin: 0; }
.app-shell { width: 100vw; height: 100vh; overflow: hidden; }
</style>
