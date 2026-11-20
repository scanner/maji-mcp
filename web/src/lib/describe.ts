/**
 * Words for an item's kind, risk and state, and for what a decision did, so nothing
 * is shown by colour alone.
 */
import type { Item, Remember, Schemas } from "@/api/client";

/** The actions that approve something, so only they can refuse parts or remember. */
export const APPROVING = new Set(["approve", "allow_once", "allow_here"]);

/** Each kind of request, as its tag names it. */
export const KINDS: Record<string, string> = {
  changeset: "Change",
  run_candidate: "Run a candidate",
  promote: "Promote a skill",
  grant: "Permission",
  connect: "Connect a service",
  pack_import: "Import a pack",
  pack_export: "Export a pack",
  policy_change: "Policy change",
  question: "Question",
  server_registration: "New MCP server",
};

export function kindLabel(item: Pick<Item, "kind">): string {
  return KINDS[item.kind] ?? item.kind;
}

/** The tag on an item that can't join a batch; its details say why. */
export const ALONE_TAG = "Decide on its own";

/** The tag on an item an agent has stopped for. */
export const WAITING_TAG = "An agent is waiting";

/** Whether an agent is waiting on the item now: it stopped for it, and it isn't decided. */
export function waitedOn(item: Pick<Item, "blocking" | "state">): boolean {
  return Boolean(item.blocking) && item.state === "waiting";
}

/** Each reason an item is high risk, named for the person deciding. */
export const RISK_REASONS: Record<string, string> = {
  data_leaves: "Data leaves",
  trust_changes: "Trust changes for later",
  your_words: "Your words are removed or replaced",
  addressed_to_ai: "Text addressed to an AI",
  beyond_the_task: "Reaches beyond the task",
  secrets_in_reach: "Secrets in reach",
};

/**
 * The tag for an item's risk: only high risk has one. Medium is the queue's usual
 * item, and a tag on every item would go unread; and no client calls an item low risk
 * or safe, since it means only that none of the reasons for high risk was found.
 */
export function riskTag(item: Pick<Item, "risk">): string | null {
  return item.risk === "high" ? "High risk" : null;
}

/** A piece of evidence a reason points at, as shown beside the reason. */
export interface Evidence {
  /** What it is, such as a fact's label or "Added line". */
  said: string;
  text: string;
  /** Whether `text` is a line of the diff, set as code. */
  line: boolean;
}

const LINES: Record<string, string> = { add: "Added line", delete: "Removed line", context: "Unchanged line" };

/** The evidence a reason points at on its item; a pointer at nothing there is left out. */
export function evidence(item: Item, reason: Schemas["RiskReason"]): Evidence[] {
  const shown: Evidence[] = [];
  // An item queued before reasons carried evidence has none.
  for (const pointer of reason.evidence ?? []) {
    if ("fact" in pointer) {
      const fact = item.facts[pointer.fact];
      if (fact) shown.push({ said: fact.label, text: fact.value, line: false });
    } else if ("line" in pointer) {
      const line = item.diff?.lines[pointer.line];
      if (line) shown.push({ said: LINES[line.op] ?? line.op, text: line.text, line: true });
    } else {
      const part = item.parts?.find((p) => p.id === pointer.part);
      if (part) shown.push({ said: "Part", text: part.label, line: false });
    }
  }
  return shown;
}

/** Where an item stands, as its tag names it. */
export const STATES: Record<string, string> = {
  waiting: "Waiting",
  decided: "Decided",
  expired: "Expired",
  withdrawn: "Withdrawn",
};

export function stateLabel(item: Pick<Item, "state">): string {
  return STATES[item.state] ?? item.state;
}

/**
 * The start of a session's key, shown beside the session's name. The name is a label
 * people can tell apart; the key is what grants and decisions are held to, and the
 * gateway's log gives it by the same 8 characters.
 */
export function shortKey(session: string): string {
  return session.slice(0, 8);
}

/**
 * Text cut after each slash, so a path shown in a narrow space can break between its
 * folders rather than inside a name: "~/Documents/" and "notes".
 */
export function afterSlashes(text: string): string[] {
  return text.split(/(?<=\/)/);
}

/** A time in the reader's own words and zone, for display beside a <time>. */
export function when(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/**
 * A time short enough for a row of a list: the time alone today, with the month and
 * day on another day, and the year too in another year.
 */
export function shortWhen(iso: string, now: Date = new Date()): string {
  const at = new Date(iso);
  if (at.toDateString() === now.toDateString()) {
    return at.toLocaleTimeString(undefined, { timeStyle: "short" });
  }
  return at.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    ...(at.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }),
    hour: "numeric",
    minute: "2-digit",
  });
}

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["second", 60],
  ["minute", 60],
  ["hour", 24],
  ["day", Infinity],
];

