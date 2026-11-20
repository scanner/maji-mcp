/**
 * What the tags mean: every tag and mark the list and an item's details show, named
 * as they name it, and what it tells you.
 */
import { mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it } from "vitest";

import TagHelp from "@/components/TagHelp.vue";
import { ALONE_TAG, KINDS, RISK_REASONS, STATES, WAITING_TAG, riskTag } from "@/lib/describe";

import { violations } from "./axe";

describe("what the tags mean", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  it("explains every tag and mark an item can show", async () => {
    const help = mount(TagHelp, { attachTo: document.body });
    const terms = help.findAll("dt").map((dt) => dt.text());
    for (const tag of [ALONE_TAG, riskTag({ risk: "high" }), WAITING_TAG, "Worth a look", "(suspected)"]) {
      expect(terms).toContain(tag);
    }
    const text = help.text();
    for (const label of [KINDS.changeset, KINDS.question, ...Object.values(RISK_REASONS)]) {
      expect(text).toContain(label);
    }
    expect(await violations(help.element)).toEqual([]);
  });

  it("says the app in brackets after who asked is unchecked, and who asked is checked", () => {
    const help = mount(TagHelp);
    const term = help.findAll("dt").find((dt) => dt.text().startsWith("From"))!;
    const said = term.element.nextElementSibling!.textContent!.replace(/\s+/g, " ");
    expect(said).toContain("which is checked");
    expect(said).toContain("which nothing checks");
  });

  it("says a session's name is made from its key, and what the characters beside it are", () => {
    const help = mount(TagHelp);
    const term = help.findAll("dt").find((dt) => dt.text().startsWith("In session"))!;
    const said = term.element.nextElementSibling!.textContent!.replace(/\s+/g, " ");
    expect(said).toContain("three words made from its key");
    expect(said).toContain("the start of the key");
  });

  it("says what each state means, so a new state can't go unexplained", () => {
    const standing = mount(TagHelp)
      .findAll(".standing li")
      .map((li) => li.text().replace(/\s+/g, " "));
    expect(standing).toHaveLength(Object.keys(STATES).length);
    Object.values(STATES).forEach((label, index) => {
      expect(standing[index]).toMatch(new RegExp(`^${label}: \\w+`));
    });
  });

  it("says no tag isn't a finding of safety", () => {
    expect(mount(TagHelp).text().replace(/\s+/g, " ")).toContain(
      "An item without the tag isn't known to be safe: none of the five was found.",
    );
  });
});
