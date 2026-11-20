"""
The control plane's database: the queue, the decisions, grants, passkeys and the
one-time values sign-in uses.

One SQLite file per instance, in WAL mode. Every call runs synchronously on the event
loop's thread, which owns the connection: at one person's pace the calls take well
under a millisecond, and keeping to one thread keeps SQLite's thread rules simple.

The rules that make a decision trustworthy are kept by the database itself, so no
code path can skip them:
- **Decisions, their outcomes, grants, revocations and events are append-only.**
  Triggers refuse to update or delete them. Reconsidering is a new record.
- **An item is decided once.** Its state moves from waiting only if its content hash
  is still the one decided on, checked in the same statement.
- **A batch of decisions lands whole or not at all,** in one transaction.
- **Challenges, setup codes and recovery codes are used once,** claimed by a statement
  that only succeeds while they are unused.
- **Secrets are stored as hashes:** client tokens, session ids, setup and recovery
  codes.
- **The database belongs to one instance.** Its id is recorded on creation, and the
  store refuses to open it as any other.
"""

# system imports
import base64
import hashlib
import json
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

# project imports
from maji_mcp.common.names import session_name
from maji_mcp.common.protocol import MajiError
from maji_mcp.control import canonical, fuzzy, ids
from maji_mcp.control.handoff import KeyHandoff

CHALLENGE_SECONDS = 5 * 60
SETUP_CODE_SECONDS = 10 * 60
SESSION_SECONDS = 8 * 60 * 60

# A session may hold this many unlocked tabs at once; unlocking another locks the
# oldest.
#
UNLOCKS_PER_SESSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'waiting',
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actions TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    blocking INTEGER NOT NULL DEFAULT 0,
    thread TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS items_by_state ON items(state, closed_at);
CREATE TABLE IF NOT EXISTS notes (
    item_id TEXT NOT NULL REFERENCES items(id),
    author TEXT NOT NULL,
    text TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id),
    body TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_by_item ON decisions(item_id);
