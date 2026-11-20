/**
 * A tab's own signing key, for deciding while deciding is unlocked in the tab.
 *
 * WebCrypto makes an ECDSA P-256 key pair whose private key can't be exported, and
 * the page keeps it only in memory, so it is gone when the tab reloads or closes. Its
 * signatures are r and s side by side, over SHA-256 of what it signs, which is how the
 * control plane verifies them (`api/openapi.yaml`, "Deciding in an unlocked tab").
 */
import { fromBase64url, toBase64url } from "@/api/webauthn";

export interface TabKey {
  readonly pair: CryptoKeyPair;
  /** The public key, as the control plane receives it. */
  readonly jwk: JsonWebKey;
}

export async function makeTabKey(): Promise<TabKey> {
  const pair = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, [
    "sign",
    "verify",
  ]);
  return { pair, jwk: await crypto.subtle.exportKey("jwk", pair.publicKey) };
}

/** Sign a challenge, given and returned as base64url. */
export async function signWith(key: TabKey, challenge: string): Promise<string> {
  const signature = await crypto.subtle.sign(
    { name: "ECDSA", hash: "SHA-256" },
    key.pair.privateKey,
    fromBase64url(challenge),
  );
  return toBase64url(signature);
}
