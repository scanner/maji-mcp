/**
 * The control plane's REST API, typed from its contract (api/openapi.yaml).
 *
 * The types are generated into schema.d.ts on every build and check, so a client
 * that drifts from the contract fails to compile. Requests go to the same origin,
 * with the session cookie; the browser adds the Origin header the control plane
 * asks for on every request that changes something.
 */
import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

export type Schemas = components["schemas"];
export type Item = Schemas["Item"];
export type Decision = Schemas["Decision"];
export type DecisionChoice = Schemas["DecisionChoice"];
export type Action = Schemas["Action"];
export type Pattern = Schemas["Pattern"];
export type Problem = Schemas["Problem"];
export type Grant = Schemas["Grant"];
export type Passkey = Schemas["Passkey"];
export type Lasts = Schemas["Lasts"];
export type Remember = Schemas["Remember"];
export type Connection = Schemas["Connection"];
export type Service = Schemas["Service"];

/**
 * Keys pasted into Connect items, by item id. They go only into the request that
 * decides, never into a store, storage or an address.
 */
export type Pasted = Record<string, string>;

// The base is the page's own origin, written out: a browser would resolve "/api/v0"
// itself, but Request elsewhere, as in tests, needs an absolute URL.
//
export const api = createClient<paths>({
  baseUrl: `${globalThis.location.origin}/api/v0`,
  credentials: "same-origin",
});

/** A refusal from the control plane, with its contract error code. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly data: unknown;

  constructor(status: number, problem: Partial<Problem> | undefined) {
    super(problem?.title ?? `the control plane answered ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = problem?.code ?? "internal";
    this.data = problem?.data;
  }
}

/** The data of a successful call, or an ApiError for anything else. */
export function ok<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (!result.response.ok || result.data === undefined) {
    throw new ApiError(result.response.status, result.error as Partial<Problem> | undefined);
  }
  return result.data;
}

/** Whether a call succeeded, for routes that answer with no body (204). */
export function done(result: { error?: unknown; response: Response }): void {
  if (!result.response.ok) {
    throw new ApiError(result.response.status, result.error as Partial<Problem> | undefined);
  }
}
