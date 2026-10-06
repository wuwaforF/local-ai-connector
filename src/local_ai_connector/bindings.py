"""Exact-chat bindings for worker endpoints, and the target pin taken when a task is authorized.

A host endpoint is shared by every chat of that host. Its binding names one chat by the
session identity the host supplies (never by model input). Each change creates a new
revision. Opening a task records the revision shown for approval; approving it pins that
chat and revision, or fails if the binding changed in between. Re-binding therefore affects
only later authorizations. A binding grants no task authority by itself.
"""
from __future__ import annotations

import re
from uuid import uuid4

from .core import require

BIND_REQUEST_TTL = 300
CREDENTIAL_TTL = 12 * 3600
_SESSION = re.compile(r"^[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9._:-]{1,200}$")

SCHEMA = """
    CREATE TABLE IF NOT EXISTS chat_bindings (
        endpoint TEXT PRIMARY KEY REFERENCES peers(id), session TEXT, label TEXT,
        revision INTEGER NOT NULL, changed_at REAL NOT NULL, source TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS binding_requests (
        id TEXT PRIMARY KEY, endpoint TEXT NOT NULL REFERENCES peers(id), session TEXT NOT NULL,
        label TEXT NOT NULL, base_revision INTEGER NOT NULL, request_key TEXT NOT NULL,
        status TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, decided_at REAL,
        UNIQUE(endpoint, session, request_key));
    CREATE TABLE IF NOT EXISTS task_targets (
        channel TEXT PRIMARY KEY REFERENCES channels(id), endpoint TEXT NOT NULL,
        session TEXT NOT NULL, label TEXT NOT NULL, revision INTEGER NOT NULL,
        offered_at REAL NOT NULL, pinned_at REAL);
"""


def valid_session(session) -> bool:
    return isinstance(session, str) and _SESSION.fullmatch(session) is not None


def session_hint(session: str) -> str:
    namespace, _, identifier = session.partition(":")
    return f"{namespace}:{identifier[:8]}"


class _Secret:
    """Holds a credential without exposing it through repr, str or formatting."""
    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self):
        return "<redacted>"

    __str__ = __repr__


class DeliveryCredentials:
    """Session-scoped secrets (such as a host inbox token), held in memory only.

    A credential is accepted only for the endpoint's currently bound session, and it is tied
    to that binding revision. Re-binding, unbinding, a newer enrollment, expiry or a service
    restart makes it stale; a stale credential is never returned.
    """

    def __init__(self, bindings: "Bindings", clock):
        self._bindings, self._clock = bindings, clock
        self._entries: dict[str, tuple[str, int, float, _Secret]] = {}
        self.generation = 0

    def enroll(self, endpoint: str, session: str, secret: str) -> dict:
        require(isinstance(secret, str) and 0 < len(secret) <= 512, "invalid_credential",
                "A delivery credential must be 1 to 512 characters.")
        current = self._bindings.current(endpoint)
        require(current is not None and current["session"] == session, "not_enrolled_target",
                "Only the explicitly bound chat can enroll a delivery credential.")
        self.generation += 1
        self._entries[endpoint] = (session, current["revision"], self._clock() + CREDENTIAL_TTL, _Secret(secret))
        return {"state": "enrolled", "endpoint": endpoint, "revision": current["revision"],
                "generation": self.generation}

    def get(self, endpoint: str, session: str, revision: int) -> str | None:
        entry = self._entries.get(endpoint)
        if entry is None:
            return None
        enrolled_session, enrolled_revision, expires, secret = entry
        current = self._bindings.current(endpoint)
        if (current is None or (current["session"], current["revision"]) != (enrolled_session, enrolled_revision)
                or (session, revision) != (enrolled_session, enrolled_revision) or self._clock() >= expires):
            del self._entries[endpoint]
            return None
        return secret.reveal()

    def invalidate(self, endpoint: str):
        self._entries.pop(endpoint, None)

    def __repr__(self):
        return f"<DeliveryCredentials endpoints={sorted(self._entries)}>"


