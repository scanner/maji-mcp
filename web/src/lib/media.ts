/**
 * Media queries, followed as they change: how wide the window is, and whether the
 * system asks for less motion.
 */
import { onScopeDispose, ref, type Ref } from "vue";

/**
 * Whether a media query matches, kept current until the component using it is
 * unmounted.
 *
 * Returns:
 *     A ref holding whether `query` matches; false where the browser can't evaluate
 *     media queries
 */
export function useMedia(query: string): Ref<boolean> {
  const list = typeof window.matchMedia === "function" ? window.matchMedia(query) : null;
  const matches = ref(list?.matches ?? false);
  const changed = (event: MediaQueryListEvent) => (matches.value = event.matches);
  list?.addEventListener("change", changed);
  onScopeDispose(() => list?.removeEventListener("change", changed));
  return matches;
}

/** Whether the system asks for less motion, at the moment of asking. */
export function lessMotion(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}
