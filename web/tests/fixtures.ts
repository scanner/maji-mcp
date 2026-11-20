/**
 * Items shaped as the control plane sends them, for component tests.
 */
import type { Item } from "@/api/client";

export function item(overrides: Partial<Item> = {}): Item {
  return {
    id: "studio-mac-01jc6z8q3v7m2k4t9r5x1y0b8n",
    kind: "changeset",
    state: "waiting",
    title: "Update journal entry",
    requester: { principal: "claude-code", session: "s-1" },
    risk: "medium",
    blocking: false,
    facts: [{ label: "Size", value: "+1 -1 lines" }],
    actions: [
      { id: "approve", label: "Approve", style: "primary" },
      { id: "return", label: "Return with note", style: "neutral", needs: ["note"] },
      { id: "reject", label: "Reject", style: "danger" },
    ],
    content_hash: `sha256:${"a".repeat(64)}`,
    created_at: "2026-11-08T09:00:00+00:00",
    notes: [],
    ...overrides,
  } as Item;
}
