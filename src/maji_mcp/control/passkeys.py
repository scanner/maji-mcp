"""
The routes that need a passkey: deciding, unlocking deciding in a tab, signing in,
and adding or removing passkeys.

Verification is py_webauthn's (`webauthn` on PyPI). Every assertion is checked
against the control plane's exact origin and relying party ID, with the user verified
(`api/openapi.yaml`, "Deciding"). What it signs comes from `canonical.py`: for a
decision, a challenge built from the hashes of exactly the choices you made; for an
unlock, the tab's key and how long it may be idle. A tab that is unlocked signs each
decision's challenge with that key instead (`unlock.py`), except a decision that
lasts beyond its run or session, which always needs the passkey.

A challenge, a setup code and a recovery code each work once; the store claims them
in a statement that only succeeds while they are unused.
"""

# system imports
import hashlib
import hmac
import secrets
from typing import Any

# 3rd party imports
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, options_to_json_dict
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidRegistrationResponse,
)
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

# project imports
from maji_mcp.common.protocol import MajiError
from maji_mcp.control import canonical, unlock
from maji_mcp.control.api import (
    SESSION_COOKIE,
    Settings,
    _json,
    open_route,
    problem,
    signed_in,
)
from maji_mcp.control.store import (
    RECONSIDER_FIELDS,
    ControlStore,
    b64url,
    secret_hash,
)

# A session signed in this recently may add or remove a passkey.
#
FRESH_SECONDS = 5 * 60
RECOVERY_CODES = 10

# What a decision made in an unlocked tab can't do without the passkey: remember an
# approval past its run or session, or connect a service. These last. A session's
# grant that ends at a time is still `session`, and covers no more than the session.
#
LASTING = frozenset({"until", "permanent"})
TOUCH_KINDS = frozenset({"connect"})
CODE_ALPHABET = "23456789abcdefghijkmnpqrstuvwxyz"
CHOICE_FIELDS = {
    "item": str,
    "content_hash": str,
    "action": str,
    "parts": dict,
    "answer": dict,
    "remember": list,
    "note": str,
    "supersedes": str,
    "credential_fingerprint": str,
    "credential_settings": dict,
}

# The longest key a Connect item takes, and how many keys one request may carry.
#
MAX_KEY = 4096
MAX_KEYS = 10


class Refused(Exception):
    """A proof that didn't hold; answered with 403 and nothing else said."""


def recovery_code() -> str:
    """Twelve characters in three groups, from an alphabet without look-alikes."""
    chars = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
    return f"{chars[:4]}-{chars[4:8]}-{chars[8:]}"


def _normal(code: str) -> str:
    return code.strip().lower().replace("-", "").replace(" ", "")


def _choices(body: dict[str, Any]) -> list[dict[str, Any]]:
    choices = body.get("decisions")
    if not isinstance(choices, list) or not 1 <= len(choices) <= 100:
        raise MajiError("invalid_argument", "decisions is a list of 1 to 100 choices")
    clean = []
    for choice in choices:
        if not isinstance(choice, dict):
            raise MajiError("invalid_argument", "each decision is an object")
        unknown = set(choice) - set(CHOICE_FIELDS)
        if unknown:
            raise MajiError("invalid_argument", f"unknown fields {sorted(unknown)}")
        for field, kind in CHOICE_FIELDS.items():
            value = choice.get(field)
            if value is not None and not isinstance(value, kind):
                raise MajiError("invalid_argument", f"{field} has the wrong type")
        for field in ("item", "content_hash", "action"):
            if not choice.get(field):
                raise MajiError("invalid_argument", f"each decision needs {field}")
        clean.append({k: v for k, v in choice.items() if v is not None})
    if len({c["item"] for c in clean}) != len(clean):
        raise MajiError("invalid_argument", "one decision per item")
    return clean


