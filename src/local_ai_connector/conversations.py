"""Native conversation plans carried by the existing approved message channel.

The ingress remains the authenticated connector endpoint. Native workers return
through that ingress, so creating a chat never rebinds or shares its credentials.
"""
import json
import time
from pathlib import Path
from uuid import UUID

from .core import require


CREATE = "mcp__codex_app__create_thread"
SEND = "mcp__codex_app__send_message_to_thread"
BINDING_MESSAGE = ("Save the selected existing Codex chat as this endpoint's collaboration target. "
                   "This approval saves routing metadata only; it sends no task and grants no future task permissions.")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def codex_plan(channel, message, registration, thread_id):
    if thread_id is not None:
        return SEND, {"threadId": thread_id, "prompt": message["body"]}
    args = {"prompt": "This is the new conversation the user requested. The request to open a new chat is already fulfilled. Answer the user's task in this chat:\n\n" + message["body"],
            "target": {"type": "projectless", "directoryName": "connector-" + channel}}
    for key in ("model", "thinking"):
        if key in registration:
            args[key] = registration[key]
    return CREATE, args


# Transport, consent, deduplication and continuation are shared; only native
# invocation construction belongs to a registered host implementation.
PROVIDERS = {"codex_ingress": codex_plan}
DIRECT_PROVIDER = "antigravity_sidecar"


def reserve_target(db, channel, selected):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_existing_routes'").fetchone():
        # Installed hooks can load the update before the running broker migrates its schema.
        return
    # Ownership stays separate; target reservation never grants archive rights.
    rows = db.execute('''SELECT n.channel,n.thread_id,r.selection FROM native_conversations n
        JOIN channels c ON c.id=n.channel LEFT JOIN native_existing_routes r ON r.channel=n.channel
        WHERE n.channel!=? AND (c.status IN ('pending','active') OR EXISTS (
            SELECT 1 FROM native_operations o WHERE o.channel=n.channel AND o.state IN ('intent','ambiguous')))''',
        (channel,)).fetchall()
    require(not any((json.loads(row['selection'])['thread_id'] if row['selection'] else row['thread_id'])
                    == selected['thread_id'] for row in rows),
            'codex_chat_reserved', 'Another pending, active or uncertain task already reserves this chat.')


