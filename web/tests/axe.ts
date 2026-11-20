/**
 * axe-core over a rendered component. jsdom can't compute colour, so contrast is left
 * to tests/contrast.test.ts and the browser test.
 */
import axe from "axe-core";

export async function violations(element: Element): Promise<string[]> {
  const result = await axe.run(element, {
    rules: { "color-contrast": { enabled: false }, region: { enabled: false } },
  });
  return result.violations.map((v) => `${v.id}: ${v.help}`);
}
