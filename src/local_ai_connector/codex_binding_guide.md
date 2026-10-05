# Codex quick binding: isolated candidate

This candidate saves an initiating endpoint's selected existing Codex chat for
later collaboration, then binds separately approved task channels to that chat.
It retains the installed trusted ingress and does not replace
its credentials, native session identity, project configuration or wake target.
The selected chat receives ordinary native messages and needs no connector MCP
installation. All AI messages and collaboration must be in English.

## Human interaction

1. When the human says they created Codex chat "xxx" for collaboration, call
   `connector_codex_binding_catalog(target=<registered Codex ingress>)`.
   Paginate with `next_cursor` to resolve the exact named chat. Match the
   human-supplied title to a unique exact catalog row; for duplicate titles,
   show workspaces and IDs and ask which row. Never choose by recency or fuzzy
   matching. Preserve the entire selected row. Read this MCP resource first;
   if resource reading is unavailable, use permitted file tools to read only
   the catalog's `binding_usage.guide_path` from the caller's installed runtime.
   The catalog already validates each row's local identity and eligibility.
   Once a unique exact row is found, proceed directly to step 2 using that row.
   Source-code inspection and terminal commands are not part of ordinary
   binding. The binding tool revalidates the selection before saving it.
   If a tool fails, report its exact error and the required next action; inspect
   implementation files only when the human separately requests debugging.
   If all pages are exhausted without an exact match, ask the human to confirm
   the title or workspace. A file-read permission prompt approves reading a
   file; the binding approval is the tool's exact-chat metadata confirmation.
2. Call `connector_codex_bind_chat(target=..., selected_conversation=<row>,
   request_key=<stable key>)`. Approval occurs in the initiating Desktop and
   shows the exact selected chat and metadata-only purpose. This sends nothing
   to Codex and does not reserve the chat. `state=bound` means the preference
   was saved; it does not prove the target is online or has acknowledged it.
   Decline or Cancel preserves any previous saved preference.
   The preference is scoped to the authenticated requester endpoint and
   registered ingress, not to an unverified initiating native chat ID. Both
   Antigravity and Claude can remember the same target. It persists across
   tasks but grants no standing task communication authority.
3. For each later independent task, call `connector_codex_bound_chat(target=...)`
   to refresh metadata for the saved ID and workspace. Then call
   `connector_codex_quick_bind` with the registered ingress endpoint,
   `selected_conversation=<entire selected row>`, the complete original task,
   a new stable task `request_key`, and a bounded wait. The initiating host shows the
   exact native chat identity, workspace and original task for approval.
   Decline or Cancel sends nothing. Do not invent approval or auto-submit a form.
4. The connector verifies metadata, reserves that target, and prepares an exact
   native SEND operation. The already trusted ingress forwards it using the
   installed one-use authorization hook. Native receipt must return the approved
   thread ID. A different returned ID is uncertain/wrong-target execution:
   stop, retain evidence and never silently rebind or resend.
5. Read the actual target answer through the ingress. A send acknowledgment
   proves submission only. Use `connector_continue(channel=...)` for later
   rounds; no new target or extended authorization is accepted there.

Ordinary `connector_delegate` and workflow tools still address the registered
ingress; use the explicit `bound_chat` then `quick_bind` route for saved targets.
Changing a saved preference never moves an existing task channel.

## Initiating host approval and waiting

Antigravity uses the configured elicitation transport: the bind operation
returns after its initiating-Desktop confirmation. Task quick_bind waits for
the actual result. For a first-task clarification, resume quick_bind with the
same original selection, task and request key, adding the returned question's
`reply_to` and the human's `reply`. For a clarification during continuation,
resume `connector_continue` with its unchanged channel/message/key and reply.
Requester profiles need not expose raw send/receive tools.

Claude Code uses its already configured `host_tool_permission` transport. Both
bind_chat and quick_bind require the native permission prompt on every call.
bind_chat returns the saved preference immediately. Task quick_bind returns an
authorized channel immediately: call `connector_receive`, advance its cursor,
answer clarifications with `connector_send` on that channel, and finish only
after all questions are resolved. Never repeat quick_bind just to wait, and
do not pass `reply_to`/`reply` to it on this transport. Same-task later rounds
use connector_continue. Independent file/command permissions stay with the
worker host. Do not switch approval transports after an error or auto-approve.

## Conditions and truthful failure states

- The registered Codex ingress, its communication permissions and its exact
  native-action hooks must already be installed, trusted and available. Initial
  project trust or MCP loading may require human action once. This candidate
  does not change independent host or organization policy. The selected chat's
  file, command and other tool permissions remain its own.
- Catalog metadata comes from the configured official local App Server using
  only `thread/list` and `thread/read(includeTurns=false)`. App Server startup
  needs normal local state access. Its separate-process `notLoaded` status says
  nothing about the Desktop chat's busy/idle state. This API is experimental.
- Reject the ingress itself, unsupported remote/cloud/ephemeral targets,
  archived targets, or changed identity/workspace/selection evidence. A stale
  pending selection is revoked before any native execution; refresh, reselect
  and use a new request key and a new initiating approval. An unavailable query
  is an explicit error, not a fabricated stale/idle result.
- One target cannot serve overlapping pending, active or uncertain native
  task operations. Saving a preference reserves nothing. Do not revoke
  unrelated tasks to get a reservation. Closed tasks
  may be followed up only within their original authority and if the target is
  still free. Target/operation identity and authority are checked again after
  external metadata I/O and before consuming the grant.
- A prepared operation may execute once. An intent or ambiguous result may be
  inspected but never automatically repeated. Cancellation after native send
  cannot retract the delivered message. A target answer must not be fabricated
  by the ingress. Selected existing chats are not connector-created property
  and are never archive-eligible through this route.
- Revoke a saved binding through its formal approval record. Only the preference
  that still references that record is removed; revoking an old superseded
  record cannot remove a newer selection. Saved targets are refreshed by ID and
  workspace, never re-resolved by title after replacement or deletion.

## Existing binding fallback

If candidate tools are absent, host approval is unavailable, or metadata cannot
be verified, report the exact failure and leave the requested chat unbound.
Reuse the previously verified Codex registration and existing-task delegation
method only within the user's fallback authorization. Identify the actual
registered chat and any shared-binding change before using that method; do not
claim the newly named chat was bound when only the old ingress is available.
The existing Codex registration installer is a fixed-ingress setup, not a
general arbitrary-chat rebind tool. Do not rerun it to replace an existing
registration. If only the old registered chat is usable, state that limitation
and identify it. Binding a new arbitrary target remains pending rather than
being claimed as complete. Preserve normal host trust and prior denial stops.

## Availability and acceptance

The feature is deliberately opt-in: the isolated server needs its configured
`codex_binding_cli`, and its stdio caller uses `--codex-quick-binding`. Production
readiness and native acceptance must be checked separately after opt-in setup. Existing
connections may cache their old tool inventory; loading new tools is a setup
step, not a per-binding terminal operation.

Isolated protocol tests use synthetic answers and hook receipts. Native
acceptance must separately observe the chosen chat, exact SEND receipt, actual
`47 * 19 = 893`, same-channel `+7 = 900`, and real host approval counts. Do not
claim one-approval production completion from the synthetic tests alone.
