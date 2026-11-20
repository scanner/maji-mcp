// @vitest-environment node
/**
 * The palette in src/styles/tokens.css, checked against WCAG's contrast ratios in
 * both themes: 4.5:1 for text, 3:1 for the lines and focus rings controls rely on.
 */
import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

const css = readFileSync(new URL("../src/styles/tokens.css", import.meta.url), "utf8");

function tokens(from: string): Record<string, string> {
  const start = css.indexOf(from);
  const block = css.slice(start, css.indexOf("}", start));
  return Object.fromEntries([...block.matchAll(/--([a-z-]+):\s*(#[0-9a-f]{6})/g)].map((m) => [m[1], m[2]]));
}

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  const linear = (c: number) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return 0.2126 * linear(r!) + 0.7152 * linear(g!) + 0.0722 * linear(b!);
}

function ratio(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi! + 0.05) / (lo! + 0.05);
}

const light = tokens(":root {");
const dark = { ...light, ...tokens("@media (prefers-color-scheme: dark)") };

// Each text colour against every background it is set on.
const TEXT: [string, string][] = [
  ["fg", "bg"], ["fg", "surface"], ["fg", "surface-sunken"],
  ["muted", "bg"], ["muted", "surface"], ["muted", "surface-sunken"],
  ["accent", "bg"], ["accent", "surface"], ["accent", "accent-soft"], ["accent-fg", "accent"],
  ["ok", "surface"], ["ok", "ok-soft"],
  ["danger", "surface"], ["danger", "danger-soft"], ["fg", "danger-soft"],
  ["caution", "surface"], ["caution", "caution-soft"],
  ["session", "session-soft"], ["session", "surface"],
  ["fg", "added-bg"], ["fg", "removed-bg"],
];
// The lines that show where a control is, and the focus ring.
const UI: [string, string][] = [
  ["line-strong", "surface"], ["line-strong", "bg"], ["focus", "bg"], ["focus", "surface"],
];

describe.each([
  ["light", light],
  ["dark", dark],
])("the %s theme", (_name, theme) => {
  it.each(TEXT)("%s text on %s reaches 4.5:1", (fg, bg) => {
    expect(ratio(theme[fg]!, theme[bg]!)).toBeGreaterThanOrEqual(4.5);
  });
  it.each(UI)("%s on %s reaches 3:1", (fg, bg) => {
    expect(ratio(theme[fg]!, theme[bg]!)).toBeGreaterThanOrEqual(3);
  });
});
