/**
 * Passkeys in the browser: turn the control plane's JSON options into what
 * navigator.credentials expects, and the credential it returns back into JSON.
 *
 * WebAuthn works in bytes; the contract sends them as base64url text.
 */

export function fromBase64url(text: string): ArrayBuffer {
  const base64 = text.replace(/-/g, "+").replace(/_/g, "/");
  const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

export function toBase64url(buffer: ArrayBuffer | ArrayBufferView): string {
  const bytes =
    buffer instanceof ArrayBuffer
      ? new Uint8Array(buffer)
      : new Uint8Array(buffer.buffer, buffer.byteOffset, buffer.byteLength);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

interface RequestOptionsJSON {
  challenge: string;
  rpId?: string;
  timeout?: number;
  userVerification?: UserVerificationRequirement;
  allowCredentials?: { id: string; type: string; transports?: string[] }[];
}

/** The options for navigator.credentials.get, from the control plane's JSON. */
export function requestOptions(json: Record<string, unknown>): PublicKeyCredentialRequestOptions {
  const options = json as unknown as RequestOptionsJSON;
  const request: PublicKeyCredentialRequestOptions = {
    challenge: fromBase64url(options.challenge),
    allowCredentials: (options.allowCredentials ?? []).map((credential) => ({
      id: fromBase64url(credential.id),
      type: "public-key",
      transports: credential.transports as AuthenticatorTransport[] | undefined,
    })),
  };
  if (options.rpId !== undefined) request.rpId = options.rpId;
  if (options.timeout !== undefined) request.timeout = options.timeout;
  if (options.userVerification !== undefined) {
    request.userVerification = options.userVerification;
  }
  return request;
}

/** An assertion as the JSON the control plane verifies. */
export function assertionJSON(credential: PublicKeyCredential): Record<string, unknown> {
  const response = credential.response as AuthenticatorAssertionResponse;
  return {
    id: credential.id,
    rawId: toBase64url(credential.rawId),
    type: credential.type,
    response: {
      authenticatorData: toBase64url(response.authenticatorData),
      clientDataJSON: toBase64url(response.clientDataJSON),
      signature: toBase64url(response.signature),
      userHandle: response.userHandle ? toBase64url(response.userHandle) : null,
    },
    clientExtensionResults: credential.getClientExtensionResults(),
  };
}

/**
 * Ask the authenticator to sign a challenge: Touch ID, a PIN, or a phone nearby.
 *
 * Returns:
 *     The assertion as JSON, or null if the person cancelled
 */
export async function sign(options: Record<string, unknown>): Promise<Record<string, unknown> | null> {
  try {
    const credential = await navigator.credentials.get({ publicKey: requestOptions(options) });
    return credential ? assertionJSON(credential as PublicKeyCredential) : null;
  } catch (error) {
    if (error instanceof DOMException && error.name === "NotAllowedError") return null;
    throw error;
  }
}

interface CreationOptionsJSON {
  challenge: string;
  rp: { id?: string; name: string };
  user: { id: string; name: string; displayName: string };
  pubKeyCredParams: { type: string; alg: number }[];
  timeout?: number;
  excludeCredentials?: { id: string; type: string; transports?: string[] }[];
  authenticatorSelection?: AuthenticatorSelectionCriteria;
  attestation?: AttestationConveyancePreference;
}

/** The options for navigator.credentials.create, from the control plane's JSON. */
export function creationOptions(json: Record<string, unknown>): PublicKeyCredentialCreationOptions {
  const options = json as unknown as CreationOptionsJSON;
  const creation: PublicKeyCredentialCreationOptions = {
    challenge: fromBase64url(options.challenge),
    rp: options.rp,
    user: { ...options.user, id: fromBase64url(options.user.id) },
    pubKeyCredParams: options.pubKeyCredParams as PublicKeyCredentialParameters[],
    excludeCredentials: (options.excludeCredentials ?? []).map((credential) => ({
      id: fromBase64url(credential.id),
      type: "public-key",
      transports: credential.transports as AuthenticatorTransport[] | undefined,
    })),
  };
  if (options.timeout !== undefined) creation.timeout = options.timeout;
  if (options.authenticatorSelection) creation.authenticatorSelection = options.authenticatorSelection;
  if (options.attestation) creation.attestation = options.attestation;
  return creation;
}

/** A new credential as the JSON the control plane registers. */
export function registrationJSON(credential: PublicKeyCredential): Record<string, unknown> {
  const response = credential.response as AuthenticatorAttestationResponse;
  return {
    id: credential.id,
    rawId: toBase64url(credential.rawId),
    type: credential.type,
    response: {
      attestationObject: toBase64url(response.attestationObject),
      clientDataJSON: toBase64url(response.clientDataJSON),
      transports: response.getTransports ? response.getTransports() : [],
    },
    clientExtensionResults: credential.getClientExtensionResults(),
    authenticatorAttachment: credential.authenticatorAttachment ?? null,
  };
}

/**
 * Ask the device to make a new passkey.
 *
 * Returns:
 *     The credential as JSON, or null if the person cancelled
 */
export async function create(options: Record<string, unknown>): Promise<Record<string, unknown> | null> {
  try {
    const credential = await navigator.credentials.create({ publicKey: creationOptions(options) });
    return credential ? registrationJSON(credential as PublicKeyCredential) : null;
  } catch (error) {
    if (error instanceof DOMException && error.name === "NotAllowedError") return null;
    throw error;
  }
}
