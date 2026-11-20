/**
 * The time now, kept current for anything on the page that says how long ago
 * something was.
 */
import { onBeforeUnmount, onMounted, ref, type Ref } from "vue";

const TICK_MS = 1000;

const now = ref(new Date());
let users = 0;
let timer: ReturnType<typeof setInterval> | undefined;

/**
 * The time now, updated each second while a component using it is mounted. Every
 * component shares one clock and one timer, which stops once the last of them is
 * unmounted.
 */
export function useNow(): Ref<Date> {
  now.value = new Date();
  onMounted(() => {
    if (users++ === 0) timer = setInterval(() => (now.value = new Date()), TICK_MS);
  });
  onBeforeUnmount(() => {
    if (--users === 0) {
      clearInterval(timer);
      timer = undefined;
    }
  });
  return now;
}