CREATE TABLE IF NOT EXISTS decision_outcomes (
    decision_id TEXT NOT NULL REFERENCES decisions(id),
    outcome TEXT NOT NULL,
    detail TEXT,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (
    id TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    pattern TEXT NOT NULL,
    access TEXT NOT NULL,
    resource TEXT,
    lasts TEXT NOT NULL,
    until TEXT,
    run TEXT,
    session TEXT,
    granted_at TEXT NOT NULL,
    decision_id TEXT REFERENCES decisions(id)
);
CREATE TABLE IF NOT EXISTS asking_again (
    item_id TEXT PRIMARY KEY REFERENCES items(id),
    at TEXT NOT NULL,
    by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS grant_revocations (
    grant_id TEXT PRIMARY KEY REFERENCES grants(id),
    at TEXT NOT NULL,
    by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_rules (
    id TEXT PRIMARY KEY,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS passkeys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    public_key BLOB NOT NULL,
    sign_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS challenges (
    id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL,
    challenge BLOB NOT NULL,
    payload TEXT NOT NULL,
    created REAL NOT NULL,
    used REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY,
    passkey_id TEXT NOT NULL,
    created REAL NOT NULL,
    expires REAL NOT NULL,
    ended REAL
);
CREATE TABLE IF NOT EXISTS unlocks (
    id TEXT PRIMARY KEY,
    session_hash TEXT NOT NULL,
    passkey_id TEXT NOT NULL,
    certificate TEXT NOT NULL,
    created REAL NOT NULL,
    last_active REAL NOT NULL,
    idle_seconds INTEGER NOT NULL,
    ends REAL NOT NULL,
    revoked REAL
);
CREATE INDEX IF NOT EXISTS unlocks_by_session ON unlocks(session_hash);
CREATE TABLE IF NOT EXISTS client_tokens (
    name TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS setup_codes (
    code_hash TEXT PRIMARY KEY,
    created REAL NOT NULL,
    used REAL
);
CREATE TABLE IF NOT EXISTS recovery_codes (
    code_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    type TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS services (
    service TEXT PRIMARY KEY,
    definition TEXT NOT NULL,
    connection TEXT,
    reported_at TEXT NOT NULL
);
"""

# Triggers that keep the history append-only. SQLite has no CREATE TRIGGER IF NOT
# EXISTS for a list, so they are made one by one below.
#
APPEND_ONLY = (
    "decisions",
    "decision_outcomes",
    "grants",
    "grant_revocations",
    "asking_again",
    "events",
)


def _now_iso(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).isoformat(timespec="seconds")


def _utc(text: Any) -> str:
    """
    A time from a client, in the store's own form, so times compare as text.

    Raises:
        MajiError: `invalid_argument` for anything but a date and time with an offset
    """
    try:
        parsed = datetime.fromisoformat(str(text))
    except ValueError:
        raise MajiError("invalid_argument", f"{text!r} isn't a date and time") from None
    if parsed.tzinfo is None:
        raise MajiError("invalid_argument", "a time needs its offset, such as Z")
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def _check_risk(facts: list[dict[str, Any]], content: dict[str, Any]) -> None:
    """
    Check an item's risk against its reasons: a high-risk item gives one or more,
    and no other item gives any. Each reason says whether the broker found it or
    suspects it, and points at the evidence on the item itself, a fact, a line of
    the diff or a part, so what it rests on is something you can see.

    Raises:
        MajiError: `invalid_argument` for an unknown risk or reason, a mismatch, a
            reason only a guess can support claimed as found, or evidence that
            isn't on the item
    """
    risk = content.get("risk", "medium")
    reasons = content.get("risk_reasons") or []
    if not isinstance(risk, str) or risk not in RISKS:
        raise MajiError("invalid_argument", f"risk is one of {sorted(RISKS)}")
    if not isinstance(reasons, list) or len(reasons) > MAX_RISK_REASONS:
        raise MajiError(
            "invalid_argument", f"risk_reasons is a list of up to {MAX_RISK_REASONS}"
        )
    if (risk == "high") != bool(reasons):
        raise MajiError(
            "invalid_argument", "a high-risk item gives its reasons, and only it does"
        )
    diff, parts = content.get("diff"), content.get("parts")
    lines = diff.get("lines") if isinstance(diff, dict) else None
    counts = {
        "fact": len(facts),
        "line": len(lines) if isinstance(lines, list) else 0,
    }
    part_ids: set[str] = (
        {p["id"] for p in parts if isinstance(p, dict) and isinstance(p.get("id"), str)}
        if isinstance(parts, list)
        else set()
    )
    for reason in reasons:
        if (
            not isinstance(reason, dict)
            or set(reason) != {"code", "basis", "text", "evidence"}
            or not isinstance(reason["code"], str)
            or reason["code"] not in RISK_REASONS
            or not isinstance(reason["basis"], str)
            or reason["basis"] not in BASES
            or not isinstance(reason["text"], str)
            or not 0 < len(reason["text"]) <= MAX_REASON_TEXT
        ):
            raise MajiError(
                "invalid_argument",
                f"a risk reason is a code from {sorted(RISK_REASONS)}, whether it was "
                f"{' or '.join(sorted(BASES))}, its text and its evidence",
            )
        if reason["code"] in SUSPECTED_ONLY and reason["basis"] != "suspected":
            raise MajiError(
                "invalid_argument",
                f"the {reason['code']} risk rests on a guess, so it is only suspected",
            )
        evidence = reason["evidence"]
        if not isinstance(evidence, list) or not 0 < len(evidence) <= MAX_EVIDENCE:
            raise MajiError(
                "invalid_argument",
                f"a risk reason points at 1 to {MAX_EVIDENCE} pieces of evidence",
            )
        for pointer in evidence:
            if not _on_item(pointer, counts, part_ids):
                raise MajiError(
                    "invalid_argument",
                    "a risk reason's evidence is a fact, a diff line or a part of "
                    f"the item, and {pointer!r} isn't",
                )


def _on_item(pointer: Any, counts: dict[str, int], part_ids: set[str]) -> bool:
    """
    Whether evidence points at something on the item: `{"fact": i}` or `{"line": i}`
    by index into its facts or diff lines, or `{"part": id}`.
    """
    if not isinstance(pointer, dict) or len(pointer) != 1:
        return False
    kind, at = next(iter(pointer.items()))
    if kind == "part":
        return isinstance(at, str) and at in part_ids
    # `bool` is a kind of `int` in Python, so `True` would pass as index 1.
    #
    return kind in counts and type(at) is int and 0 <= at < counts[kind]


def secret_hash(secret: str) -> str:
    """The SHA-256 a secret is stored as, written `sha256:<hex>`."""
    return "sha256:" + hashlib.sha256(secret.encode()).hexdigest()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class Conflict(MajiError):
    """An item changed, or was decided, since the decision on it was made."""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        super().__init__(
            "conflict",
            "an item changed or was decided since you saw it",
            {"items": items},
        )


# What each kind of item lets you do while it waits (`docs/design/control-plane.md`,
# "What a queue item is"). `needs` lists what a decision must carry for that action.
#
ACTIONS: dict[str, list[dict[str, Any]]] = {
    "changeset": [
        {"id": "approve", "label": "Approve", "style": "primary"},
        {
            "id": "return",
            "label": "Return with note",
            "style": "neutral",
            "needs": ["note"],
        },
        {"id": "reject", "label": "Reject", "style": "danger"},
    ],
    "run_candidate": [
        {"id": "allow_once", "label": "Allow once", "style": "primary"},
        {"id": "allow_here", "label": "Allow on this machine", "style": "neutral"},
        {"id": "deny", "label": "Deny", "style": "danger"},
    ],
    "promote": [
        {"id": "promote", "label": "Promote and sign", "style": "primary"},
        {
            "id": "request_changes",
            "label": "Request changes",
            "style": "neutral",
            "needs": ["note"],
        },
        {"id": "reject", "label": "Reject", "style": "danger"},
    ],
    "grant": [
        {"id": "approve", "label": "Grant", "style": "primary"},
        {"id": "deny", "label": "Deny", "style": "danger"},
    ],
    "connect": [
        {
            "id": "connect",
            "label": "Connect",
            "style": "primary",
            "needs": ["credential"],
        },
        {"id": "deny", "label": "Deny", "style": "danger"},
    ],
    "pack_import": [
        {"id": "accept", "label": "Accept and sign", "style": "primary"},
        {"id": "reject", "label": "Reject", "style": "danger"},
    ],
    "pack_export": [
        {"id": "approve", "label": "Approve export", "style": "primary"},
        {"id": "reject", "label": "Reject", "style": "danger"},
    ],
    "policy_change": [
        {"id": "accept", "label": "Accept", "style": "primary"},
        {"id": "reject", "label": "Reject", "style": "danger"},
    ],
    "question": [
        {"id": "answer", "label": "Answer", "style": "primary", "needs": ["answer"]},
        {"id": "decline", "label": "Decline to answer", "style": "neutral"},
    ],
    "server_registration": [
        {"id": "approve", "label": "Approve", "style": "primary"},
        {"id": "reject", "label": "Reject", "style": "danger"},
    ],
}

# The actions an approval can be remembered with. A refusal never makes a grant.
#
REMEMBERING = frozenset({"approve", "allow_once", "allow_here"})

# What a decided item lets you do to reconsider its latest decision, by the item's kind
# and the action decided (`docs/design/control-plane.md`, "History is append-only").
# Each takes something back, so its choice carries a note at most, which is what lets a
# tab your passkey unlocked sign it whatever the item's risk.
#
RECONSIDERING: dict[tuple[str, str], list[dict[str, Any]]] = {
    ("promote", "promote"): [{"id": "demote", "label": "Demote", "style": "danger"}],
    ("connect", "connect"): [
        {"id": "disconnect", "label": "Disconnect", "style": "danger"}
    ],
}
RECONSIDER_FIELDS = frozenset({"item", "content_hash", "action", "supersedes", "note"})
UNDONE = frozenset({"conflict", "failed"})

# The longest value a connection's setting may have, such as an organization slug.
#
MAX_SETTING = 200

# What a service definition the broker reports must hold (`Service` in the contract).
#
SERVICE_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
SERVICE_FIELDS = frozenset(
    {
        "service",
        "label",
        "method",
        "host",
        "sent_as",
        "permissions",
        "key_page",
        "settings",
    }
)
CONNECTION_FIELDS = frozenset(
    {"status", "fingerprint", "settings", "connected_at", "decision", "item"}
)

# The reasons an item is high risk (`docs/design/control-plane.md`, "What makes an item
# high risk"). Each is something the broker can see that an agent's own permission
# prompt can't, and a high-risk item always names its own.
#
RISKS = frozenset({"low", "medium", "high"})
RISK_REASONS = frozenset(
    {
        "data_leaves",
        "trust_changes",
        "your_words",
        "addressed_to_ai",
        "beyond_the_task",
        "secrets_in_reach",
    }
)
MAX_RISK_REASONS = len(RISK_REASONS)
MAX_REASON_TEXT = 300
MAX_EVIDENCE = 10

# A reason is `found` when the broker establishes it from what the request does, and
# `suspected` when it rests on a detector or a judgment, which can be wrong. Text
# addressed to an AI is a detector's guess, reaching beyond the task is a judgment of
# the session's goal, and secrets in reach is a guess from file names, so none of
# them is ever found.
#
BASES = frozenset({"found", "suspected"})
SUSPECTED_ONLY = frozenset({"addressed_to_ai", "beyond_the_task", "secrets_in_reach"})

# The item fields that hold content, in the order the API returns them. Everything
# here goes into the content hash; the bookkeeping is added around it on the way out.
#
CONTENT_FIELDS = (
    "id",
    "kind",
    "title",
    "subject",
    "skill",
    "connection",
    "requester",
    "thread",
    "risk",
    "risk_reasons",
    "coalesced",
    "facts",
    "diff",
    "claim",
    "words",
    "parts",
    "resources",
    "patterns",
    "choices",
    "preconditions",
    "depends_on",
    "created_at",
)


@dataclass
class Page:
    rows: list[dict[str, Any]]
    next_cursor: str | None


def _cursor(offset: int) -> str:
    return b64url(f"o:{offset}".encode())


def _offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        text = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        kind, _, value = text.partition(":")
        if kind != "o" or not value.isdigit():
            raise ValueError(text)
        return int(value)
    except ValueError:
        raise MajiError("invalid_argument", "unknown cursor") from None


def _checked_service(
    reported: Any,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """
    Check one service the broker reports, and split off its connection.

    Raises:
        MajiError: `invalid_argument` for a definition or connection out of shape
    """
    if not isinstance(reported, dict):
        raise MajiError("invalid_argument", "each service is an object")
    connection = reported.get("connection")
    definition = {k: v for k, v in reported.items() if k != "connection"}
    if set(definition) - SERVICE_FIELDS or not SERVICE_NAME.fullmatch(
        str(definition.get("service", ""))
    ):
        raise MajiError("invalid_argument", "a service is its definition's fields")
    strings = ("label", "method", "host", "sent_as")
    if not all(isinstance(definition.get(k), str) and definition[k] for k in strings):
        raise MajiError("invalid_argument", f"a service needs {list(strings)}")
    if definition["method"] != "api_key":
        raise MajiError("invalid_argument", "a service's method is api_key")
    permissions = definition.get("permissions")
    if not isinstance(permissions, list) or not all(
        isinstance(p, str) for p in permissions
    ):
        raise MajiError("invalid_argument", "permissions is a list of strings")
    settings = definition.get("settings")
    if not isinstance(settings, list):
        raise MajiError("invalid_argument", "settings is a list")
    for setting in settings:
        if not isinstance(setting, dict) or not all(
            isinstance(setting.get(k), str) for k in ("name", "label", "pattern")
        ):
            raise MajiError("invalid_argument", "a setting has a name, label, pattern")
        try:
            re.compile(setting["pattern"])
        except re.error:
            raise MajiError(
                "invalid_argument", "a setting's pattern compiles"
            ) from None
    if connection is not None and (
        not isinstance(connection, dict)
        or set(connection) - CONNECTION_FIELDS
        or connection.get("status") not in ("connected", "expired", "revoked")
    ):
        raise MajiError("invalid_argument", "a connection is its contract fields")
    return definition, connection


def _check_credential(item: dict[str, Any], choice: dict[str, Any]) -> None:
    """
    Check what a Connect choice says about the key pasted with it: its fingerprint,
    and a value for each of the connection's settings, matching its pattern.

    Raises:
        MajiError: `invalid_argument`, naming the setting at fault but never its value
    """
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(choice["credential_fingerprint"])):
        raise MajiError(
            "invalid_argument", "credential_fingerprint is sha256: and 64 hex digits"
        )
    wanted = {s["name"]: s for s in (item.get("connection") or {}).get("settings", [])}
    given = choice.get("credential_settings") or {}
    if not isinstance(given, dict) or set(given) != set(wanted):
        raise MajiError(
            "invalid_argument",
            f"credential_settings are {sorted(wanted)}, each given once",
        )
    for name, value in given.items():
        if (
            not isinstance(value, str)
            or len(value) > MAX_SETTING
            or not re.fullmatch(wanted[name]["pattern"], value)
        ):
            raise MajiError(
                "invalid_argument",
                f"{wanted[name]['label']} doesn't look like one",
            )


###############################################################################
#
class ControlStore:
    """
    The control plane's database.

    Args:
        path: the database file, or `:memory:`
        instance: this instance's id; a database made for another is refused
        clock: seconds since the epoch; replaced in tests
    """

    def __init__(
        self,
        path: Path | str,
        instance: str,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.clock = clock or time.time
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        for table in APPEND_ONLY:
            for verb in ("UPDATE", "DELETE"):
                self.db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_{verb.lower()} "
                    f"BEFORE {verb} ON {table} BEGIN "
                    f"SELECT RAISE(ABORT, '{table} is append-only'); END"
                )
        row = self.db.execute("SELECT value FROM meta WHERE key='instance'").fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO meta (key, value) VALUES ('instance', ?)", (instance,)
            )
        elif row["value"] != instance:
            raise MajiError(
                "invalid_argument",
                f"this database belongs to instance {row['value']!r}, not {instance!r}",
            )
        self.instance = instance

        # Keys pasted into Connect items, on their way to the broker: in memory only,
        # never in this database (`handoff.py`).
        #
        self.keys = KeyHandoff(self.clock)

    def now(self) -> str:
        return _now_iso(self.clock())

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def _event(self, kind: str, **data: Any) -> None:
        self.db.execute(
            "INSERT INTO events (at, type, data) VALUES (?, ?, ?)",
            (self.now(), kind, json.dumps(data)),
        )

    ###########################################################################
    # Items

    def create_item(
        self,
        kind: str,
        title: str,
        requester: dict[str, Any],
        facts: list[dict[str, Any]],
        *,
        priority: int = 0,
        blocking: bool = False,
        expires_at: str | None = None,
        **content: Any,
    ) -> dict[str, Any]:
        """
        Put an item in the queue.

        Args:
            kind: one of the contract's item kinds
            title: a line saying what is asked
            requester: the principal, session and machine that asked
            facts: what the broker computed
            priority: the order to work in, higher first
            blocking: an agent is waiting on this item to go on
            expires_at: when an undecided item lapses
            content: the item's other content fields, such as `diff`, `claim` or
                `parts`; ones left as None are left out

        Returns:
            The item, as the API returns it
        """
        if kind not in ACTIONS:
            raise MajiError("invalid_argument", f"unknown item kind {kind!r}")
        unknown = set(content) - set(CONTENT_FIELDS)
        if unknown:
            raise MajiError(
                "invalid_argument", f"unknown item fields: {sorted(unknown)}"
            )
        _check_risk(facts, content)
        part_ids = {p["id"] for p in content.get("parts") or []}
        for pattern in content.get("patterns") or []:
            if set(pattern.get("parts", [])) - part_ids:
                raise MajiError(
                    "invalid_argument",
                    f"pattern {pattern.get('id')!r} names parts the item doesn't have",
                )
        body: dict[str, Any] = {
            "id": ids.item_id(self.instance),
            "kind": kind,
            "title": title,
            "requester": requester,
            "facts": facts,
            "created_at": self.now(),
            **{k: v for k, v in content.items() if v is not None},
        }
        body.setdefault("risk", "medium")
        ordered = {k: body[k] for k in CONTENT_FIELDS if k in body}
        thread = ordered.get("thread")
        self.db.execute(
            "INSERT INTO items (id, kind, content, content_hash, actions, priority, "
            "blocking, thread, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ordered["id"],
                kind,
                json.dumps(ordered),
                canonical.content_hash(ordered),
                json.dumps(ACTIONS[kind]),
                priority,
                int(blocking),
                thread["id"] if isinstance(thread, dict) else None,
                ordered["created_at"],
                _utc(expires_at) if expires_at is not None else None,
            ),
        )
        self._event("item.created", item=ordered["id"])
        found = self.get_item(ordered["id"])
        assert found is not None
        return found

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return self._item(row) if row else None

    def _item(self, row: sqlite3.Row) -> dict[str, Any]:
        item: dict[str, Any] = json.loads(row["content"])
        item["state"] = row["state"]
        item["priority"] = row["priority"]
        item["blocking"] = bool(row["blocking"])
        item["content_hash"] = row["content_hash"]
        session = item["requester"].get("session")
        item["session_name"] = session_name(session) if session else None
        if row["expires_at"]:
            item["expires_at"] = row["expires_at"]
        item["notes"] = [
            {"author": n["author"], "text": n["text"], "at": n["at"]}
            for n in self.db.execute(
                "SELECT author, text, at FROM notes WHERE item_id = ? ORDER BY rowid",
                (row["id"],),
            )
        ]
        decision = self.db.execute(
            "SELECT id FROM decisions WHERE item_id = ? ORDER BY rowid DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        item["decision"] = self.get_decision(decision["id"]) if decision else None
        superseded = (item["decision"] or {}).get("supersedes")
        item["superseded"] = self.get_decision(superseded) if superseded else None
        if row["state"] == "waiting":
            item["actions"] = json.loads(row["actions"])
        else:
            item["actions"] = self._reconsidering(item)
        asking = self._asking_again(row["id"])
        if asking is not None:
            item["ask_again"] = asking
        return item

    def _reconsidering(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        """
        The actions that reconsider a decided item's latest decision now: none once
        applying it failed or conflicted, and for a promotion, none once another
        promotion of the same skill has replaced it.
        """
        decision = item.get("decision")
        if not decision:
            return []
        offered = RECONSIDERING.get((item["kind"], decision["action"]))
        if not offered or (decision.get("applied") or {}).get("outcome") in UNDONE:
            return []
        if item["kind"] == "promote" and not self._in_effect(item, decision["id"]):
            return []
        if item["kind"] == "connect" and not self._connected_by(item, decision["id"]):
            return []
        return [dict(action) for action in offered]

    def _connected_by(self, item: dict[str, Any], decision_id: str) -> bool:
        """
        Whether a Connect decision made the connection the broker holds now, as the
        broker last reported it: a later key, or a disconnection, means it didn't.
        """
        service = (item.get("connection") or {}).get("service")
        row = self.db.execute(
            "SELECT connection FROM services WHERE service = ?", (service,)
        ).fetchone()
        held = json.loads(row["connection"]) if row and row["connection"] else {}
        return bool(
            held.get("status") == "connected" and held.get("decision") == decision_id
        )

    def _in_effect(self, item: dict[str, Any], decision_id: str) -> bool:
        """
        Whether a promotion is the one in effect for its skill: no promotion of the
        same skill was decided after it, other than one that failed or conflicted.
        A later promotion counts even if it has since been demoted, since demoting it
        doesn't bring this one back.
        """
        skill = item.get("skill")
        if not isinstance(skill, dict) or not skill.get("id"):
            return False
        later = self.db.execute(
            "SELECT d.id FROM decisions d JOIN items i ON i.id = d.item_id "
            "WHERE i.kind = 'promote' AND i.id != ? "
            "AND json_extract(i.content, '$.skill.id') = ? "
            "AND json_extract(d.body, '$.action') = 'promote' "
            "AND d.rowid > (SELECT rowid FROM decisions WHERE id = ?)",
            (item["id"], skill["id"], decision_id),
        ).fetchall()
        for row in later:
            found = self.get_decision(row["id"]) or {}
            if (found.get("applied") or {}).get("outcome") not in UNDONE:
                return False
        return True

    def _asking_again(self, item_id: str) -> dict[str, str] | None:
        row = self.db.execute(
            "SELECT at, by FROM asking_again WHERE item_id = ?", (item_id,)
        ).fetchone()
        return {"at": row["at"], "by": row["by"]} if row else None

    def list_items(
        self,
        lane: str,
        *,
        thread: str | None = None,
        kind: str | None = None,
        blocking: bool | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page:
        """
        List one lane of the queue.

        Ahead is what's waiting: items blocking an agent first, then by thread, then
        priority, then age. Behind is what's been closed, newest first.
        """
        where: list[str] = []
        args: list[Any] = []
        if lane == "ahead":
            where.append("state = 'waiting'")
            order = "blocking DESC, thread, priority DESC, created_at, id"
        elif lane == "behind":
            where.append("state != 'waiting'")
            order = "closed_at DESC, id DESC"
        else:
            raise MajiError("invalid_argument", "lane is ahead or behind")
        if thread is not None:
            where.append("thread = ?")
            args.append(thread)
        if kind is not None:
            where.append("kind = ?")
            args.append(kind)
        if blocking is not None:
            where.append("blocking = ?")
            args.append(int(blocking))
        offset = _offset(cursor)
        rows = self.db.execute(
            f"SELECT * FROM items WHERE {' AND '.join(where)} ORDER BY {order} "
            "LIMIT ? OFFSET ?",
            (*args, limit + 1, offset),
        ).fetchall()
        more = len(rows) > limit
        return Page(
            [self._item(r) for r in rows[:limit]],
            _cursor(offset + limit) if more else None,
        )

    def expire_due(self) -> list[str]:
        """Close every waiting item past its expiry, returning their ids."""
        now = self.now()
        due = [
            r["id"]
            for r in self.db.execute(
                "SELECT id FROM items WHERE state = 'waiting' "
                "AND expires_at IS NOT NULL AND expires_at <= ?",
                (now,),
            )
        ]
        for item_id in due:
            closed = self.db.execute(
                "UPDATE items SET state = 'expired', closed_at = ? "
                "WHERE id = ? AND state = 'waiting'",
                (now, item_id),
            )
            if closed.rowcount == 1:
                self._event("item.expired", item=item_id)
        return due

    def _own_item(self, item_id: str, principal: str) -> sqlite3.Row:
        """The item, if this principal asked for it; anyone else is told it's absent."""
        row = self.db.execute(
            "SELECT * FROM items WHERE id = ? "
            "AND json_extract(content, '$.requester.principal') = ?",
            (item_id, principal),
        ).fetchone()
        if row is None:
            raise MajiError("not_found", f"no item {item_id} of yours")
        found: sqlite3.Row = row
        return found

    def withdraw(self, item_id: str, principal: str) -> str:
        """
        Take back an item its requester no longer needs, while it waits.

        Returns:
            The item's state afterwards: `withdrawn`, or whatever it had already
            become
        """
        row = self._own_item(item_id, principal)
        done = self.db.execute(
            "UPDATE items SET state = 'withdrawn', closed_at = ? "
            "WHERE id = ? AND state = 'waiting'",
            (self.now(), item_id),
        )
        if done.rowcount == 1:
            self._event("item.withdrawn", item=item_id)
            return "withdrawn"
        return str(row["state"])

    def item_status(self, item_id: str, principal: str) -> dict[str, Any]:
        """
        What became of a request: its state, the decision on it, and what applying
        that decision did. Only the requester can ask.
        """
        self._own_item(item_id, principal)
        return self.status_of(item_id)

    def status_of(self, item_id: str) -> dict[str, Any]:
        """
        What became of any item: its state, the decision on it, and what applying
        that decision did. For the broker, which raised it; a requester asks through
        `item_status`.

        Raises:
            MajiError: `not_found` for an unknown item
        """
        row = self.db.execute(
            "SELECT state FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise MajiError("not_found", f"no item {item_id}")
        status: dict[str, Any] = {"item": item_id, "state": row["state"]}
        found = self.db.execute(
            "SELECT id FROM decisions WHERE item_id = ? ORDER BY rowid DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        decision = self.get_decision(found["id"]) if found else None
        if decision is not None:
            status["decision"] = {
                k: decision[k]
                for k in ("action", "parts", "answer", "note", "at", "granted")
                if decision.get(k) is not None
            }
            status["outcome"] = decision["applied"]
        asking = self._asking_again(item_id)
        if asking is not None:
            status["ask_again"] = asking["at"]
        return status

    def let_ask_again(self, item_id: str, by: str) -> dict[str, Any]:
        """
        Let the requester ask again for the mounts you refused in an item.

        After you refuse a path, the broker refuses the agent's reruns that ask for
        it in the same session, so an agent that retries can't keep putting it to
        you. Once you've told the agent more, you may want to see its next request:
        this lifts that refusal, and the next run asking for the path raises a new
        item. It grants nothing, and the refusal stays in the item's history.

        Returns:
            The item, with when you let it ask again

        Raises:
            MajiError: `not_found` for an unknown item, `invalid_argument` for one
                that refused no mount
        """
        item = self.get_item(item_id)
        if item is None:
            raise MajiError("not_found", f"no item {item_id}")
        if not refused_mounts(item):
            raise MajiError(
                "invalid_argument", f"{item_id} isn't a decision that refused a mount"
            )
        done = self.db.execute(
            "INSERT OR IGNORE INTO asking_again (item_id, at, by) VALUES (?, ?, ?)",
            (item_id, self.now(), by),
        )
        if done.rowcount == 1:
            self._event("item.updated", item=item_id)
        found = self.get_item(item_id)
        assert found is not None
        return found

    ###########################################################################
    # Decisions

    def check_choice(self, choice: dict[str, Any]) -> dict[str, Any]:
        """
        Check that a decision fits its item as it stands now.

        Returns:
            The item

        Raises:
            MajiError: `not_found` for an unknown item, `invalid_argument` for an
                action the item doesn't offer or one missing what it needs
            Conflict: the item isn't waiting, or has changed since it was seen
        """
        item = self.get_item(choice["item"])
        if item is None:
            raise MajiError("not_found", f"no item {choice['item']}")
        if "supersedes" in choice:
            return self._check_reconsidering(item, choice)
        if item["state"] != "waiting" or item["content_hash"] != choice["content_hash"]:
            raise Conflict(
                [
                    {
                        "item": item["id"],
                        "state": item["state"],
                        "content_hash": item["content_hash"],
                    }
                ]
            )
        action = next((a for a in item["actions"] if a["id"] == choice["action"]), None)
        if action is None:
            raise MajiError(
                "invalid_argument",
                f"{item['id']} offers {[a['id'] for a in item['actions']]}, "
                f"not {choice['action']!r}",
            )
        missing = [
            need
            for need in action.get("needs", [])
            if not choice.get(
                "credential_fingerprint" if need == "credential" else need
            )
        ]
        if missing:
            raise MajiError("invalid_argument", f"{choice['action']} needs {missing}")
        if "credential" in action.get("needs", []):
            _check_credential(item, choice)
        elif "credential_fingerprint" in choice or "credential_settings" in choice:
            raise MajiError(
                "invalid_argument", f"{choice['action']} doesn't take a pasted key"
            )
        parts = choice.get("parts")
        if parts is not None:
            known = {p["id"] for p in item.get("parts", [])}
            if set(parts) - known:
                raise MajiError(
                    "invalid_argument", f"unknown parts {sorted(set(parts) - known)}"
                )
        refused = {k for k, v in (parts or {}).items() if v != "approve"}
        remembered = choice.get("remember")
        if remembered is not None:
            if (
                not isinstance(remembered, list)
                or not 1 <= len(remembered) <= 20
                or not all(isinstance(r, dict) for r in remembered)
            ):
                raise MajiError(
                    "invalid_argument", "remember is a list of 1 to 20 patterns"
                )
            if len({r.get("pattern") for r in remembered}) != len(remembered):
                raise MajiError(
                    "invalid_argument", "each pattern is remembered at most once"
                )
            for remember in remembered:
                self._check_remember(item, choice["action"], remember, refused)
        if choice["action"] in REMEMBERING:
            self._check_mounts_remembered(item, remembered or [], refused)
        return item

    @staticmethod
    def _check_reconsidering(
        item: dict[str, Any], choice: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Check a choice that reconsiders a decided item's latest decision.

        It names that decision in `supersedes`, and carries a note at most besides,
        so it can only take something back: never remember a pattern, approve a part
        or answer.

        Raises:
            MajiError: `invalid_argument` for an item still waiting, a choice
                carrying more than a note, or an action no decided item offers
            Conflict: the item's latest decision isn't the one named, or the action
                isn't open now, as when a later promotion replaced this one
        """
        if item["state"] == "waiting":
            raise MajiError(
                "invalid_argument",
                f"{item['id']} is waiting: there is no decision to reconsider",
            )
        extra = set(choice) - RECONSIDER_FIELDS
        if extra:
            raise MajiError(
                "invalid_argument",
                f"reconsidering a decision carries a note at most, not {sorted(extra)}",
            )
        latest = item.get("decision") or {}
        open_now = {a["id"] for a in item["actions"]}
        if (
            item["content_hash"] != choice["content_hash"]
            or latest.get("id") != choice["supersedes"]
            or choice["action"] not in open_now
        ):
            if not any(
                choice["action"] == a["id"]
                for offered in RECONSIDERING.values()
                for a in offered
            ):
                raise MajiError(
                    "invalid_argument",
                    f"{choice['action']!r} doesn't reconsider a decision",
                )
            raise Conflict(
                [
                    {
                        "item": item["id"],
                        "state": item["state"],
                        "content_hash": item["content_hash"],
                    }
                ]
            )
        return item

    @staticmethod
    def _check_mounts_remembered(
        item: dict[str, Any], remembered: list[dict[str, Any]], refused: set[str]
    ) -> None:
        """
        Check an approval remembers every mount it approves.

        A script that mounts a path is usually followed by more scripts over it, and
        `run_python` returns as soon as it raises the item, so a mount approved just
        once, or for a run that has already ended, would cover no run at all. A mount
        is remembered for the session at least (2026-11-09), though a session's
        grant may end sooner at a time (2026-11-10). A mount over a part this
        decision refuses needs nothing.
        """
        kept = {r.get("pattern"): r.get("lasts") for r in remembered}
        for pattern in item.get("patterns", []):
            if pattern["access"] != "mount" or set(pattern.get("parts", [])) & refused:
                continue
            if kept.get(pattern["id"]) in (None, "run"):
                raise MajiError(
                    "invalid_argument",
                    f"{pattern['summary']} is a mount, remembered at least for the "
                    "session: choose how long",
                )

    def _check_remember(
        self,
        item: dict[str, Any],
        action: str,
        remember: dict[str, Any],
        refused: set[str],
    ) -> None:
        """
        Check one pattern an approval asks to remember, which widens access.

        Only an approving action can carry one, with a pattern the item offers that
        doesn't cover writes of content and covers no part this decision refuses, for
        a duration the grant can be held to: a run or a session the requester named,
        or a time still to come. A session's grant can also end at a time, if the
        session hasn't ended first: a session can last days, and a mount kept for the
        working day ends with it.
        """
        if action not in REMEMBERING:
            raise MajiError("invalid_argument", f"{action} can't be remembered")
        offered = {p["id"]: p for p in item.get("patterns", [])}
        pattern = offered.get(remember.get("pattern"))
        if pattern is None:
            raise MajiError(
                "invalid_argument",
                f"{item['id']} offers no pattern {remember.get('pattern')!r}",
            )
        if set(pattern.get("parts", [])) & refused:
            raise MajiError(
                "invalid_argument",
                f"pattern {pattern['id']!r} covers a part this decision refuses",
            )
        if pattern["access"] not in ("read", "mount", "structural"):
            raise MajiError(
                "invalid_argument", "a write of content can't be remembered"
            )
        lasts, until = remember.get("lasts"), remember.get("until")
        if lasts not in ("run", "session", "until", "permanent"):
            raise MajiError(
                "invalid_argument", "lasts is run, session, until or permanent"
            )
        if lasts == "until" and until is None:
            raise MajiError("invalid_argument", "lasts until needs until")
        if until is not None and lasts not in ("until", "session"):
            raise MajiError(
                "invalid_argument", "until is given only when lasts is until or session"
            )
        if until is not None and _utc(until) <= self.now():
            raise MajiError("invalid_argument", "until has to be in the future")
        if lasts in ("run", "session") and not item["requester"].get(lasts):
            raise MajiError(
                "invalid_argument",
                f"the requester named no {lasts} to hold the grant to",
            )

    def record_decisions(
        self,
        choices: list[dict[str, Any]],
        by: dict[str, Any],
        signature: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Record a batch of decisions, all or none.

        Each item moves from waiting to decided only if its content hash is still the
        one decided on. An approval makes a grant for the item's requester for each
        pattern it remembers. A choice reconsidering a decided item's latest decision,
        such as a demotion, is recorded on top of it, naming it in `supersedes`, and
        the item stays decided.

        Raises:
            Conflict: some item changed or was decided meanwhile; nothing is recorded
        """
        at = self.now()
        recorded = []
        with self._transaction():
            for choice in choices:
                item = self.check_choice(choice)
                if "supersedes" in choice:
                    # A decided item stays decided, and moves to the top of the
                    # history, which is newest first by when it was last closed.
                    #
                    self.db.execute(
                        "UPDATE items SET closed_at = ? WHERE id = ?",
                        (at, choice["item"]),
                    )
                else:
                    moved = self.db.execute(
                        "UPDATE items SET state = 'decided', closed_at = ? "
                        "WHERE id = ? AND state = 'waiting' AND content_hash = ?",
                        (at, choice["item"], choice["content_hash"]),
                    )
                    if moved.rowcount != 1:
                        raise Conflict([{"item": choice["item"]}])
                decision = {k: v for k, v in choice.items() if v is not None}
                decision.update(
                    id=f"{self.instance}-{ids.ulid()}",
                    decision_hash=canonical.decision_hash(choice),
                    by={**by, "instance": self.instance},
                    at=at,
                    signature=signature,
                    supersedes=choice.get("supersedes"),
                )
                # Each grant refers to the decision, so the decision goes in first,
                # already naming the grants it makes.
                #
                remembered = choice.get("remember") or []
                decision["granted"] = [
                    f"{self.instance}-{ids.ulid()}" for _ in remembered
                ]
                self.db.execute(
                    "INSERT INTO decisions (id, item_id, body, at) VALUES (?, ?, ?, ?)",
                    (decision["id"], choice["item"], json.dumps(decision), at),
                )
                offered = {p["id"]: p for p in item.get("patterns", [])}
                for remember, grant_id in zip(
                    remembered, decision["granted"], strict=True
                ):
                    self._add_grant(
                        item["requester"],
                        offered[remember["pattern"]],
                        remember,
                        decision["id"],
                        at,
                        grant_id,
                    )
                self._event(
                    "item.decided", item=choice["item"], decision=decision["id"]
                )
                recorded.append(decision)
        return [self.get_decision(d["id"]) or d for d in recorded]

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT body FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        if row is None:
            return None
        decision: dict[str, Any] = json.loads(row["body"])
        outcome = self.db.execute(
            "SELECT outcome, detail, at FROM decision_outcomes WHERE decision_id = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (decision_id,),
        ).fetchone()
        decision["applied"] = (
            {
                "outcome": outcome["outcome"],
                "at": outcome["at"],
                "detail": outcome["detail"] or "",
            }
            if outcome
            else None
        )
        return decision

    def list_decisions(
        self,
        *,
        item_id: str | None = None,
        by: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page:
        where: list[str] = ["1"]
        args: list[Any] = []
        if item_id is not None:
            where.append("item_id = ?")
            args.append(item_id)
        if by is not None:
            where.append("json_extract(body, '$.by.kind') = ?")
            args.append(by)
        offset = _offset(cursor)
        rows = self.db.execute(
            f"SELECT id FROM decisions WHERE {' AND '.join(where)} "
            "ORDER BY rowid DESC LIMIT ? OFFSET ?",
            (*args, limit + 1, offset),
        ).fetchall()
        more = len(rows) > limit
        found = [self.get_decision(r["id"]) for r in rows[:limit]]
        return Page([d for d in found if d], _cursor(offset + limit) if more else None)

    def decisions_since(self, position: int, limit: int = 100) -> dict[str, Any]:
        """
        The decisions recorded after a position, each with its item, for the broker
        to apply in order. The broker keeps the last position it has applied, so
        it misses none across a restart.
        """
        rows = self.db.execute(
            "SELECT rowid, id, item_id FROM decisions WHERE rowid > ? "
            "ORDER BY rowid LIMIT ?",
            (position, limit),
        ).fetchall()
        found = []
        for row in rows:
            found.append(
                {
                    "position": row["rowid"],
                    "decision": self.get_decision(row["id"]),
                    "item": self.get_item(row["item_id"]),
                }
            )
        return {"decisions": found, "position": rows[-1]["rowid"] if rows else position}

    def record_outcome(self, decision_id: str, outcome: str, detail: str = "") -> None:
        """Record what applying a decision did; the latest record is the one shown."""
        if outcome not in ("applied", "conflict", "failed"):
            raise MajiError(
                "invalid_argument", "outcome is applied, conflict or failed"
            )
        if self.get_decision(decision_id) is None:
            raise MajiError("not_found", f"no decision {decision_id}")
        self.db.execute(
            "INSERT INTO decision_outcomes (decision_id, outcome, detail, at) "
            "VALUES (?, ?, ?, ?)",
            (decision_id, outcome, detail[:2000], self.now()),
        )
        self._event("decision.applied", decision=decision_id)

    ###########################################################################
    # Grants

    def _add_grant(
        self,
        requester: dict[str, Any],
        pattern: dict[str, Any],
        remember: dict[str, Any],
        decision_id: str | None,
        at: str,
        grant_id: str | None = None,
    ) -> str:
        grant_id = grant_id or f"{self.instance}-{ids.ulid()}"
        self.db.execute(
            "INSERT INTO grants (id, principal, pattern, access, resource, lasts, until, "
            "run, session, granted_at, decision_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                grant_id,
                requester["principal"],
                json.dumps(pattern),
                pattern["access"],
                pattern.get("resource"),
                remember["lasts"],
                _utc(remember["until"]) if remember.get("until") else None,
                requester.get("run"),
                requester.get("session"),
                at,
                decision_id,
            ),
        )
        self._event("grant.changed", grant=grant_id)
        return grant_id

    def add_grant(
        self,
        principal: str,
        pattern: dict[str, Any],
        lasts: str,
        *,
        until: str | None = None,
        decision_id: str | None = None,
        session: str | None = None,
    ) -> dict[str, Any]:
        """Record a grant directly, as setup and the tests do."""
        grant_id = self._add_grant(
            {"principal": principal, "session": session},
            pattern,
            {"lasts": lasts, "until": until},
            decision_id,
            self.now(),
        )
        found = self.get_grant(grant_id)
        assert found is not None
        return found

    def get_grant(self, grant_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT g.*, r.at AS revoked_at FROM grants g "
            "LEFT JOIN grant_revocations r ON r.grant_id = g.id WHERE g.id = ?",
            (grant_id,),
        ).fetchone()
        return self._grant(row) if row else None

    def _grant(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "principal": row["principal"],
            "pattern": json.loads(row["pattern"]),
            "lasts": row["lasts"],
            "until": row["until"],
            "run": row["run"],
            "session": row["session"],
            "session_name": session_name(row["session"]) if row["session"] else None,
            "granted_at": row["granted_at"],
            "decision": row["decision_id"],
            "revoked_at": row["revoked_at"],
        }

    def _grant_rows(
        self,
        *,
        principal: str | None = None,
        active: bool = True,
        resource: str | None = None,
        access: str | None = None,
        lasts: str | None = None,
        session: str | None = None,
    ) -> list[sqlite3.Row]:
        where: list[str] = ["1"]
        args: list[Any] = []
        if active:
            where.append("r.at IS NULL AND (g.until IS NULL OR g.until > ?)")
            args.append(self.now())
        for column, value in (
            ("principal", principal),
            ("resource", resource),
            ("access", access),
            ("lasts", lasts),
        ):
            if value is not None:
                where.append(f"g.{column} = ?")
                args.append(value)
        if session is not None:
            # Every grant records the session it was made in, but only one that lasts
            # for the session is held by it; one kept until revoked outlives it.
            #
            where.append("g.lasts = 'session' AND g.session = ?")
            args.append(session)
        return self.db.execute(
            "SELECT g.*, r.at AS revoked_at FROM grants g "
            "LEFT JOIN grant_revocations r ON r.grant_id = g.id "
            f"WHERE {' AND '.join(where)} ORDER BY g.granted_at DESC, g.id DESC",
            args,
        ).fetchall()

    def list_grants(
        self,
        *,
        q: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
        **filters: Any,
    ) -> Page:
        """
        List grants, newest first, or by how well they match a fuzzy search.

        A search matches against each grant's pattern summary, its resource and its
        principal, and keeps the best of the three.
        """
        grants = [self._grant(r) for r in self._grant_rows(**filters)]
        if q and q.strip():
            scored = []
            for grant in grants:
                best = _best_match(q, grant)
                if best is not None:
                    grant["match"] = best
                    scored.append(grant)
            grants = sorted(scored, key=lambda g: -g["match"]["score"])
        offset = _offset(cursor)
        more = len(grants) > offset + limit
        return Page(
            grants[offset : offset + limit], _cursor(offset + limit) if more else None
        )

    def group_grants(
        self, by: str, *, active: bool = True, q: str | None = None
    ) -> list[dict[str, Any]]:
        """
        Count grants along one dimension, largest group first.

        Grouped by session, each group also names its session for people, so the
        grants of a session that has ended can be found and revoked together. Only a
        grant that lasts for the session is held by it: the rest, which last for a
        run, until a time or until revoked, group under `(none)`, as grants without a
        resource do, though each records the session it was made in.
        """
        column = {
            "resource": "resource",
            "principal": "principal",
            "access": "access",
            "lasts": "lasts",
            "session": "session",
        }.get(by)
        if column is None:
            raise MajiError(
                "invalid_argument",
                "group by resource, principal, access, lasts or session",
            )
        groups: dict[str, dict[str, Any]] = {}
        for row in self._grant_rows(active=active):
            grant = self._grant(row)
            if q and q.strip() and _best_match(q, grant) is None:
                continue
            held = by != "session" or row["lasts"] == "session"
            key = (row[column] if held else None) or "(none)"
            group = groups.setdefault(
                key,
                {
                    "key": key,
                    "count": 0,
                    "permanent": 0,
                    "last_used_at": None,
                    "next_expiry": None,
                    **(
                        {"session_name": session_name(key) if key != "(none)" else None}
                        if by == "session"
                        else {}
                    ),
                },
            )
            group["count"] += 1
            if row["lasts"] == "permanent":
                group["permanent"] += 1
            if row["until"] and (
                group["next_expiry"] is None or row["until"] < group["next_expiry"]
            ):
                group["next_expiry"] = row["until"]
        return sorted(groups.values(), key=lambda g: (-g["count"], g["key"]))

    def revoke_grants(self, grant_ids: list[str], by: str) -> int:
        """Revoke grants that are still active; returns how many were."""
        revoked = 0
        at = self.now()
        with self._transaction():
            for grant_id in grant_ids:
                done = self.db.execute(
                    "INSERT OR IGNORE INTO grant_revocations (grant_id, at, by) "
                    "SELECT id, ?, ? FROM grants WHERE id = ?",
                    (at, by, grant_id),
                )
                if done.rowcount == 1:
                    revoked += 1
                    self._event("grant.changed", grant=grant_id)
        return revoked

    def matching_grant_ids(self, **filters: Any) -> list[str]:
        return [r["id"] for r in self._grant_rows(active=True, **filters)]

    def grant_covers(
        self,
        principal: str,
        access: str,
        resource: str,
        *,
        session: str | None = None,
        run: str | None = None,
        exact: bool = False,
    ) -> str | None:
        """
        Find an active grant that lets this principal do this to this resource.

        A grant for a run covers only that run, and one for a session only that
        session. The control plane matches the principal, the kind of access and the
        resource; the finer `match` in a pattern is the broker's to check.

        A mount's resource is a path, and a grant to mount a folder covers everything
        in it (2026-11-10): the path's own grant is looked for first, then each
        folder holding it, nearest first. Each is compared whole, so a grant on
        `/src/api` covers `/src/api/rules` and not `/src/api-old`.

        With `exact`, only a grant on the path itself counts. The broker asks that
        way for a file named like a secret: a mount of a folder leaves such a file
        out, so a grant on the folder was never a grant to read it.

        Returns:
            The grant's id, or None
        """
        resources = [resource]
        if access == "mount" and not exact:
            resources += [str(p) for p in PurePosixPath(resource).parents]
        for candidate in resources:
            for row in self._grant_rows(
                principal=principal, access=access, resource=candidate
            ):
                if row["lasts"] == "run" and (run is None or row["run"] != run):
                    continue
                if row["lasts"] == "session" and (
                    session is None or row["session"] != session
                ):
                    continue
                return str(row["id"])
        return None

    ###########################################################################
    # Connections

    def report_services(self, services: list[dict[str, Any]]) -> None:
        """
        Record the services the broker can call with a key, and the connection it
        holds for each, as the broker reports them, in place of what it reported
        before. A change to a service's connection is an event, so open clients
        show it.

        Args:
            services: each service's definition (`Service` in the contract), with
                `connection`: what the broker holds for it, or None

        Raises:
            MajiError: `invalid_argument` for a definition or a connection that
                isn't in the contract's shape
        """
        checked = [_checked_service(service) for service in services]
        before = {
            row["service"]: row["connection"]
            for row in self.db.execute("SELECT service, connection FROM services")
        }
        with self._transaction():
            self.db.execute("DELETE FROM services")
            for definition, connection in checked:
                held = json.dumps(connection) if connection is not None else None
                self.db.execute(
                    "INSERT INTO services (service, definition, connection, "
                    "reported_at) VALUES (?, ?, ?, ?)",
                    (definition["service"], json.dumps(definition), held, self.now()),
                )
                if before.get(definition["service"]) != held:
                    self._event("connection.changed", service=definition["service"])

    def get_service(self, service: str) -> dict[str, Any] | None:
        """A service's definition, as the broker last reported it, or None."""
        row = self.db.execute(
            "SELECT definition FROM services WHERE service = ?", (service,)
        ).fetchone()
        return json.loads(row["definition"]) if row else None

    def list_connections(self) -> list[dict[str, Any]]:
        """
        Each service the broker can call with a key, and the connection it holds.

        Returns:
            One `Connection` (in the contract) per service, by name: `not_connected`
            when the broker holds no key, and `item`, the Connect item waiting for
            the service if there is one, else the one that connected it
        """
        found = []
        for row in self.db.execute("SELECT * FROM services ORDER BY service"):
            definition = json.loads(row["definition"])
            held = json.loads(row["connection"]) if row["connection"] else {}
            waiting = self._waiting_connect(definition["service"])
            connection = {
                "service": definition["service"],
                "label": definition["label"],
                "method": definition["method"],
                "host": definition["host"],
                "status": held.get("status", "not_connected"),
                **{
                    k: held[k]
                    for k in ("fingerprint", "settings", "connected_at", "decision")
                    if held.get(k) is not None
                },
            }
            item = waiting["id"] if waiting else held.get("item")
            if item:
                connection["item"] = item
            found.append(connection)
        return found

    def request_connection(
        self, service: str, requester: dict[str, Any], claim: str | None = None
    ) -> tuple[dict[str, Any], bool]:
        """
        Put a Connect item for a service in the queue, built from the broker's
        definition of the service, or return the one already waiting.

        Everything the item says comes from that definition: the host the key goes
        to, how it is sent, the permissions it needs, and the settings asked for
        with it. Whoever asks supplies only the claim, which clients show as theirs.

        Args:
            service: the service's name
            requester: who asked: an agent's principal and session, or you
            claim: why, in the requester's words

        Returns:
            The item, and whether it is new

        Raises:
            MajiError: `not_found` for a service the broker hasn't reported
        """
        definition = self.get_service(service)
        if definition is None:
            raise MajiError("not_found", f"the broker has no service {service!r}")
        waiting = self._waiting_connect(service)
        if waiting is not None:
            return waiting, False
        facts = [
            {"label": "Sent to", "value": definition["host"]},
            {"label": "Sent as", "value": definition["sent_as"]},
            {"label": "The key needs", "value": ", ".join(definition["permissions"])},
        ]
        row = self.db.execute(
            "SELECT connection FROM services WHERE service = ?", (service,)
        ).fetchone()
        held = json.loads(row["connection"]) if row["connection"] else {}
        if held.get("status") == "connected":
            facts.append(
                {
                    "label": "Replaces",
                    "value": f"the key connected {held.get('connected_at')}, "
                    f"{str(held.get('fingerprint', ''))[:15]}",
                }
            )
        item = self.create_item(
            "connect",
            f"Connect {definition['label']}",
            requester,
            facts,
            subject=f"{service} stubs",
            connection=definition,
            claim=claim,
        )
        return item, True

    def _waiting_connect(self, service: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT id FROM items WHERE kind = 'connect' AND state = 'waiting' "
            "AND json_extract(content, '$.connection.service') = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (service,),
        ).fetchone()
        return self.get_item(row["id"]) if row else None

    ###########################################################################
    # Policy

    def policy_rules(self) -> list[dict[str, Any]]:
        return [
            json.loads(r["body"])
            for r in self.db.execute("SELECT body FROM policy_rules ORDER BY id")
        ]

    ###########################################################################
    # Client tokens, sessions, setup and recovery codes

    def add_client_token(self, name: str, token: str) -> None:
        self.db.execute(
            "INSERT INTO client_tokens (name, token_hash, created_at) VALUES (?, ?, ?)",
            (name, secret_hash(token), self.now()),
        )

    def client_for_token(self, token: str) -> str | None:
        row = self.db.execute(
            "SELECT name FROM client_tokens WHERE token_hash = ?", (secret_hash(token),)
        ).fetchone()
        return row["name"] if row else None

    def set_client_tokens(self, hashes: dict[str, str]) -> None:
        """
        Make the clients exactly these: each name with its token's hash, as
        `sha256:<hex>`. The tokens file is the source of truth.
        """
        at = self.now()
        with self._transaction():
            self.db.execute("DELETE FROM client_tokens")
            self.db.executemany(
                "INSERT INTO client_tokens (name, token_hash, created_at) VALUES (?, ?, ?)",
                [(name, digest, at) for name, digest in hashes.items()],
            )

    def start_session(self, session_id: str, passkey_id: str) -> None:
        now = self.clock()
        self.db.execute(
            "INSERT INTO sessions (id_hash, passkey_id, created, expires) VALUES (?, ?, ?, ?)",
            (secret_hash(session_id), passkey_id, now, now + SESSION_SECONDS),
        )

    def session(self, session_id: str) -> dict[str, Any] | None:
        """The live session with this id: its passkey, when it was signed in and ends."""
        row = self.db.execute(
            "SELECT passkey_id, created, expires FROM sessions "
            "WHERE id_hash = ? AND ended IS NULL AND expires > ?",
            (secret_hash(session_id), self.clock()),
        ).fetchone()
        return (
            {
                "passkey": row["passkey_id"],
                "created": row["created"],
                "expires": row["expires"],
            }
            if row
            else None
        )

    def end_session(self, session_id: str) -> None:
        """End a session, and lock every tab it had unlocked."""
        now = self.clock()
        with self._transaction():
            self.db.execute(
                "UPDATE sessions SET ended = ? WHERE id_hash = ? AND ended IS NULL",
                (now, secret_hash(session_id)),
            )
            self.db.execute(
                "UPDATE unlocks SET revoked = ? WHERE session_hash = ? AND revoked IS NULL",
                (now, secret_hash(session_id)),
            )

    ###########################################################################
    # Unlocked deciding

    def add_unlock(
        self,
        session_id: str,
        passkey_id: str,
        certificate: dict[str, Any],
        idle_seconds: int,
    ) -> dict[str, Any]:
        """
        Unlock deciding for one tab of a session, with the key its passkey certified.

        The unlock ends with the session, or once it has been idle for `idle_seconds`.
        Beyond `UNLOCKS_PER_SESSION` tabs, the oldest is locked.

        Args:
            session_id: the session the tab is signed in with
            passkey_id: the passkey that certified the key
            certificate: the key, as JWK, and the assertion that certified it, kept so
                a decision signed with the key can be verified again anywhere
            idle_seconds: how long it may go without use before it locks

        Returns:
            The unlock's id, idle limit and end
        """
        session = self.session(session_id)
        if session is None:
            raise MajiError("unauthenticated", "the session has ended")
        now = self.clock()
        unlock_id = secrets.token_urlsafe(16)
        with self._transaction():
            self.db.execute(
                "INSERT INTO unlocks (id, session_hash, passkey_id, certificate, "
                "created, last_active, idle_seconds, ends) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    unlock_id,
                    secret_hash(session_id),
                    passkey_id,
                    json.dumps(certificate),
                    now,
                    now,
                    idle_seconds,
                    session["expires"],
                ),
            )
            self.db.execute(
                "UPDATE unlocks SET revoked = ? WHERE session_hash = ? "
                "AND revoked IS NULL AND id NOT IN (SELECT id FROM unlocks "
                "WHERE session_hash = ? AND revoked IS NULL "
                "ORDER BY created DESC, rowid DESC LIMIT ?)",
                (
                    now,
                    secret_hash(session_id),
                    secret_hash(session_id),
                    UNLOCKS_PER_SESSION,
                ),
            )
        return {
            "id": unlock_id,
            "idle_seconds": idle_seconds,
            "ends_at": _now_iso(session["expires"]),
        }

    def live_unlock(self, unlock_id: str, session_id: str) -> dict[str, Any] | None:
        """
        A tab's unlock, while it still holds: this session's, not locked, used within
        its idle limit, and before its session ends.

        Returns:
            Its passkey and certificate, or None when it is locked or not this session's
        """
        now = self.clock()
        row = self.db.execute(
            "SELECT u.passkey_id, u.certificate, u.idle_seconds FROM unlocks u "
            "JOIN sessions s ON s.id_hash = u.session_hash "
            "WHERE u.id = ? AND u.session_hash = ? AND u.revoked IS NULL "
            "AND u.last_active + u.idle_seconds > ? AND u.ends > ? "
            "AND s.ended IS NULL AND s.expires > ?",
            (unlock_id, secret_hash(session_id), now, now, now),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": unlock_id,
            "passkey": row["passkey_id"],
            "certificate": json.loads(row["certificate"]),
            "idle_seconds": row["idle_seconds"],
        }

    def unlock_used(self, unlock_id: str, session_id: str) -> bool:
        """Count the tab as active now, if its unlock still holds; whether it does."""
        if self.live_unlock(unlock_id, session_id) is None:
            return False
        self.db.execute(
            "UPDATE unlocks SET last_active = ? WHERE id = ?",
            (self.clock(), unlock_id),
        )
        return True

    def lock(self, unlock_id: str, session_id: str) -> None:
        """Lock a tab of this session; locking one already locked does nothing."""
        self.db.execute(
            "UPDATE unlocks SET revoked = ? WHERE id = ? AND session_hash = ? "
            "AND revoked IS NULL",
            (self.clock(), unlock_id, secret_hash(session_id)),
        )

    def add_setup_code(self, code: str) -> None:
        """Record a setup code, which works only while no passkey exists."""
        self.db.execute(
            "INSERT INTO setup_codes (code_hash, created) VALUES (?, ?)",
            (secret_hash(code), self.clock()),
        )

    def use_setup_code(self, code: str) -> bool:
        """Spend a setup code: true once, while it is fresh and no passkey exists."""
        if self.passkey_count():
            return False
        used = self.db.execute(
            "UPDATE setup_codes SET used = ? WHERE code_hash = ? AND used IS NULL "
            "AND created > ?",
            (self.clock(), secret_hash(code), self.clock() - SETUP_CODE_SECONDS),
        )
        return used.rowcount == 1

    def add_recovery_codes(self, codes: list[str]) -> None:
        at = self.now()
        self.db.executemany(
            "INSERT INTO recovery_codes (code_hash, created_at) VALUES (?, ?)",
            [(secret_hash(c), at) for c in codes],
        )

    def use_recovery_code(self, code: str) -> bool:
        used = self.db.execute(
            "UPDATE recovery_codes SET used_at = ? WHERE code_hash = ? AND used_at IS NULL",
            (self.now(), secret_hash(code)),
        )
        return used.rowcount == 1

    ###########################################################################
    # Challenges

    def add_challenge(
        self, challenge_id: str, purpose: str, challenge: bytes, payload: dict[str, Any]
    ) -> None:
        self.db.execute(
            "INSERT INTO challenges (id, purpose, challenge, payload, created) VALUES (?, ?, ?, ?, ?)",
            (challenge_id, purpose, challenge, json.dumps(payload), self.clock()),
        )

    def use_challenge(self, challenge_id: str, purpose: str) -> dict[str, Any] | None:
        """
        Spend a challenge: once, for its purpose, within five minutes of being made.

        Returns:
            Its bytes and payload, or None when it is unknown, spent or stale
        """
        now = self.clock()
        used = self.db.execute(
            "UPDATE challenges SET used = ? WHERE id = ? AND purpose = ? AND used IS NULL "
            "AND created > ?",
            (now, challenge_id, purpose, now - CHALLENGE_SECONDS),
        )
        if used.rowcount != 1:
            return None
        row = self.db.execute(
            "SELECT challenge, payload FROM challenges WHERE id = ?", (challenge_id,)
        ).fetchone()
        return {
            "challenge": bytes(row["challenge"]),
            "payload": json.loads(row["payload"]),
        }

    ###########################################################################
    # Passkeys

    def passkey_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM passkeys").fetchone()[0])

    def add_passkey(
        self, credential_id: str, name: str, public_key: bytes, sign_count: int
    ) -> dict[str, Any]:
        self.db.execute(
            "INSERT INTO passkeys (id, name, public_key, sign_count, created_at) VALUES (?, ?, ?, ?, ?)",
            (credential_id, name, public_key, sign_count, self.now()),
        )
        self._event("passkey.added", passkey=credential_id)
        found = self.get_passkey(credential_id)
        assert found is not None
        return found

    def get_passkey(self, credential_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM passkeys WHERE id = ?", (credential_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "public_key": bytes(row["public_key"]),
            "sign_count": row["sign_count"],
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
        }

    def list_passkeys(self) -> list[dict[str, Any]]:
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "created_at": r["created_at"],
                "last_used_at": r["last_used_at"],
            }
            for r in self.db.execute("SELECT * FROM passkeys ORDER BY created_at, id")
        ]

    def passkey_used(self, credential_id: str, sign_count: int) -> None:
        self.db.execute(
            "UPDATE passkeys SET sign_count = ?, last_used_at = ? WHERE id = ?",
            (sign_count, self.now(), credential_id),
        )

    def remove_passkey(self, credential_id: str) -> bool:
        """Remove a passkey, unless it is the last one."""
        with self._transaction():
            if self.passkey_count() <= 1:
                raise MajiError("conflict", "the last passkey can't be removed")
            gone = self.db.execute(
                "DELETE FROM passkeys WHERE id = ?", (credential_id,)
            )
        return gone.rowcount == 1

    ###########################################################################
    # Events

    def last_event(self) -> int:
        """The position of the newest event, or 0."""
        row = self.db.execute("SELECT MAX(seq) FROM events").fetchone()
        return int(row[0] or 0)

    def event_time(self, seq: int) -> str | None:
        """When the event at a position was recorded, or None if there is none."""
        row = self.db.execute("SELECT at FROM events WHERE seq = ?", (seq,)).fetchone()
        return str(row["at"]) if row else None

    def events_after(self, seq: int, limit: int = 500) -> list[dict[str, Any]]:
        return [
            {"seq": r["seq"], "at": r["at"], "type": r["type"], **json.loads(r["data"])}
            for r in self.db.execute(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (seq, limit)
            )
        ]


def _best_match(q: str, grant: dict[str, Any]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for field, text in (
        ("summary", grant["pattern"].get("summary", "")),
        ("resource", grant["pattern"].get("resource") or ""),
        ("principal", grant["principal"]),
    ):
        found = fuzzy.match(q, text)
        if found is not None and (best is None or found.score > best["score"]):
            best = {
                "score": found.score,
                "field": field,
                "ranges": [list(r) for r in found.ranges],
            }
    return best


def refused_mounts(item: dict[str, Any]) -> list[str]:
    """
    The paths an item asked to mount that its decision refused.

    A decision refuses a mount when it refuses the whole item, or approves it but
    leaves out the part the mount is over.

    Returns:
        Each refused mount's path, in the item's order; none for an item still
        waiting, or one that asked to mount nothing
    """
    decision = item.get("decision")
    if item.get("state") != "decided" or not decision:
        return []
    whole = decision.get("action") not in REMEMBERING
    parts = decision.get("parts") or {}
    return [
        str(pattern.get("resource"))
        for pattern in item.get("patterns", [])
        if pattern.get("access") == "mount"
        and (
            whole
            or any(
                parts.get(p, "approve") != "approve" for p in pattern.get("parts", [])
            )
        )
    ]