class Conversations:
    def __init__(self, db, registrations=None):
        self.db = db
        self.registrations = registrations or {}
        for peer, entry in self.registrations.items():
            if isinstance(entry, dict) and entry.get("provider") == DIRECT_PROVIDER:
                require(set(entry) == {"provider", "project_id", "workspace_uri", "socket"}
                        and isinstance(entry["project_id"], str) and str(UUID(entry["project_id"])) == entry["project_id"]
                        and isinstance(entry["workspace_uri"], str) and entry["workspace_uri"].startswith("file:///")
                        and isinstance(entry["socket"], str) and Path(entry["socket"]).is_absolute(),
                        "invalid_conversation_config", "Antigravity needs a project, workspace URI and private socket")
                continue
            require(isinstance(entry, dict) and set(entry) <= {"provider", "session_id", "cwd", "model", "thinking"}
                    and entry.get("provider") in PROVIDERS, "invalid_conversation_config", "Unknown conversation provider")
            require(isinstance(entry.get("session_id"), str) and str(UUID(entry["session_id"])) == entry["session_id"]
                    and isinstance(entry.get("cwd"), str) and Path(entry["cwd"]).is_absolute(),
                    "invalid_conversation_config", "A native ingress session and absolute cwd are required")
            require(all(isinstance(entry[k], str) and entry[k] for k in ("model", "thinking") if k in entry),
                    "invalid_conversation_config", "Invalid native model settings")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS native_conversations (
                channel TEXT PRIMARY KEY REFERENCES channels(id),
                registration TEXT NOT NULL, thread_id TEXT UNIQUE, host_id TEXT);
            CREATE TABLE IF NOT EXISTS native_operations (
                question_id TEXT PRIMARY KEY REFERENCES messages(id),
                channel TEXT NOT NULL REFERENCES channels(id),
                tool_name TEXT NOT NULL, arguments TEXT NOT NULL,
                state TEXT NOT NULL, turn_id TEXT, consumed_at REAL, receipt TEXT);
            CREATE TABLE IF NOT EXISTS native_archives (
                request_channel TEXT PRIMARY KEY REFERENCES channels(id),
                source_channel TEXT NOT NULL REFERENCES channels(id),
                registration TEXT NOT NULL, target TEXT NOT NULL,
                state TEXT NOT NULL, receipt TEXT);
            CREATE TABLE IF NOT EXISTS native_retired (
                channel TEXT PRIMARY KEY REFERENCES channels(id), thread_id TEXT NOT NULL,
                request_channel TEXT NOT NULL REFERENCES channels(id), archived_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS native_deliveries (
                message_id TEXT PRIMARY KEY REFERENCES messages(id));
            CREATE TABLE IF NOT EXISTS native_existing_routes (
                channel TEXT PRIMARY KEY REFERENCES native_conversations(channel),
                selection TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS native_binding_requests (
                channel TEXT PRIMARY KEY REFERENCES channels(id), selection TEXT NOT NULL,
                registration TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS native_bindings (
                requester TEXT NOT NULL REFERENCES peers(id), responder TEXT NOT NULL REFERENCES peers(id),
                approved_channel TEXT NOT NULL REFERENCES native_binding_requests(channel),
                selection TEXT NOT NULL, registration TEXT NOT NULL,
                PRIMARY KEY(requester,responder));
        """)
        if "lifecycle" not in {row[1] for row in db.execute("PRAGMA table_info(native_conversations)")}:
            db.execute("ALTER TABLE native_conversations ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'active'")
            db.commit()

    def existing_route(self, channel):
        row = self.db.execute('SELECT selection FROM native_existing_routes WHERE channel=?', (channel,)).fetchone()
        return json.loads(row[0]) if row else None

    def reserve(self, channel, selected):
        reserve_target(self.db, channel, selected)

    def binding_request(self, channel):
        row = self.db.execute('SELECT * FROM native_binding_requests WHERE channel=?', (channel,)).fetchone()
        return {**dict(row), 'selection': json.loads(row['selection']),
                'registration': json.loads(row['registration'])} if row else None

    def saved_binding(self, requester, responder):
        row = self.db.execute('SELECT * FROM native_bindings WHERE requester=? AND responder=?',
                              (requester, responder)).fetchone()
        return {**dict(row), 'selection': json.loads(row['selection']),
                'registration': json.loads(row['registration'])} if row else None

    def remember_binding(self, channel):
        request = self.binding_request(channel['id'])
        require(request['registration'] == self.registrations.get(channel['responder']),
                'stale_conversation_config', 'The registered Codex ingress changed before saving the target.')
        self.db.execute('''INSERT INTO native_bindings VALUES (?,?,?,?,?)
            ON CONFLICT(requester,responder) DO UPDATE SET approved_channel=excluded.approved_channel,
            selection=excluded.selection,registration=excluded.registration''',
            (channel['requester'], channel['responder'], channel['id'],
             canonical(request['selection']), canonical(request['registration'])))

    def open(self, channel, peer, mode, selected=None, *, binding_only=False):
        require(mode in ("existing", "new"), "invalid_conversation_mode", "Choose existing or new")
        if selected is not None:
            registration = self.registrations.get(peer, {})
            require(mode == 'existing' and registration.get('provider') == 'codex_ingress',
                    'unsupported_codex_binding', 'Selected-chat routing requires the configured Codex ingress.')
            if binding_only:
                self.db.execute('INSERT INTO native_binding_requests VALUES (?,?,?)',
                                (channel, canonical(selected), canonical(registration)))
                return
            self.reserve(channel, selected)
            self.db.execute('INSERT INTO native_conversations(channel,registration) VALUES (?,?)',
                            (channel, canonical(registration)))
            self.db.execute('INSERT INTO native_existing_routes VALUES (?,?)', (channel, canonical(selected)))
        if mode == "new":
            require(peer in self.registrations, "new_conversation_unsupported",
                    "This endpoint cannot create a native conversation; no existing chat was substituted")
            self.db.execute("INSERT INTO native_conversations(channel,registration) VALUES (?,?)",
                            (channel, canonical(self.registrations[peer])))

    def describe(self, channel):
        binding = self.binding_request(channel)
        if binding is not None:
            selected = binding['selection']
            return {'mode': 'existing', 'created': False, 'thread_id': selected['thread_id'],
                    'host_id': 'local', 'selected': selected, 'binding_only': True}
        selected = self.existing_route(channel)
        if selected is not None:
            return {'mode': 'existing', 'created': False, 'thread_id': selected['thread_id'],
                    'host_id': 'local', 'selected': selected}
        retired = self.db.execute("SELECT thread_id FROM native_retired WHERE channel=?", (channel,)).fetchone()
        if retired:
            return {"mode": "new", "created": True, "thread_id": retired[0], "host_id": "local", "state": "archived"}
        row = self.db.execute("SELECT thread_id,host_id,registration,lifecycle FROM native_conversations WHERE channel=?", (channel,)).fetchone()
        if row is None:
            return {"mode": "existing", "created": False}
        result = {"mode": "new", "created": row["thread_id"] is not None,
                "thread_id": row["thread_id"], "host_id": row["host_id"]}
        if json.loads(row["registration"])["provider"] == DIRECT_PROVIDER:
            result.update(state=row["lifecycle"], cleanup_actions=["archive"])
        return result

    def direct(self, channel):
        row = self.db.execute("SELECT registration FROM native_conversations WHERE channel=?", (channel,)).fetchone()
        return row is not None and json.loads(row[0])["provider"] == DIRECT_PROVIDER

    def archive(self, channel):
        row = self.db.execute("SELECT * FROM native_archives WHERE request_channel=?", (channel,)).fetchone()
        return dict(row) if row else None

    def externally_dispatched(self, channel):
        return self.direct(channel) or self.archive(channel) is not None

    def require_active(self, channel):
        require(self.binding_request(channel) is None, 'binding_channel',
                'A saved-target approval grants no task messages or continuation; approve each new task separately.')
        require(self.archive(channel) is None, "archive_channel", "An archive request cannot carry task messages or continuations")
        require(self.db.execute("SELECT 1 FROM native_retired WHERE channel=?", (channel,)).fetchone() is None,
                "conversation_archived", "The native chat was archived and its active registration was removed")
        row = self.db.execute("SELECT lifecycle FROM native_conversations WHERE channel=?", (channel,)).fetchone()
        require(row is None or row[0] == "active", "conversation_archiving", "The chat is reserved for an approved archive operation")

    def observed(self, rows):
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO native_deliveries VALUES (?)",
                                [(row["id"],) for row in rows if self.direct(row["channel"])])

    def delivery(self, message):
        native = self.db.execute("SELECT * FROM native_conversations WHERE channel=?", (message["channel"],)).fetchone()
        if native is None:
            return message
        message = {**message, "conversation": self.describe(message["channel"])}
        if json.loads(native["registration"])["provider"] == DIRECT_PROVIDER:
            return message
        if message["resolved"]:
            return message
        channel = self.db.execute("SELECT * FROM channels WHERE id=?", (message["channel"],)).fetchone()
        if message["recipient"] != channel["responder"]:
            return message
        registration = json.loads(native["registration"])
        require(self.registrations.get(channel["responder"]) == registration,
                "stale_conversation_config", "Native conversation registration changed; task was not rerouted")
        operation = self.db.execute("SELECT * FROM native_operations WHERE question_id=?", (message["id"],)).fetchone()
        if operation is None:
            selected = self.existing_route(channel['id'])
            require(selected is not None or native["thread_id"] is not None or message["client_key"] == "initial:" + channel["id"],
                    "conversation_not_created", "The original native conversation has not been confirmed")
            if selected is not None:
                tool, arguments = SEND, {'threadId': selected['thread_id'], 'hostId': 'local', 'prompt': message['body']}
            else:
                tool, arguments = PROVIDERS[registration["provider"]](channel["id"], message, registration, native["thread_id"])
            self.db.execute("INSERT INTO native_operations VALUES (?,?,?,?,?,NULL,NULL,NULL)",
                            (message["id"], channel["id"], tool, canonical(arguments), "prepared"))
            operation = self.db.execute("SELECT * FROM native_operations WHERE question_id=?", (message["id"],)).fetchone()
        execution = {"state": operation["state"], "conversation": self.describe(channel["id"]),
                     "authorization": "The initiating user approved this channel's native conversation mode and original task. The host hook independently checks the exact action, ingress identity, expiry and single-use grant.",
                     "instruction": "Execute the exact native tool arguments once. Wait for the actual worker answer using wait_threads/read_thread, then relay it with connector_send replying to the original requester question. If this message is a clarification answer, continue that original question; an answer message cannot itself be reply_to. Do not answer the task in the ingress chat. A prepared operation authorizes that exact native action. An intent or ambiguous operation must not be repeated; report the uncertain outcome. An acknowledged operation can be waited on, never resent."}
        if operation["state"] == "prepared":
            execution.update(tool=operation["tool_name"], arguments=json.loads(operation["arguments"]))
        return {**message, "native_execution": execution}

    def require_executed(self, channel, question):
        if self.direct(channel):
            require(self.describe(channel)["created"], "native_execution_unconfirmed", "Native creation is not confirmed")
            return
        if self.describe(channel)["mode"] == "new" or self.existing_route(channel) is not None:
            row = self.db.execute("SELECT state FROM native_operations WHERE question_id=? AND channel=?",
                                  (question, channel)).fetchone()
            require(row is not None and row["state"] == "acknowledged", "native_execution_unconfirmed",
                    "The native tool receipt is missing; an ingress answer cannot stand in for native execution")
            require(self.db.execute("SELECT 1 FROM native_operations WHERE channel=? AND state!='acknowledged' LIMIT 1",
                                    (channel,)).fetchone() is None, "native_execution_unconfirmed",
                    "A native follow-up or clarification has not been confirmed")

    def unconfirmed(self, question):
        row = self.db.execute("SELECT state FROM native_operations WHERE question_id=?", (question,)).fetchone()
        return row is not None and row["state"] in ("ambiguous", "failed")


def native_receipt(tool, response):
    """Parse the installed native tool's MCP output, not a model-reported ID."""
    require(isinstance(response, dict) and response.get("isError", False) is False,
            "invalid_native_receipt", "Native tool did not confirm success")
    content = response.get("content")
    require(isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
            and content[0].get("type") == "text" and isinstance(content[0].get("text"), str),
            "invalid_native_receipt", "Unexpected native tool result")
    result = json.loads(content[0]["text"])
    require(isinstance(result, dict), "invalid_native_receipt", "Native result is not an object")
    if tool == CREATE:
        tid = result.get("threadId")
        require(isinstance(tid, str) and str(UUID(tid)) == tid and result.get("hostId") == "local",
                "invalid_native_receipt", "Creation did not confirm a local thread ID")
    return result


def _hook_operations(db, peer, tool, encoded, session, cwd, name, turn_id, now):
    routed = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_existing_routes'").fetchone()
    selection = 'r.selection' if routed else 'NULL AS selection'
    route_join = 'LEFT JOIN native_existing_routes r ON r.channel=c.id' if routed else ''
    rows = db.execute(f"""SELECT o.*, c.status, c.expires, c.approved_at, n.registration, n.thread_id,
        {selection} FROM native_operations o JOIN channels c ON c.id=o.channel
        JOIN native_conversations n ON n.channel=c.id
        {route_join}
        WHERE c.responder=? AND o.tool_name=? AND o.arguments=?""", (peer, tool, encoded)).fetchall()
    eligible = [r for r in rows if json.loads(r['registration'])['session_id'] == session
                and json.loads(r['registration'])['cwd'] == cwd]
    if name == 'PermissionRequest':
        eligible = [r for r in eligible if r['state'] == 'prepared' and r['status'] == 'active'
                    and r['approved_at'] is not None and now < r['expires']]
    else:
        eligible = [r for r in eligible if r['turn_id'] == turn_id]
        in_flight = [r for r in eligible if r['state'] == 'intent']
        if in_flight:
            eligible = in_flight
    require(len(eligible) == 1, 'native_grant_missing', 'No unique approved native operation matches')
    return eligible[0]


def handle_hook(db, event, peer, session, cwd, now=None, *, selected_verifier=None):
    """Consume exact broker plans and record native results in SQLite transactions.

    Only the owner-installed native hook calls this entry point. It is not exposed
    as an MCP operation and never accepts model-provided approval or thread IDs.
    """
    supplied_now = now
    name = event.get("hook_event_name")
    require(name in ("PermissionRequest", "PostToolUse"), "invalid_hook", "Unsupported hook event")
    require(event.get("session_id") == session and event.get("cwd") == cwd
            and isinstance(event.get("turn_id"), str) and event["turn_id"],
            "invalid_hook", "Hook ingress identity mismatch")
    tool, args = event.get("tool_name"), event.get("tool_input")
    require(tool in (CREATE, SEND) and isinstance(args, dict), "invalid_hook", "Unsupported native operation")
    encoded = canonical(args)
    checked_selection = None
    checked_operation = None
    if name == 'PermissionRequest':
        row = _hook_operations(db, peer, tool, encoded, session, cwd, name, event['turn_id'],
                               time.time() if supplied_now is None else supplied_now)
        checked_selection = row['selection']
        checked_operation = (row['question_id'], row['channel'])
        if checked_selection is not None:
            require(selected_verifier is not None, 'codex_binding_unavailable',
                    'Selected-chat execution requires current native metadata verification.')
            # Native metadata I/O must not hold the broker's shared SQLite write lock.
            selected_verifier(json.loads(checked_selection), session)
    db.execute("BEGIN IMMEDIATE")
    try:
        now = time.time() if supplied_now is None else supplied_now
        row = _hook_operations(db, peer, tool, encoded, session, cwd, name, event['turn_id'], now)
        if name == "PermissionRequest":
            require((row['question_id'], row['channel']) == checked_operation, 'native_grant_mismatch',
                    'The operation changed during metadata verification; authority was not transferred.')
            require(row['selection'] == checked_selection, 'native_grant_mismatch',
                    'Selected route changed during metadata verification.')
            target = json.loads(row['selection'])['thread_id'] if row['selection'] else row['thread_id']
            if target and json.loads(row['registration'])['provider'] == 'codex_ingress':
                reserve_target(db, row['channel'], {'thread_id': target})
            require(row["status"] == "active" and row["approved_at"] is not None and now < row["expires"]
                    and row["state"] == "prepared", "native_grant_unavailable", "Native grant expired, revoked or already consumed")
            db.execute("UPDATE native_operations SET state='intent',turn_id=?,consumed_at=? WHERE question_id=?",
                       (event["turn_id"], now, row["question_id"]))
        else:
            require(row["state"] in ("intent", "acknowledged", "ambiguous") and row["turn_id"] == event["turn_id"],
                    "native_receipt_mismatch", "Receipt does not match the consumed operation")
            receipt = canonical(event.get("tool_response"))
            if row["receipt"] is not None:
                require(row["receipt"] == receipt, "native_receipt_conflict", "Native result changed")
            else:
                from .core import ConnectorError
                try:
                    result = native_receipt(tool, event.get("tool_response"))
                    selected = row['selection']
                    if selected is not None:
                        require(tool == SEND and result.get('threadId') == args.get('threadId'),
                                'invalid_native_receipt', 'SEND receipt did not confirm the approved selected chat.')
                    if tool == CREATE:
                        require(result["threadId"] != session, "invalid_native_receipt", "Creation returned the ingress itself")
                        db.execute("UPDATE native_conversations SET thread_id=?,host_id=? WHERE channel=? AND thread_id IS NULL",
                                   (result["threadId"], result["hostId"], row["channel"]))
                except (ConnectorError, ValueError, KeyError, TypeError):
                    db.execute("UPDATE native_operations SET state='ambiguous',receipt=? WHERE question_id=?",
                               (receipt, row["question_id"]))
                else:
                    db.execute("UPDATE native_operations SET state='acknowledged',receipt=? WHERE question_id=?",
                               (receipt, row["question_id"]))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return {"hookSpecificOutput": {"hookEventName": name, "decision": {"behavior": "allow"}}} if name == "PermissionRequest" else {}
