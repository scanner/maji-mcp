/**
 * The conversions between the control plane's base64url JSON and WebAuthn's bytes.
 */
import { describe, expect, it } from "vitest";

import { fromBase64url, requestOptions, toBase64url } from "@/api/webauthn";

describe("base64url", () => {
  it("turns bytes into text and back, without padding or + and /", () => {
    const bytes = new Uint8Array([251, 255, 0, 62, 63, 1]);
    const text = toBase64url(bytes);
    expect(text).not.toMatch(/[+/=]/);
    expect(new Uint8Array(fromBase64url(text))).toEqual(bytes);
  });
});

describe("requestOptions", () => {
  it("decodes the challenge and each allowed credential's id", () => {
    const options = requestOptions({
      challenge: "AQID",
      rpId: "localhost",
      userVerification: "required",
      allowCredentials: [{ id: "BAUG", type: "public-key" }],
    });
    expect(new Uint8Array(options.challenge as ArrayBuffer)).toEqual(new Uint8Array([1, 2, 3]));
    expect(new Uint8Array(options.allowCredentials![0]!.id as ArrayBuffer)).toEqual(
      new Uint8Array([4, 5, 6]),
    );
    expect(options.userVerification).toBe("required");
  });
});
