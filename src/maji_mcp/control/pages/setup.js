// The setup page: register the first passkey with the one-time code from the link.
//
// The code arrives in the URL's fragment, which a browser never sends to a server,
// and is removed from the address bar and history as soon as the page has read it.

"use strict";

const API = "/api/v0";

function show(id) {
  for (const section of document.querySelectorAll("main > section")) {
    section.hidden = section.id !== id;
  }
}

function fail(message) {
  const error = document.getElementById("error");
  error.textContent = message;
  error.hidden = false;
}

function fromBase64url(text) {
  const base64 = text.replace(/-/g, "+").replace(/_/g, "/");
  const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0)).buffer;
}

function toBase64url(buffer) {
  let text = "";
  for (const byte of new Uint8Array(buffer)) text += String.fromCharCode(byte);
  return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function post(path, body) {
  const response = await fetch(API + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "same-origin",
  });
  const data = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error((data && data.title) || `the control plane answered ${response.status}`);
  }
  return data;
}

async function register(code, name) {
  const options = await post("/passkeys/registration/options", { name });
  const publicKey = options.public_key;
  publicKey.challenge = fromBase64url(publicKey.challenge);
  publicKey.user.id = fromBase64url(publicKey.user.id);
  for (const credential of publicKey.excludeCredentials || []) {
    credential.id = fromBase64url(credential.id);
  }
  const made = await navigator.credentials.create({ publicKey });
  const credential = {
    id: made.id,
    rawId: toBase64url(made.rawId),
    type: made.type,
    response: {
      attestationObject: toBase64url(made.response.attestationObject),
      clientDataJSON: toBase64url(made.response.clientDataJSON),
      transports: made.response.getTransports ? made.response.getTransports() : [],
    },
    clientExtensionResults: made.getClientExtensionResults ? made.getClientExtensionResults() : {},
    authenticatorAttachment: made.authenticatorAttachment || null,
  };
  return post("/passkeys", {
    registration_id: options.registration_id,
    credential,
    setup_code: code,
  });
}

function showCodes(codes) {
  const list = document.getElementById("code-list");
  list.replaceChildren(
    ...codes.map((code) => {
      const item = document.createElement("li");
      item.textContent = code;
      return item;
    }),
  );
  document.getElementById("copy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(codes.join("\n"));
      document.getElementById("copied").hidden = false;
    } catch {
      const range = document.createRange();
      range.selectNodeContents(list);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }
  });
  document.getElementById("saved").addEventListener("change", (event) => {
    document.getElementById("finished").hidden = !event.target.checked;
  });
  show("codes");
}

async function start() {
  const code = new URLSearchParams(location.hash.slice(1)).get("code");
  if (location.hash) history.replaceState(null, "", location.pathname);

  let health;
  try {
    health = await (await fetch(API + "/health")).json();
  } catch {
    fail("The control plane didn't answer. Is the stack up?");
    return;
  }
  if (!health.setup_needed) return show("already");
  if (!code) return show("no-code");
  if (!window.PublicKeyCredential) {
    fail("This browser can't create passkeys. Open the link in Safari, Chrome or Firefox.");
    return;
  }

  show("register");
  document.getElementById("form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = document.getElementById("create");
    button.disabled = true;
    document.getElementById("error").hidden = true;
    try {
      const made = await register(code, document.getElementById("name").value.trim());
      showCodes(made.recovery_codes || []);
    } catch (error) {
      button.disabled = false;
      fail(
        error.name === "NotAllowedError"
          ? "The passkey wasn't created. Try again when you're ready to use Touch ID or your PIN."
          : `${error.message}. If the link has expired, run make up again for a new one.`,
      );
    }
  });
}

start();
