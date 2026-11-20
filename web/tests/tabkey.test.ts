// @vitest-environment node
/**
 * The tab's key, with the real WebCrypto: its private half can't leave the page, and
 * its signatures have the shape the control plane verifies.
 */
import { describe, expect, it } from "vitest";

import { makeTabKey, signWith } from "@/api/tabkey";
import { fromBase64url } from "@/api/webauthn";

describe("a tab's key", () => {
  it("can't be exported, and gives the control plane only its public half", async () => {
    const key = await makeTabKey();
    expect(key.pair.privateKey.extractable).toBe(false);
    await expect(crypto.subtle.exportKey("jwk", key.pair.privateKey)).rejects.toThrow();
    expect(key.jwk).toMatchObject({ kty: "EC", crv: "P-256" });
    expect(key.jwk.d).toBeUndefined();
  });

  it("signs a challenge as r and s side by side, over SHA-256", async () => {
    const key = await makeTabKey();
    const challenge = "AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA";
    const signature = fromBase64url(await signWith(key, challenge));
    expect(signature.byteLength).toBe(64);
    const verified = await crypto.subtle.verify(
      { name: "ECDSA", hash: "SHA-256" },
      key.pair.publicKey,
      signature,
      fromBase64url(challenge),
    );
    expect(verified).toBe(true);
  });
});