/**
 * How long ago a time was, in the reader's own words: in seconds under a minute,
 * minutes under an hour, hours under a day, and days after that. A time ahead of
 * the reader's clock is said as now.
 */
export function ago(iso: string, now: Date = new Date()): string {
  let amount = Math.max(0, Math.floor((now.getTime() - new Date(iso).getTime()) / 1000));
  for (const [unit, size] of UNITS) {
    if (amount < size) {
      return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(-amount, unit);
    }
    amount = Math.floor(amount / size);
  }
  return "";
}

/** The time a row of a list gives an item: when it was decided, once it is, or else asked. */
export function listedAt(item: Pick<Item, "state" | "created_at" | "decision">): { label: string; at: string } {
  if (item.state === "decided" && item.decision) return { label: "Decided", at: item.decision.at };
  return { label: "Asked", at: item.created_at };
}

/**
 * The paths an item asked to mount that its decision refused: all of them when it
 * refused the item, or those over a part an approval left out. The control plane's
 * `refused_mounts` says the same.
 */
export function refusedMounts(item: Item): string[] {
  const decision = item.decision;
  if (item.state !== "decided" || !decision) return [];
  const whole = !APPROVING.has(decision.action);
  return (item.patterns ?? [])
    .filter(
      (p) =>
        p.access === "mount" &&
        (whole || (p.parts ?? []).some((id) => (decision.parts?.[id] ?? "approve") !== "approve")),
    )
    .map((p) => p.resource ?? p.summary);
}

/** How long a remembered approval lasts, to follow "remembered". */
export function rememberedFor(remember: Remember): string {
  if (remember.lasts === "run") return "for the rest of this run";
  if (remember.lasts === "session") {
    return remember.until ? `for this session, until ${when(remember.until)} at the latest` : "for this session";
  }
  if (remember.lasts === "until" && remember.until) return `until ${when(remember.until)}`;
  return "until you revoke it";
}

export interface PartDecided {
  id: string;
  label: string;
  /** Whether this part was refused; null when the decision wasn't an approval. */
  refused: boolean | null;
  /** How long each remembered pattern over this part alone lasts. */
  kept: string[];
}

export interface Decided {
  parts: PartDecided[];
  /** What was remembered over the whole request or several parts, with how long. */
  broader: { summary: string; lasts: string }[];
}

/**
 * What a decision did with each part of its item, and what it remembered.
 *
 * Returns:
 *     Each part with whether it was refused and how long it is remembered, and the
 *     remembered patterns that cover more than one part. A pattern the item no longer
 *     offers is named by its id.
 */
export function decided(item: Item): Decided {
  const decision = item.decision;
  const approving = decision ? APPROVING.has(decision.action) : false;
  const patterns = new Map((item.patterns ?? []).map((p) => [p.id, p]));
  const kept = new Map<string, string[]>();
  const broader: Decided["broader"] = [];
  for (const remember of decision?.remember ?? []) {
    const pattern = patterns.get(remember.pattern);
    const lasts = rememberedFor(remember);
    const only = pattern?.parts?.length === 1 ? pattern.parts[0]! : null;
    if (only !== null && item.parts?.some((p) => p.id === only)) {
      kept.set(only, [...(kept.get(only) ?? []), lasts]);
    } else {
      broader.push({ summary: pattern?.summary ?? remember.pattern, lasts });
    }
  }
  const parts = (item.parts ?? []).map((part) => ({
    id: part.id,
    label: part.label,
    refused: approving ? (decision?.parts?.[part.id] ?? "approve") !== "approve" : null,
    kept: kept.get(part.id) ?? [],
  }));
  return { parts, broader };
}

/**
 * Whether a diff is a new file, such as a skill's first version, as the broker says.
 * Lines only added to a file that exists are a change to it, not a new file.
 */
export function isNewFile(diff: { new?: boolean } | null | undefined): boolean {
  return diff?.new === true;
}

/** Each action in the past tense, for the record of a decision and the status line. */
export const DECIDED: Record<string, string> = {
  approve: "Approved",
  reject: "Rejected",
  deny: "Denied",
  return: "Returned",
  answer: "Answered",
  decline: "Declined",
  accept: "Accepted",
  promote: "Promoted",
  request_changes: "Changes asked for",
  demote: "Demoted",
  connect: "Key sent: the broker checks it with the service, and the Connections page says once it's connected",
  disconnect: "Disconnected",
};

/** What each action that needs a note does, for the note's label. */
export const NEEDS_A_NOTE: Record<string, string> = {
  return: "to return it",
  request_changes: "to ask for changes",
};
