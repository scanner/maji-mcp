/**
 * Marking the characters a fuzzy search matched.
 */
import { describe, expect, it } from "vitest";

import { segments } from "@/lib/highlight";

describe("segments", () => {
  it("splits a text into matched and unmatched runs, in order", () => {
    expect(segments("reads of ~/src/api", [[11, 14], [15, 18]])).toEqual([
      { text: "reads of ~/", matched: false },
      { text: "src", matched: true },
      { text: "/", matched: false },
      { text: "api", matched: true },
    ]);
  });

  it("leaves a text with no ranges whole", () => {
    expect(segments("sentry", [])).toEqual([{ text: "sentry", matched: false }]);
  });
});
