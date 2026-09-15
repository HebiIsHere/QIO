/**
 * 出现/退出的存在性管理：让弹窗、菜单这类浮层在关闭时先播完短退出动画再卸载。
 *
 * 关键约定（任务02 C/D）：
 * - 「功能完成」不依赖动画事件：退出只用定时器决定卸载，动画没触发、时长为零、
 *   组件被卸载都能正确收尾；减少动画时直接跳过退出阶段。
 * - 关闭过程中的浮层由调用方决定是否继续拦截点击（模态保拦截、菜单不拦截），
 *   但退出结束后一定从 DOM 移除，不留透明残留、不保留焦点。
 */
import { onUnmounted, ref, watch } from "vue";
import { prefersReducedMotion } from "../utils/motion";

export function usePresence(visible: () => boolean, exitMs = 150) {
  const mounted = ref(visible());
  const leaving = ref(false);
  let timer: ReturnType<typeof setTimeout> | null = null;

  function clear() {
    if (timer) clearTimeout(timer);
    timer = null;
  }

  watch(visible, (v) => {
    clear();
    if (v) {
      mounted.value = true;
      leaving.value = false;
      return;
    }
    if (!mounted.value) return;
    // 减少动画 / 时长为零：立即卸载，绝不让界面等一个看不见的动画
    if (exitMs <= 0 || prefersReducedMotion()) {
      mounted.value = false;
      leaving.value = false;
      return;
    }
    leaving.value = true;
    timer = setTimeout(() => {
      mounted.value = false;
      leaving.value = false;
      timer = null;
    }, exitMs);
  });

  onUnmounted(clear);

  return { mounted, leaving };
}
