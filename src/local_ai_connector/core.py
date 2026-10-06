from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)


class ConnectorError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def require(condition, code, message):
    if not condition:
        raise ConnectorError(code, message)


class Broker:
    """One broker process owns the database and event condition."""

    def __init__(self, path: Path, *, clock=time.time, conversations=None, codex_catalog=None):
        self.clock = clock
        self.codex_catalog = codex_catalog
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.changed = asyncio.Condition()
        # Optional delivery observers (e.g. the wake-up adapter). Empty by default.
        self.observers = []
        self.profiles = {}
        self.db.executescript("""
            PRAGMA foreign_keys=ON;
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS peers (
                id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS channels (
                id TEXT PRIMARY KEY, requester TEXT REFERENCES peers(id),
                responder TEXT REFERENCES peers(id), original TEXT NOT NULL,
                status TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                idle_seconds REAL NOT NULL, idle_since REAL,
                open_key TEXT NOT NULL, approved_at REAL, UNIQUE(requester, open_key));
            CREATE TABLE IF NOT EXISTS messages (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                channel TEXT NOT NULL REFERENCES channels(id), sender TEXT NOT NULL,
                recipient TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
                reply_to TEXT REFERENCES messages(id), resolved INTEGER NOT NULL DEFAULT 0,
                sent REAL NOT NULL, client_key TEXT NOT NULL,
                UNIQUE(sender, client_key));
            CREATE TABLE IF NOT EXISTS incidents (
                id TEXT PRIMARY KEY, peer TEXT NOT NULL, code TEXT NOT NULL,
                detail TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS approval_decisions (
                channel TEXT PRIMARY KEY REFERENCES channels(id), source TEXT NOT NULL,
                peer TEXT NOT NULL REFERENCES peers(id), decision TEXT NOT NULL,
                created REAL NOT NULL);
        """)
        if "approved_at" not in {row[1] for row in self.db.execute("PRAGMA table_info(channels)")}:
            self.db.execute("ALTER TABLE channels ADD COLUMN approved_at REAL")
            self.db.commit()
        from .conversations import Conversations
        self.conversations = Conversations(self.db, conversations)
        from .bindings import Bindings
        self.bindings = Bindings(self.db, self.clock)
        from .workflows import Workflows
        self.workflows = Workflows(self)

    def close(self):
        self.db.close()

    def register(self, peer: str, token: str, profile: dict | None = None):
        from .registry import PeerProfile
        profile = PeerProfile.model_validate({} if profile is None else profile).model_dump()
        profile["name"] = profile["name"] or peer
        require(peer and len(peer) <= 100, "invalid_peer", "端点名称无效")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.db:
            existing = self.db.execute("SELECT token_hash FROM peers WHERE id=?", (peer,)).fetchone()
            require(not existing or secrets.compare_digest(existing[0], digest), "peer_exists", "端点已存在，不能替换身份")
            self.db.execute("INSERT OR IGNORE INTO peers VALUES (?, ?)", (peer, digest))
        self.profiles[peer] = profile

    def authenticate(self, token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = self.db.execute("SELECT id FROM peers WHERE token_hash=?", (digest,)).fetchone()
        require(row is not None, "unauthorized", "端点凭据无效")
        return row[0]

    def channel(self, cid, peer=None):
        row = self.db.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
        require(row is not None, "not_found", "通道不存在")
        c = dict(row)
        require(peer is None or peer in (c["requester"], c["responder"]), "forbidden", "该端点不属于此通道")
        return c

    def expire(self):
        now = self.clock()
        with self.db:
            self.db.execute("UPDATE channels SET status='expired' WHERE status IN ('pending','active') AND expires<=?", (now,))
            self.db.execute("""UPDATE channels SET status='closed' WHERE status='active'
                AND idle_since IS NOT NULL AND idle_since+idle_seconds<=?""", (now,))

    async def notify(self):
        async with self.changed:
            self.changed.notify_all()

    async def verify_codex_selection(self, target, selected, *, exact=True):
        registration = self.conversations.registrations.get(target, {})
        require(self.codex_catalog is not None and registration.get('provider') == 'codex_ingress',
                'codex_binding_unavailable', 'Selected-chat routing is not enabled on this installation.')
        return await self.codex_catalog.verify(selected, registration['session_id'], exact=exact)

    async def verify_codex_route(self, cid, *, exact=False):
        binding = self.conversations.binding_request(cid)
        if binding is not None:
            channel = self.channel(cid)
            require(binding['registration'] == self.conversations.registrations.get(channel['responder']),
                    'stale_conversation_config', 'The registered Codex ingress changed.')
            await self.verify_codex_selection(channel['responder'], binding['selection'], exact=exact)
            require(self.conversations.binding_request(cid) == binding
                    and binding['registration'] == self.conversations.registrations.get(channel['responder']),
                    'stale_conversation_config', 'The pending binding or Codex ingress changed during verification.')
            return
        selected = self.conversations.existing_route(cid)
        if selected is not None:
            channel = self.channel(cid)
            await self.verify_codex_selection(channel['responder'], selected, exact=exact)
            self.conversations.reserve(cid, selected)
        else:
            native = self.db.execute('SELECT thread_id,registration FROM native_conversations WHERE channel=?', (cid,)).fetchone()
            if native and native['thread_id'] and json.loads(native['registration'])['provider'] == 'codex_ingress':
                self.conversations.reserve(cid, {'thread_id': native['thread_id']})

    async def saved_codex_target(self, peer, target):
        saved = self.conversations.saved_binding(peer, target)
        require(saved is not None, 'codex_binding_missing', 'This endpoint has no approved saved Codex target.')
        source = self.channel(saved['approved_channel'], peer)
        require(source['approved_at'] is not None and source['status'] == 'closed',
                'codex_binding_revoked', 'The saved target approval is no longer valid.')
        registration = saved['registration']
        require(registration == self.conversations.registrations.get(target),
                'stale_conversation_config', 'The registered Codex ingress changed.')
        require(self.codex_catalog is not None, 'codex_binding_unavailable', 'Codex target discovery is not enabled.')
        selected = await self.codex_catalog.verify(saved['selection'], registration['session_id'], exact=False, refresh=True)
        require(self.conversations.saved_binding(peer, target) == saved
                and self.conversations.registrations.get(target) == registration,
                'codex_binding_changed', 'Saved target changed during metadata discovery; read it again.')
        return {'state': 'binding_ready', 'target': target, 'selected_conversation': selected,
                'scope': {'requester': peer, 'responder': target}, 'task_authorized': False,
                'detail': 'Routing metadata only. Submit each new task with quick_bind and a new initiating-host approval.'}

    async def open(self, peer, target, body, key, ttl_seconds=3600, idle_seconds=120, conversation_mode="existing", *, _archive=None, selected_conversation=None, binding_only=False, expected_revision=None, expected_label=None, require_target=False):
        self.expire()
        require(isinstance(body, str) and 0 < len(body.strip()) <= 16000, "invalid_body", "求助内容须为 1 至 16000 字符")
        require(isinstance(key, str) and 0 < len(key) <= 200, "invalid_key", "每次新操作需要稳定且唯一的去重编号")
        require(10 <= ttl_seconds <= 86400 and 1 <= idle_seconds <= 3600, "invalid_expiry", "授权期限或空闲期限无效")
        require(peer != target and self.db.execute("SELECT 1 FROM peers WHERE id=?", (target,)).fetchone(), "invalid_target", "请选择另一已登记端点")
        if binding_only:
            from .conversations import BINDING_MESSAGE
            require(selected_conversation is not None and body == BINDING_MESSAGE and _archive is None
                    and conversation_mode == 'existing', 'invalid_binding_request',
                    'Saving a target uses the fixed metadata-only approval scope and an exact selected row.')
        old = self.db.execute("SELECT * FROM channels WHERE requester=? AND open_key=?", (peer, key)).fetchone()
        if old:
            binding = self.conversations.binding_request(old['id'])
            require((binding is not None) == binding_only, 'idempotency_conflict', 'A request key cannot change approval scope.')
            require((binding['selection'] if binding else self.conversations.existing_route(old['id'])) == selected_conversation,
                    'idempotency_conflict', 'A request key cannot select another chat or metadata revision.')
            previous_archive = self.conversations.archive(old["id"])
            require((previous_archive is None) == (_archive is None) and
                    (previous_archive is None or (previous_archive["source_channel"], previous_archive["target"]) ==
                     (_archive["source_channel"], _archive["target"])),
                    "idempotency_conflict", "This key belongs to a different task or archive request")
            require(old["responder"] == target and old["original"] == body and old["expires"]-old["created"]==ttl_seconds and old["idle_seconds"]==idle_seconds, "idempotency_conflict", "同一去重编号不能用于不同求助或期限")
            require(self.conversations.describe(old["id"])["mode"] == conversation_mode,
                    "idempotency_conflict", "同一去重编号不能改变新建或既有聊天模式")
            offered = self.bindings.target(old["id"])
            require(expected_revision is None or (offered is not None and offered["revision"] == expected_revision),
                    "idempotency_conflict", "A request key cannot change the bound chat revision; use a new key.")
            return {**dict(old), "conversation": self.conversations.describe(old["id"]),
                    **self._target_fields(old["id"]),
                    **({"operation": "archive"} if previous_archive else {})}
        if selected_conversation is not None:
            require(conversation_mode == 'existing' and _archive is None,
                    'unsupported_codex_binding', 'Quick binding uses an explicitly selected existing chat.')
            await self.verify_codex_selection(target, selected_conversation)
            # Metadata I/O yields; another retry may have created this same request meanwhile.
            if self.db.execute('SELECT 1 FROM channels WHERE requester=? AND open_key=?', (peer, key)).fetchone():
                return await self.open(peer, target, body, key, ttl_seconds, idle_seconds, conversation_mode,
                                       _archive=_archive, selected_conversation=selected_conversation, binding_only=binding_only)
            self.expire()
        cid, now = str(uuid4()), self.clock()
        with self.db:
            self.db.execute("INSERT INTO channels VALUES (?,?,?,?,?,?,?,?,?,?,?)", (cid, peer, target, body, "pending", now, now+ttl_seconds, idle_seconds, None, key, None))
            self.conversations.open(cid, target, conversation_mode, selected_conversation, binding_only=binding_only)
            if not binding_only and not _archive:
                # The chat shown for approval; pinned only if it is unchanged when approved.
                self.bindings.offer(cid, target, expected_revision, expected_label, require_target)
            if _archive:
                self.db.execute("INSERT INTO native_archives VALUES (?,?,?,?,?,NULL)",
                                (cid, _archive["source_channel"], _archive["registration"], _archive["target"], "pending"))
        await self.notify()
        return {**self.channel(cid), "conversation": self.conversations.describe(cid), **self._target_fields(cid),
                **({"operation": "archive"} if _archive else {})}

    def _target_fields(self, cid):
        view = self.bindings.target_view(cid)
        return {"target_chat": view} if view is not None else {}

    async def archive_open(self, peer, source_channel, conversation_id, key):
        from .conversations import DIRECT_PROVIDER, canonical
        source = self.channel(source_channel, peer)
        require(source["requester"] == peer, "forbidden", "Only the original requester can ask to archive its created chat")
        require(self.conversations.binding_request(source_channel) is None, 'binding_channel',
                'A saved target is an existing chat, not connector-created property.')
        require(isinstance(conversation_id, str) and conversation_id, "invalid_target", "Native conversation ID is required")
        previous = self.db.execute("SELECT id FROM channels WHERE requester=? AND open_key=?", (peer, key)).fetchone()
        old = self.conversations.archive(previous[0]) if previous else None
        if old:
            require(old["source_channel"] == source_channel and json.loads(old["target"])["conversation_id"] == conversation_id,
                    "idempotency_conflict", "An archive retry must keep its original channel and native conversation ID")
            archive = old
        else:
            native = self.db.execute("SELECT * FROM native_conversations WHERE channel=?", (source_channel,)).fetchone()
            require(native is not None and native["thread_id"] == conversation_id, "invalid_archive_target",
                    "Only a chat created and confirmed by this connector can be archived")
            registration = json.loads(native["registration"])
            require(registration["provider"] == DIRECT_PROVIDER, "archive_unsupported", "This native provider has no verified archive adapter")
            require(self.conversations.registrations.get(source["responder"]) == registration,
                    "stale_conversation_config", "Native provider configuration changed")
            require(native["lifecycle"] == "active", "conversation_archiving", "This chat already has an approved archive request")
            target = {k: registration[k] for k in ("project_id", "workspace_uri")}
            target["conversation_id"] = conversation_id
            archive = {"source_channel": source_channel, "registration": native["registration"], "target": canonical(target)}
        body = (f"Archive the connector-created native conversation {conversation_id} in {source['responder']}. "
                f"Original channel: {source_channel}. After the host confirms archived=true, remove its active connector "
                "registration and prevent further task delivery. Preserve the archived chat and project files. "
                "This is a separate archive authorization; it does not authorize deletion.")
        return await self.open(peer, source["responder"], body, key, _archive=archive)

    def _message(self, c, sender, kind, body, key, reply_to=None):
        recipient = c["responder"] if sender == c["requester"] else c["requester"]
        mid = str(uuid4())
        self.db.execute("INSERT INTO messages(id,channel,sender,recipient,kind,body,reply_to,sent,client_key) VALUES (?,?,?,?,?,?,?,?,?)", (mid,c["id"],sender,recipient,kind,body,reply_to,self.clock(),key))
        return dict(self.db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone())

    async def decide(self, cid, approve: bool, *, host_approval=None, host_approval_source="host_elicitation"):
        self.expire()
        c = self.channel(cid)
        if approve:
            self.workflows.check_channel(cid)
        if host_approval is not None:
            require(host_approval_source in ("host_elicitation", "host_tool_permission"),
                    "invalid_request", "审批来源无效")
            peer, decision = host_approval
            require(c["requester"] == peer, "forbidden", "只能提交本端点发起的任务决定")
            require(decision in ("accept", "decline", "cancel") and approve == (decision == "accept"),
                    "invalid_request", "宿主审批决定无效")
            previous = self.db.execute("SELECT decision,source FROM approval_decisions WHERE channel=?", (cid,)).fetchone()
            require(previous is None or (previous[0] == decision and previous[1] == host_approval_source), "invalid_state", "此任务已有不同的审批决定或来源")
        if c["status"] == ("active" if approve else "denied"):
            return {**c, **self._target_fields(cid)}
        if approve and c['status'] == 'closed' and c['approved_at'] is not None and self.conversations.binding_request(cid) is not None:
            return c
        require(c["status"] == "pending", "invalid_state", "此求助已处理或已到期")
        if approve:
            try:
                await self.verify_codex_route(cid, exact=True)
            except ConnectorError as exc:
                if exc.code in ('codex_selection_stale', 'stale_conversation_config', 'unsupported_codex_chat'):
                    with self.db:
                        self.db.execute("UPDATE channels SET status='revoked' WHERE id=? AND status='pending'", (cid,))
                    await self.notify()
                raise
            self.expire()
            c = self.channel(cid)
            require(c['status'] == 'pending', 'invalid_state', 'Authorization changed during target verification.')
        archive = self.conversations.archive(cid)
        binding = self.conversations.binding_request(cid)
        if archive and approve:
            self.conversations.require_active(archive["source_channel"])
            source = self.channel(archive["source_channel"])
            busy = self.db.execute("SELECT 1 FROM messages WHERE channel=? AND kind='question' AND resolved=0",
                                   (source["id"],)).fetchone()
            require(source["status"] != "active" or not busy, "conversation_busy", "Finish the current task before archiving its chat")
            require(self.db.execute("SELECT 1 FROM native_operations WHERE channel=? AND state IN ('intent','ambiguous')",
                                    (source["id"],)).fetchone() is None,
                    "native_execution_unconfirmed", "A native operation is still running or has an unknown result")
        if approve and self.bindings.pin_conflict(cid):
            # The approval showed a chat that is no longer bound. Never redirect: end the task.
            with self.db:
                self.db.execute("UPDATE channels SET status='revoked' WHERE id=? AND status='pending'", (cid,))
            self.record_incident(c["requester"], "target_binding_changed", cid)
            await self.notify()
            raise ConnectorError("target_binding_changed",
                                 "The bound chat changed before approval; nothing was delivered. Submit the task again.")
        with self.db:
            self.db.execute("UPDATE channels SET status=? WHERE id=?", (('closed' if binding else 'active') if approve else 'denied', cid))
            if approve:
                self.db.execute("UPDATE channels SET approved_at=? WHERE id=?", (self.clock(),cid))
                self.bindings.pin(cid)
                if binding:
                    self.conversations.remember_binding(c)
                else:
                    self._message(c, c["requester"], "question", c["original"], "initial:"+cid)
                if archive:
                    self.db.execute("UPDATE native_conversations SET lifecycle='archiving' WHERE channel=?", (archive["source_channel"],))
                    self.db.execute("UPDATE native_archives SET state='approved' WHERE request_channel=?", (cid,))
            if host_approval is not None:
                # A failed audit write must also roll back approval and initial delivery.
                self.db.execute("INSERT INTO approval_decisions VALUES (?,?,?,?,?)",
                                (cid, host_approval_source, peer, decision, self.clock()))
        await self.notify()
        return {**self.channel(cid), **self._target_fields(cid)}

    async def send(self, peer, cid, kind, body, key, reply_to=None, *, session=None):
        self.expire()
        c = self.channel(cid, peer)
        self.bindings.require_session(c, peer, session)
        self.conversations.require_active(cid)
        require(kind in ("question", "answer"), "invalid_kind", "消息类型须为 question 或 answer")
        require(isinstance(body, str) and 0 < len(body.strip()) <= 16000, "invalid_body", "消息须为 1 至 16000 字符")
        require(isinstance(key, str) and 0 < len(key) <= 200 and not key.startswith("initial:"), "invalid_key", "去重编号无效")
        old = self.db.execute("SELECT * FROM messages WHERE sender=? AND client_key=?", (peer,key)).fetchone()
        if old:
            require((old["channel"],old["kind"],old["body"],old["reply_to"]) == (cid,kind,body,reply_to), "idempotency_conflict", "同一去重编号不能用于不同消息")
            require(c["status"] == "active" or
                    (c["status"] == "closed" and c["approved_at"] is not None and c["expires"] > self.clock()),
                    "channel_unavailable", "通道尚未批准、已关闭或已到期")
            return dict(old)
        require(c["status"] == "active", "channel_unavailable", "通道尚未批准、已关闭或已到期")
        require(kind != "answer" or reply_to, "missing_reply", "回复必须指定对应问题编号")
        if reply_to:
            ref = self.db.execute("SELECT * FROM messages WHERE id=? AND channel=?", (reply_to,cid)).fetchone()
            require(ref is not None and ref["sender"] != peer and ref["kind"] == "question" and not ref["resolved"], "wrong_reply", "对应问题不存在、已回答或不属于对方")
        if kind == "answer" and peer == c["responder"]:
            self.conversations.require_executed(cid, reply_to)
        with self.db:
            msg = self._message(c,peer,kind,body,key,reply_to)
            if kind == "answer":
                self.db.execute("UPDATE messages SET resolved=1 WHERE id=?", (reply_to,))
            self.db.execute("UPDATE channels SET idle_since=NULL WHERE id=?", (cid,))
        await self.notify()
        return msg

    async def continue_channel(self, peer, cid, body, key):
        """Start or replay one requester round within the channel's original authority."""
        require(isinstance(body, str) and 0 < len(body.strip()) <= 16000, "invalid_body", "消息须为 1 至 16000 字符")
        require(isinstance(key, str) and 0 < len(key) <= 190, "invalid_key", "去重编号无效")
        client_key = "continue:" + key
        self.expire()
        c = self.channel(cid, peer)
        self.conversations.require_active(cid)
        require(c["requester"] == peer, "forbidden", "仅原任务发起端可以继续此任务")
        allowed = (c["approved_at"] is not None and c["expires"] > self.clock() and
                   (c["status"] == "active" or
                    (c["status"] == "closed" and c["idle_since"] is not None)))
        require(allowed, "channel_unavailable", "任务授权已结束；不会重新打开、延长或替换此任务")
        old = self.db.execute("SELECT * FROM messages WHERE sender=? AND client_key=?", (peer, client_key)).fetchone()
        if old:
            require((old["channel"], old["kind"], old["body"], old["reply_to"]) ==
                    (cid, "question", body, None), "idempotency_conflict", "同一续接编号不能用于不同任务或消息")
            return dict(old)
        await self.verify_codex_route(cid)
        self.expire()
        c = self.channel(cid, peer)
        require(c['status'] in ('active', 'closed') and c['approved_at'] is not None and c['expires'] > self.clock(),
                'channel_unavailable', 'Authorization ended while checking the selected chat.')
        old = self.db.execute('SELECT 1 FROM messages WHERE sender=? AND client_key=?', (peer, client_key)).fetchone()
        if old:
            return await self.continue_channel(peer, cid, body, key)
        require(c["expires"] > self.clock(), "channel_unavailable", "任务授权已结束；不会重新打开、延长或替换此任务")
        with self.db:
            self.db.execute("UPDATE channels SET status='active', idle_since=NULL WHERE id=?", (cid,))
            msg = self._message(c, peer, "question", body, client_key)
        await self.notify()
        return msg

    async def finish(self, peer, cid):
        self.expire()
        c = self.channel(cid,peer)
        require(c["status"] == "active", "channel_unavailable", "通道未处于交流状态")
        pending = self.db.execute("SELECT 1 FROM messages WHERE channel=? AND kind='question' AND resolved=0", (cid,)).fetchone()
        require(not pending, "unanswered_questions", "仍有未完成求助，不能开始空闲关闭计时")
        with self.db:
            self.db.execute("UPDATE channels SET idle_since=COALESCE(idle_since, ?) WHERE id=?", (self.clock(),cid))
        await self.notify()
        return self.channel(cid)

    async def revoke(self, cid):
        self.channel(cid)
        with self.db:
            self.db.execute("UPDATE channels SET status='revoked' WHERE id=?", (cid,))
            self.db.execute('DELETE FROM native_bindings WHERE approved_channel=?', (cid,))
        await self.notify()
        return self.channel(cid)

    async def receive(self, peer, cid=None, after=0, timeout=300, *, session=None):
        require(isinstance(after,int) and after>=0 and 0<=timeout<=3600, "invalid_wait", "等待游标或时限无效")
        if cid:
            self.bindings.require_session(self.channel(cid, peer), peer, session)
        deadline = asyncio.get_running_loop().time()+timeout
        async with self.changed:
            while True:
                self.expire()
                c = self.channel(cid,peer) if cid else None
                if cid:
                    self.workflows.check_channel(cid)
                blocked = self.workflows.blocked_channels(peer) if cid is None else []
                rows = self.db.execute("""SELECT m.* FROM messages m JOIN channels c ON c.id=m.channel
                    LEFT JOIN native_conversations n ON n.channel=c.id
                    LEFT JOIN task_targets t ON t.channel=c.id
                    WHERE m.recipient=? AND m.seq>? AND (? IS NULL OR m.channel=?)
                    AND m.channel NOT IN (SELECT value FROM json_each(?))
                    AND (t.channel IS NULL OR m.recipient!=c.responder OR t.session=?)
                    AND c.status='active' AND (m.recipient!=c.responder OR (
                        NOT EXISTS (SELECT 1 FROM native_archives a WHERE a.request_channel=c.id) AND
                        (COALESCE(json_extract(n.registration,'$.provider'),'')!='antigravity_sidecar' OR
                         (m.channel=? AND n.thread_id IS NOT NULL AND n.lifecycle='active'))))
                    ORDER BY m.seq LIMIT 100""", (peer,after,cid,cid,json.dumps([b["channel"] for b in blocked]),session,cid)).fetchall()
                if rows or blocked or (c and c["status"] not in ("active","pending")):
                    with self.db:
                        messages = [self.conversations.delivery(dict(r)) for r in rows]
                    self.conversations.observed(rows)
                    self._observe(peer, rows)
                    return {"state":c["status"] if c else ("active" if rows else "blocked"),
                            "messages":messages, "cursor":rows[-1]["seq"] if rows else after,
                            **({"blocked_channels": blocked} if blocked else {})}
                remaining = deadline-asyncio.get_running_loop().time()
                if remaining<=0:
                    return {"state":"waiting", "messages":[], "cursor":after}
                next_expiry = self.db.execute("""SELECT MIN(CASE WHEN idle_since IS NULL THEN expires
                    ELSE MIN(expires,idle_since+idle_seconds) END) FROM channels
                    WHERE status IN ('pending','active') AND (requester=? OR responder=?)""", (peer,peer)).fetchone()[0]
                wait = min(remaining,max(0.001,next_expiry-self.clock())) if next_expiry else remaining
                try:
                    await asyncio.wait_for(self.changed.wait(),wait)
                except TimeoutError:
                    pass

    def delegation_result(self, peer, cid):
        self.expire()
        c = self.channel(cid)
        require(c["requester"] == peer, "forbidden", "仅任务发起端点可以读取委派结果")
        result = {"state": c["status"], "channel": cid, **self._target_fields(cid)}
        binding = self.conversations.binding_request(cid)
        if binding is not None:
            saved = self.conversations.saved_binding(peer, c['responder'])
            state = ('bound' if saved is not None and saved['approved_channel'] == cid else 'superseded') if c['approved_at'] is not None and c['status'] == 'closed' else c['status']
            return {**result, 'state': state, 'operation': 'binding',
                    'conversation': self.conversations.describe(cid), 'task_authorized': False,
                    'scope': {'requester': peer, 'responder': c['responder']},
                    'detail': 'Saved target metadata grants no tasks. Refresh this endpoint target and approve each new task separately.'}
        archive = self.conversations.archive(cid)
        if archive:
            result.update(operation="archive", conversation=self.conversations.describe(archive["source_channel"]))
            if archive["state"] in ("failed", "ambiguous"):
                return {**result, "state": "archive_" + archive["state"], "detail": json.loads(archive["receipt"])}
        # Closing preserves the archive, not an indefinite authorization to replay it.
        if c["status"] in ("active", "closed") and c["expires"] <= self.clock():
            return {"state": "expired", "channel": cid}
        if c["status"] in ("active", "closed") and c["approved_at"] is not None:
            if self.conversations.describe(cid)["mode"] == "new" or self.conversations.existing_route(cid) is not None:
                result["conversation"] = self.conversations.describe(cid)
                initial = self.db.execute("SELECT id FROM messages WHERE channel=? AND client_key=?",
                                          (cid, "initial:" + cid)).fetchone()
                if initial and self.conversations.unconfirmed(initial[0]):
                    return {**result, "state": "native_execution_unconfirmed",
                            "detail": "Native execution could not be confirmed. Do not start a replacement or resend; inspect the original native tool result."}
            row = self.db.execute("""SELECT a.* FROM messages a JOIN messages q ON a.reply_to=q.id
                WHERE q.channel=? AND q.client_key=? AND q.sender=? AND q.kind='question'
                AND a.channel=q.channel AND a.sender=? AND a.kind='answer'""",
                (cid, "initial:"+cid, peer, c["responder"])).fetchone()
            if row is not None:
                result["answer"] = dict(row)
        return result

    def round_result(self, peer, cid, question_id):
        self.expire()
        c = self.channel(cid, peer)
        require(c["requester"] == peer, "forbidden", "仅任务发起端可以读取续接结果")
        state = "expired" if c["status"] == "closed" and c["expires"] <= self.clock() else c["status"]
        if c["approved_at"] is None or state not in ("active", "closed") or c["expires"] <= self.clock():
            return {"state": state, "channel": cid, "question_id": question_id,
                    "answer": None, "questions": []}
        question = self.db.execute("SELECT * FROM messages WHERE id=? AND channel=? AND sender=? AND kind='question'",
                                   (question_id, cid, peer)).fetchone()
        require(question is not None, "not_found", "续接问题不存在")
        answer = self.db.execute("SELECT * FROM messages WHERE reply_to=? AND sender=? AND kind='answer'",
                                 (question_id, c["responder"])).fetchone()
        pending = [dict(row) for row in self.db.execute(
            "SELECT * FROM messages WHERE channel=? AND sender=? AND kind='question' AND resolved=0 ORDER BY seq",
            (cid, c["responder"]))]
        return {"state": "native_execution_unconfirmed" if self.conversations.unconfirmed(question_id) else c["status"],
                "channel": cid, "question_id": question_id,
                "answer": dict(answer) if answer else None, "questions": pending,
                "conversation": self.conversations.describe(cid)}

    def _observe(self, peer, rows):
        # Delivery is preserved when an observer fails, but the failure is reported:
        # logged with its traceback and recorded as an incident visible in status/admin.
        for callback in self.observers:
            try:
                callback(peer, [dict(r) for r in rows])
            except Exception as exc:
                logger.exception("delivery observer failed for peer %s", peer)
                self.record_incident(peer, "observer_error", f"{type(exc).__name__}: {exc}"[:500])

    def snapshot(self, peer=None, session=None):
        self.expire()
        rows = self.db.execute("SELECT * FROM channels WHERE ? IS NULL OR requester=? OR (responder=? AND approved_at IS NOT NULL) ORDER BY created DESC", (peer,peer,peer)).fetchall()
        incidents=self.db.execute("SELECT * FROM incidents WHERE ? IS NULL OR peer=? ORDER BY created DESC LIMIT 20",(peer,peer)).fetchall()
        peers = [r[0] for r in self.db.execute("SELECT id FROM peers ORDER BY id")]
        workers = [{"id": p, **self.profiles.get(p, {}), "availability": "unknown",
                    "conversation_modes": ["existing", "new"] if p in self.conversations.registrations else ["existing"],
                    **({"cleanup_actions": ["archive"]} if self.conversations.registrations.get(p, {}).get("provider") == "antigravity_sidecar" else {}),
                    **({"binding": self.bindings.describe(p, session if p == peer else None)} if p in self.bindings.bindable else {})}
                   for p in peers]
        channels = []
        for row in rows:
            channel = dict(row)
            conversation = self.conversations.describe(row["id"])
            if conversation['mode'] == 'new' or conversation.get('selected') is not None:
                channel["conversation"] = conversation
            archive = self.conversations.archive(row["id"])
            if archive:
                channel.update(operation="archive", source_channel=archive["source_channel"], archive_state=archive["state"])
            channel.update(self._target_fields(row["id"]))
            channels.append(channel)
        return {"channels":channels, "peers": peers, "workers": workers,
                "self": peer, "incidents":[dict(r) for r in incidents]}

    async def request_binding(self, peer, session, label, key):
        result = self.bindings.request(peer, session, label, key)
        await self.notify()
        return result

    async def decide_binding(self, peer, session, request_id, decision, source):
        result = self.bindings.decide(peer, session, request_id, decision, source)
        await self.notify()
        return result

    async def unbind(self, peer, session=None, *, admin=False):
        result = self.bindings.unbind(peer, session, admin=admin)
        await self.notify()
        return result

    def enroll(self, peer, session, secret):
        return self.bindings.credentials.enroll(peer, session, secret)

    def record_incident(self,peer,code,detail):
        with self.db:
            self.db.execute("INSERT INTO incidents VALUES (?,?,?,?,?)",(str(uuid4()),peer,code,detail,self.clock()))
