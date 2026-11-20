/**
 * Grants: what agents may do without asking you again. Counted in groups, found by a
 * fuzzy search, and revoked one at a time or a group at once. Revoking narrows
 * access, so it needs no passkey.
 */
import { defineStore } from "pinia";
import { ref } from "vue";

import { api, ok } from "@/api/client";
import type { Grant, Schemas } from "@/api/client";

export type GroupBy = "resource" | "principal" | "access" | "lasts" | "session";

/** The group of grants without a value along the dimension grouped by. */
export const NONE = "(none)";

/**
 * What a grant has along a dimension, or null when it has none. Every grant records
 * the session it was made in, but only one that lasts for the session is held by it.
 */
function along(grant: Grant, by: GroupBy): string | null {
  if (by === "resource") return grant.pattern.resource ?? null;
  if (by === "access") return grant.pattern.access;
  if (by === "session") return grant.lasts === "session" ? (grant.session ?? null) : null;
  return grant[by];
}

export const useGrants = defineStore("grants", () => {
  const by = ref<GroupBy>("resource");
  const query = ref("");
  const groups = ref<Schemas["GrantGroup"][]>([]);
  const grants = ref<Grant[]>([]);
  const group = ref<string | null>(null);

  function filter(): Record<string, string> {
    return group.value === null || group.value === NONE ? {} : { [by.value]: group.value };
  }

  async function load(): Promise<void> {
    const q = query.value.trim() || undefined;
    const [counted, listed] = await Promise.all([
      api.GET("/grants/groups", { params: { query: { by: by.value, q } } }),
      api.GET("/grants", { params: { query: { q, limit: 200, ...filter() } } }),
    ]);
    groups.value = ok(counted).groups;
    // The API can't ask for grants without a value, so that group is picked out here.
    //
    const all = ok(listed).grants;
    grants.value = group.value === NONE ? all.filter((g) => along(g, by.value) === null) : all;
  }

  async function revoke(ids: string[]): Promise<number> {
    const result = ok(await api.POST("/grants/revocations", { body: { grant_ids: ids } }));
    await load();
    return result.revoked;
  }

  /** Revoke every active grant in a group, without waiting for an agent to use one. */
  async function revokeGroup(key: string): Promise<number> {
    const result = ok(
      await api.POST("/grants/revocations", { body: { filter: { [by.value]: key } } }),
    );
    group.value = null;
    await load();
    return result.revoked;
  }

  return { by, query, groups, grants, group, load, revoke, revokeGroup };
});