def _pasted(value: Any) -> dict[str, str]:
    """
    Check the keys pasted with a request, by item id: printable text, without space
    at either end. A refusal never names or quotes a key.

    Raises:
        MajiError: `invalid_argument` for anything else
    """
    if value is None:
        return {}
    if (
        not isinstance(value, dict)
        or len(value) > MAX_KEYS
        or not all(
            isinstance(k, str)
            and isinstance(v, str)
            and 1 <= len(v) <= MAX_KEY
            and v.isprintable()
            and v == v.strip()
            for k, v in value.items()
        )
    ):
        raise MajiError(
            "invalid_argument",
            f"secrets are keys by item id, each 1 to {MAX_KEY} printable characters",
        )
    return value


def _fingerprint(key: str) -> str:
    """A key's fingerprint, as a Connect choice names it: its SHA-256."""
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()


def routes(store: ControlStore, settings: Settings) -> list[Route]:
    """The passkey routes, for the API's route list."""

    def descriptors() -> list[PublicKeyCredentialDescriptor]:
        return [
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(p["id"]))
            for p in store.list_passkeys()
        ]

    def assertion_options(challenge: bytes) -> dict[str, Any]:
        options = generate_authentication_options(
            rp_id=settings.rp_id,
            challenge=challenge,
            allow_credentials=descriptors(),
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return dict(options_to_json_dict(options))

    def verify(assertion: Any, challenge: bytes) -> dict[str, Any]:
        """
        Check an assertion against the challenge issued, this origin and a passkey.

        Returns:
            The passkey, with the assertion's parts for the decision record

        Raises:
            Refused: anything didn't hold
        """
        if not isinstance(assertion, dict) or not isinstance(assertion.get("id"), str):
            raise Refused
        passkey = store.get_passkey(assertion["id"])
        if passkey is None:
            raise Refused
        try:
            verified = verify_authentication_response(
                credential=assertion,
                expected_challenge=challenge,
                expected_rp_id=settings.rp_id,
                expected_origin=settings.origin,
                credential_public_key=passkey["public_key"],
                credential_current_sign_count=passkey["sign_count"],
                require_user_verification=True,
            )
        except InvalidAuthenticationResponse, ValueError, KeyError, TypeError:
            raise Refused from None
        store.passkey_used(passkey["id"], verified.new_sign_count)
        response = assertion.get("response", {})
        return {
            "passkey": passkey["id"],
            "authenticator_data": response.get("authenticatorData"),
            "client_data_json": response.get("clientDataJSON"),
            "signature": response.get("signature"),
        }

    def refused(title: str = "the proof didn't hold") -> JSONResponse:
        return problem(403, "permission_denied", title)

    def locked() -> JSONResponse:
        return problem(
            403,
            "permission_denied",
            "deciding has locked in this tab; your passkey unlocks it",
            {"reason": "locked"},
        )

    def tab_session(request: Request) -> str | None:
        """The session of a signed-in browser; a token never unlocks anything."""
        auth = request.state.auth
        if auth is None or auth["kind"] != "session":
            return None
        return str(auth["session_id"])

    def needs_passkey(choices: list[dict[str, Any]]) -> bool:
        """
        Whether a decision needs a passkey touch even in an unlocked tab: it lasts
        beyond its run or session, connects a service, or decides a high-risk item.
        A choice reconsidering a decision, such as demoting a skill, takes something
        back and carries a note at most (`check_choice`), so it needs none.
        """
        for choice in choices:
            if "supersedes" in choice and set(choice) <= RECONSIDER_FIELDS:
                continue
            if any(r.get("lasts") in LASTING for r in choice.get("remember") or []):
                return True
            item = store.get_item(choice["item"])
            if item is not None and (
                item["kind"] in TOUCH_KINDS or item.get("risk") == "high"
            ):
                return True
        return False

    def unlocked_proof(
        request: Request, given: Any, issued: dict[str, Any], hashes: list[str]
    ) -> tuple[dict[str, Any], dict[str, Any]] | JSONResponse:
        """
        Check a decision signed by an unlocked tab's key.

        Returns:
            Who decided and the signature to record, or the response refusing it
        """
        session_id = tab_session(request)
        if (
            session_id is None
            or not isinstance(given, dict)
            or not isinstance(given.get("id"), str)
        ):
            return refused("only a tab your passkey unlocked decides without it")
        live = store.live_unlock(given["id"], session_id)
        if live is None:
            return locked()
        if needs_passkey(issued["payload"]["choices"]):
            return problem(
                403,
                "permission_denied",
                "this decision lasts beyond its run or session, so it needs your "
                "passkey",
                {"reason": "needs_passkey"},
            )
        certificate = live["certificate"]
        if not unlock.verify(
            certificate["key"], issued["challenge"], given.get("signature")
        ):
            return refused()
        store.unlock_used(live["id"], session_id)
        by = {"kind": "you", "passkey": live["passkey"], "unlock": live["id"]}
        signature = {
            "passkey": live["passkey"],
            "nonce": issued["payload"]["nonce"],
            "batch": hashes,
            "signature": given["signature"],
            "unlock": {"id": live["id"], **certificate},
        }
        return by, signature

    ###########################################################################
    # Deciding

    @signed_in
    async def challenge_decisions(request: Request) -> Response:
        choices = _choices(await _json(request))
        items = [store.check_choice(choice) for choice in choices]

        # A Connect decision takes a passkey touch of its own, so the touch you make
        # is for that service and nothing else.
        #
        if len(choices) > 1 and any(item["kind"] == "connect" for item in items):
            raise MajiError("invalid_argument", "a Connect decision is made on its own")
        hashes = [canonical.decision_hash(c) for c in choices]
        nonce = secrets.token_urlsafe(16)
        challenge = canonical.challenge(nonce, hashes)
        challenge_id = secrets.token_urlsafe(16)
        store.add_challenge(
            challenge_id, "decide", challenge, {"nonce": nonce, "choices": choices}
        )
        return JSONResponse(
            {
                "challenge_id": challenge_id,
                "nonce": nonce,
                "decision_hashes": hashes,
                "public_key": assertion_options(challenge),
            }
        )

    @signed_in
    async def decide(request: Request) -> Response:
        body = await _json(request)
        pasted = _pasted(body.get("secrets"))
        if ("assertion" in body) == ("unlock" in body):
            raise MajiError(
                "invalid_argument",
                "a decision carries a passkey assertion or an unlocked tab's signature",
            )
        issued = store.use_challenge(str(body.get("challenge_id", "")), "decide")
        if issued is None:
            return problem(410, "stale", "the challenge was used already, or expired")
        choices = issued["payload"]["choices"]
        hashes = [canonical.decision_hash(c) for c in choices]
        if "unlock" in body:
            checked = unlocked_proof(request, body["unlock"], issued, hashes)
            if isinstance(checked, JSONResponse):
                return checked
            by, signature = checked
        else:
            try:
                proof = verify(body.get("assertion"), issued["challenge"])
            except Refused:
                return refused()
            by = {"kind": "you", "passkey": proof["passkey"]}
            signature = {**proof, "nonce": issued["payload"]["nonce"], "batch": hashes}

        # Each key pasted goes with the Connect choice you signed: its SHA-256 is the
        # fingerprint the signature covers. Only then is anything recorded, and the
        # key is held for the broker, never recorded itself.
        #
        keyed = {c["item"]: c for c in choices if "credential_fingerprint" in c}
        if set(pasted) != set(keyed) or not all(
            hmac.compare_digest(
                _fingerprint(key), keyed[item]["credential_fingerprint"]
            )
            for item, key in pasted.items()
        ):
            raise MajiError(
                "invalid_argument",
                "each key pasted has to be the one whose fingerprint you signed",
            )
        decisions = store.record_decisions(choices, by, signature)
        for decision in decisions:
            if decision["item"] in pasted:
                store.keys.hold(decision["id"], pasted[decision["item"]])
        return JSONResponse({"decisions": decisions}, status_code=201)

    ###########################################################################
    # Unlocked deciding

    @signed_in
    async def unlock_options(request: Request) -> Response:
        session_id = tab_session(request)
        if session_id is None:
            return refused("only a signed-in browser can unlock deciding")
        key = unlock.check_key((await _json(request)).get("key"))
        nonce = secrets.token_urlsafe(16)
        idle = settings.unlock_idle_seconds
        challenge = canonical.unlock_challenge(
            nonce, canonical.key_thumbprint(key), idle
        )
        challenge_id = secrets.token_urlsafe(16)
        store.add_challenge(
            challenge_id,
            "unlock",
            challenge,
            {
                "nonce": nonce,
                "key": key,
                "idle_seconds": idle,
                "session": secret_hash(session_id),
            },
        )
        return JSONResponse(
            {
                "challenge_id": challenge_id,
                "nonce": nonce,
                "idle_seconds": idle,
                "public_key": assertion_options(challenge),
            }
        )

    @signed_in
    async def unlock_tab(request: Request) -> Response:
        session_id = tab_session(request)
        if session_id is None:
            return refused("only a signed-in browser can unlock deciding")
        body = await _json(request)
        issued = store.use_challenge(str(body.get("challenge_id", "")), "unlock")
        if issued is None:
            return problem(410, "stale", "the challenge was used already, or expired")
        payload = issued["payload"]
        if payload["session"] != secret_hash(session_id):
            return refused("that challenge was for another session")
        try:
            proof = verify(body.get("assertion"), issued["challenge"])
        except Refused:
            return refused()
        certificate = {
            "key": payload["key"],
            "nonce": payload["nonce"],
            "idle_seconds": payload["idle_seconds"],
            **proof,
        }
        made = store.add_unlock(
            session_id, proof["passkey"], certificate, payload["idle_seconds"]
        )
        return JSONResponse(made, status_code=201)

    @signed_in
    async def unlock_active(request: Request) -> Response:
        session_id = tab_session(request)
        if session_id is None or not store.unlock_used(
            request.path_params["unlock_id"], session_id
        ):
            return locked()
        return Response(status_code=204)

    @signed_in
    async def lock_tab(request: Request) -> Response:
        session_id = tab_session(request)
        if session_id is None:
            return refused("only a signed-in browser has a tab to lock")
        store.lock(request.path_params["unlock_id"], session_id)
        return Response(status_code=204)

    ###########################################################################
    # Signing in

    @open_route
    async def session_options(request: Request) -> Response:
        challenge = secrets.token_bytes(32)
        challenge_id = secrets.token_urlsafe(16)
        store.add_challenge(challenge_id, "session", challenge, {})
        return JSONResponse(
            {"challenge_id": challenge_id, "public_key": assertion_options(challenge)}
        )

    @open_route
    async def sign_in(request: Request) -> Response:
        body = await _json(request)
        issued = store.use_challenge(str(body.get("challenge_id", "")), "session")
        if issued is None:
            return refused("the challenge was used already, or expired")
        try:
            proof = verify(body.get("assertion"), issued["challenge"])
        except Refused:
            return refused()
        session_id = secrets.token_urlsafe(32)
        store.start_session(session_id, proof["passkey"])
        response = Response(status_code=204)
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            max_age=8 * 60 * 60,
            path="/",
            httponly=True,
            samesite="strict",
            secure=settings.secure,
        )
        return response

    async def sign_out(request: Request) -> Response:
        auth = request.state.auth
        if auth is None or auth["kind"] != "session":
            return problem(401, "unauthenticated", "no session to end")
        store.end_session(auth["session_id"])
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    ###########################################################################
    # Passkeys

    @signed_in
    async def list_passkeys(request: Request) -> Response:
        return JSONResponse({"passkeys": store.list_passkeys()})

    @open_route
    async def registration_options(request: Request) -> Response:
        body = await _json(request)
        name = body.get("name")
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise MajiError("invalid_argument", "name is 1 to 100 characters")
        options = generate_registration_options(
            rp_id=settings.rp_id,
            rp_name=settings.rp_name,
            user_name=f"maji-mcp on {store.instance}",
            user_id=hashlib.sha256(f"maji-mcp:{store.instance}".encode()).digest(),
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=descriptors(),
        )
        registration_id = secrets.token_urlsafe(16)
        store.add_challenge(
            registration_id, "register", options.challenge, {"name": name}
        )
        return JSONResponse(
            {
                "registration_id": registration_id,
                "public_key": dict(options_to_json_dict(options)),
            }
        )

    @open_route
    async def register(request: Request) -> Response:
        body = await _json(request)
        auth = request.state.auth
        first = store.passkey_count() == 0
        setup_code = body.get("setup_code")
        recovery = body.get("recovery_code")

        # Which proof this registration rests on: the setup code while no passkey
        # exists, else a recovery code, else a session signed in moments ago. It is
        # spent only once the new passkey has verified.
        #
        if first:
            if not isinstance(setup_code, str):
                return refused("the first passkey needs the setup code")
            proof = "setup"
        elif isinstance(recovery, str):
            proof = "recovery"
        elif (
            auth is not None
            and auth["kind"] == "session"
            and store.clock() - auth["signed_in"] < FRESH_SECONDS
        ):
            proof = "session"
        else:
            return refused("sign in again, within five minutes, to add a passkey")

        credential = body.get("credential")
        if not isinstance(credential, dict):
            return refused()
        issued = store.use_challenge(str(body.get("registration_id", "")), "register")
        if issued is None:
            return refused("the registration was used already, or expired")
        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=issued["challenge"],
                expected_rp_id=settings.rp_id,
                expected_origin=settings.origin,
                require_user_verification=True,
            )
        except InvalidRegistrationResponse, ValueError, KeyError, TypeError:
            return refused()
        if proof == "setup" and not store.use_setup_code(_normal(str(setup_code))):
            return refused("the setup code is wrong, spent or expired")
        if proof == "recovery" and not store.use_recovery_code(_normal(str(recovery))):
            return refused("the recovery code is wrong or spent")

        passkey = store.add_passkey(
            b64url(verified.credential_id),
            issued["payload"]["name"],
            verified.credential_public_key,
            verified.sign_count,
        )
        result: dict[str, Any] = {
            k: v for k, v in passkey.items() if k not in ("public_key", "sign_count")
        }
        if first:
            codes = [recovery_code() for _ in range(RECOVERY_CODES)]
            store.add_recovery_codes([_normal(c) for c in codes])
            result["recovery_codes"] = codes
        return JSONResponse(result, status_code=201)

    @signed_in
    async def remove_passkey(request: Request) -> Response:
        auth = request.state.auth
        if (
            auth["kind"] != "session"
            or store.clock() - auth["signed_in"] >= FRESH_SECONDS
        ):
            return refused("sign in again, within five minutes, to remove a passkey")
        if store.get_passkey(request.path_params["passkey_id"]) is None:
            raise MajiError("not_found", "no such passkey")
        store.remove_passkey(request.path_params["passkey_id"])
        return Response(status_code=204)

    return [
        Route("/decisions/challenge", challenge_decisions, methods=["POST"]),
        Route("/decisions", decide, methods=["POST"]),
        Route("/unlocks/options", unlock_options, methods=["POST"]),
        Route("/unlocks", unlock_tab, methods=["POST"]),
        Route("/unlocks/{unlock_id}/active", unlock_active, methods=["POST"]),
        Route("/unlocks/{unlock_id}", lock_tab, methods=["DELETE"]),
        Route("/session/options", session_options, methods=["POST"]),
        Route("/session", sign_in, methods=["POST"]),
        Route("/session", sign_out, methods=["DELETE"]),
        Route("/passkeys", list_passkeys, methods=["GET"]),
        Route("/passkeys", register, methods=["POST"]),
        Route("/passkeys/registration/options", registration_options, methods=["POST"]),
        Route("/passkeys/{passkey_id}", remove_passkey, methods=["DELETE"]),
    ]