class Bindings:
    def __init__(self, db, clock):
        self.db, self.clock = db, clock
        self.bindable: set[str] = set()
        self.db.executescript(SCHEMA)
        self.credentials = DeliveryCredentials(self, clock)

    # --- current binding ------------------------------------------------------------
    def current(self, endpoint: str) -> dict | None:
        row = self.db.execute("SELECT * FROM chat_bindings WHERE endpoint=? AND session IS NOT NULL",
                              (endpoint,)).fetchone()
        return dict(row) if row else None

    def revision(self, endpoint: str) -> int:
        row = self.db.execute("SELECT revision FROM chat_bindings WHERE endpoint=?", (endpoint,)).fetchone()
        return row[0] if row else 0

    def describe(self, endpoint: str, session: str | None = None) -> dict | None:
        if endpoint not in self.bindable:
            return None
        current = self.current(endpoint)
        result = {"bound": current is not None, "revision": self.revision(endpoint)}
        if current is not None:
            result.update(label=current["label"], chat=session_hint(current["session"]))
        if session is not None:
            result["this_chat_bound"] = current is not None and current["session"] == session
        return result

    def _set(self, endpoint, session, label, revision, source):
        self.db.execute("""INSERT INTO chat_bindings VALUES (?,?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET
            session=excluded.session, label=excluded.label, revision=excluded.revision,
            changed_at=excluded.changed_at, source=excluded.source""",
                        (endpoint, session, label, revision, self.clock(), source))
        self.credentials.invalidate(endpoint)

    # --- bind requests (approved in the chat being bound) ---------------------------
    def request(self, endpoint: str, session, label: str, key: str) -> dict:
        require(endpoint in self.bindable, "binding_unsupported",
                "This endpoint has no trusted chat identity, so exact-chat binding is unavailable.")
        require(valid_session(session), "missing_session_identity",
                "The host did not supply a trusted identity for this chat; nothing was bound.")
        require(isinstance(label, str) and 0 < len(label.strip()) <= 80 and label.isprintable(),
                "invalid_label", "A chat label must be 1 to 80 printable characters.")
        require(isinstance(key, str) and 0 < len(key) <= 200, "invalid_key", "A stable request key is required.")
        label = label.strip()
        old = self.db.execute("SELECT * FROM binding_requests WHERE endpoint=? AND session=? AND request_key=?",
                              (endpoint, session, key)).fetchone()
        if old is not None:
            require(old["label"] == label, "idempotency_conflict", "A request key cannot change the chat label.")
            return self._request_view(dict(old))
        now = self.clock()
        request = {"id": str(uuid4()), "endpoint": endpoint, "session": session, "label": label,
                   "base_revision": self.revision(endpoint), "request_key": key, "status": "pending",
                   "created": now, "expires": now + BIND_REQUEST_TTL, "decided_at": None}
        with self.db:
            self.db.execute("INSERT INTO binding_requests VALUES (:id,:endpoint,:session,:label,:base_revision,"
                            ":request_key,:status,:created,:expires,:decided_at)", request)
        return self._request_view(request)

    def _request_view(self, request: dict) -> dict:
        current = self.current(request["endpoint"])
        status = request["status"]
        if status == "pending" and request["expires"] <= self.clock():
            status = "expired"
        return {"request": request["id"], "state": status, "endpoint": request["endpoint"],
                "label": request["label"], "chat": session_hint(request["session"]),
                "replaces": None if current is None else {"label": current["label"], "chat": session_hint(current["session"]),
                                                          "revision": current["revision"]},
                "task_authorized": False}

    def decide(self, endpoint: str, session, request_id: str, decision: str, source: str) -> dict:
        row = self.db.execute("SELECT * FROM binding_requests WHERE id=?", (request_id,)).fetchone()
        require(row is not None and row["endpoint"] == endpoint, "not_found", "Binding request not found.")
        request = dict(row)
        # The decision is accepted only from the chat that asked to be bound.
        require(session == request["session"], "wrong_session", "This chat did not request that binding.")
        require(decision in ("accept", "decline", "cancel"), "invalid_request", "Invalid binding decision.")
        if request["status"] != "pending":
            require(request["status"] == ("approved" if decision == "accept" else "declined"),
                    "invalid_state", "This binding request was already decided.")
            return self._request_view(request)
        require(request["expires"] > self.clock(), "binding_request_expired",
                "The binding request expired; ask again to bind this chat.")
        now = self.clock()
        with self.db:
            if decision != "accept":
                self.db.execute("UPDATE binding_requests SET status='declined', decided_at=? WHERE id=?", (now, request_id))
            elif self.revision(endpoint) != request["base_revision"]:
                self.db.execute("UPDATE binding_requests SET status='conflict', decided_at=? WHERE id=?", (now, request_id))
            else:
                self._set(endpoint, request["session"], request["label"], request["base_revision"] + 1, source)
                self.db.execute("UPDATE binding_requests SET status='approved', decided_at=? WHERE id=?", (now, request_id))
        request = dict(self.db.execute("SELECT * FROM binding_requests WHERE id=?", (request_id,)).fetchone())
        require(request["status"] != "conflict", "binding_changed",
                "The binding changed while this request was waiting; nothing was saved. Ask again.")
        result = self._request_view(request)
        if request["status"] == "approved":
            result["revision"] = request["base_revision"] + 1
        return result

    def unbind(self, endpoint: str, session=None, *, admin=False) -> dict:
        current = self.current(endpoint)
        require(current is not None, "target_not_bound", "No chat is bound to this endpoint.")
        require(admin or session == current["session"], "wrong_session", "Only the bound chat or the owner can unbind it.")
        with self.db:
            self._set(endpoint, None, None, current["revision"] + 1, "owner" if admin else "bound_chat")
        return {"state": "unbound", "endpoint": endpoint, "revision": current["revision"] + 1}

    # --- task target offer and pin --------------------------------------------------
    def offer(self, channel: str, endpoint: str, expected_revision: int | None, expected_label: str | None = None,
              require_target: bool = False):
        """Record the target shown for approval. Call inside the channel-creating transaction.

        require_target is set where the host's approval prompt shows only tool arguments: the
        task must then name the bound chat's label and revision, so the prompt displays them.
        """
        if endpoint not in self.bindable:
            return None
        current = self.current(endpoint)
        require(current is not None, "target_not_bound",
                "No chat is bound to this worker. Bind one from inside the worker chat first.")
        require(not require_target or (expected_revision is not None and expected_label is not None),
                "target_chat_required", "Pass target_chat and binding_revision from connector_status so the "
                "approval prompt shows the bound chat; nothing was sent.")
        require((expected_revision is None or expected_revision == current["revision"])
                and (expected_label is None or expected_label == current["label"]), "target_binding_changed",
                "The bound chat changed. Read connector_status again; nothing was sent.")
        self.db.execute("INSERT INTO task_targets VALUES (?,?,?,?,?,?,NULL)",
                        (channel, endpoint, current["session"], current["label"], current["revision"], self.clock()))
        return current

    def target(self, channel: str) -> dict | None:
        row = self.db.execute("SELECT * FROM task_targets WHERE channel=?", (channel,)).fetchone()
        return dict(row) if row else None

    def target_view(self, channel: str) -> dict | None:
        target = self.target(channel)
        if target is None:
            return None
        return {"label": target["label"], "chat": session_hint(target["session"]),
                "revision": target["revision"], "pinned": target["pinned_at"] is not None}

    def pin_conflict(self, channel: str) -> bool:
        """True when the offered target is no longer the current binding."""
        target = self.target(channel)
        if target is None or target["pinned_at"] is not None:
            return False
        current = self.current(target["endpoint"])
        return current is None or (current["session"], current["revision"]) != (target["session"], target["revision"])

    def pin(self, channel: str):
        """Pin the offered target. Call inside the approving transaction after pin_conflict()."""
        self.db.execute("UPDATE task_targets SET pinned_at=? WHERE channel=? AND pinned_at IS NULL",
                        (self.clock(), channel))

    def require_session(self, channel: dict, peer: str, session):
        """Only the pinned chat may act for the worker side of a pinned task."""
        if peer != channel["responder"]:
            return
        target = self.target(channel["id"])
        if target is None:
            return
        require(session == target["session"], "wrong_session",
                "This task is pinned to another chat; this chat cannot receive or answer it.")
