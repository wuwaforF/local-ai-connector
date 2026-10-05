# Claude chat binding through the existing terminal script

Use this guide when the human explicitly requests binding an existing Claude
Desktop Code chat. A caller with terminal tools can perform the registration;
the MCP inspection tools do not launch a terminal or execute registration.
An installation and a registered `claude_code` endpoint must already exist.
This workflow does not create a chat, change its model or archive it.

## Required inputs and authority

1. Call `connector_claude_binding_catalog`. Show numbered titles and workspaces,
   and retain the exact row selected by the user. Inspect that row with its
   `desktop_id` and `selection_revision`. A stale row requires a new selection.
   `native_authorization_unavailable` is a local inspection-only result; no
   Claude approval was attempted. It does not establish a host refusal.
2. Resolve only that Desktop ID's metadata file and verify its CLI UUID,
   `cwd`, `originCwd` and unarchived state. Desktop and CLI IDs are different
   identities; never derive one by removing a prefix or match by title alone.
3. Require a legitimate current address receipt from this same selected chat.
   Keep its actual source evidence. A guessed path, an old chat's receipt or a
   model-written JSON object is not sufficient. If the receipt is missing or a
   collection request was denied, stop and state this prerequisite. Never retry
   the denied outcome through another command, tool, environment extraction,
   socket enumeration, hooks or permission-mode/rule changes.
4. Confirm your terminal and filesystem tools can access this installation and
   its local data. Guide discovery, a peer message and an MCP call grant no
   permissions. If access is unavailable, report the exact launcher and missing
   inputs so an authorized terminal-capable caller can perform the steps.
5. Establish installation-wide idle before the first write, including capture,
   using the authorized local CLI status below. `connector_status` is filtered
   to the caller and cannot establish global idle. If any pending/active channel
   remains, stop and report it; this binding request does not authorize finishing
   or revoking unrelated tasks. `capture --apply` does not enforce this channel
   guard; the later binding installer does. Preserve unrelated state and
   credentials. Scope must authorize the selected replacement or first binding.

## Installation paths and command preparation

The MCP resource resolves the following paths from local startup configuration.
When reading the packaged file directly, replace its two `@@...@@` placeholders
using `manual_binding.installation_root` and `manual_binding.data_directory`
from the catalog. Use proper shell quoting, not JSON quoting.

Run in a Bash or Zsh terminal. Assign the remaining values from the exact
selected metadata and accepted receipt, treating them as data. The record name
is the verified CLI UUID plus `.json`, under this installation's data directory.
Do not copy endpoint credentials into commands or the conversation.

```sh
connector_root=@@INSTALLATION_ROOT@@
connector_data=@@DATA_DIRECTORY@@
register="$connector_root/deployment/register-claude-code.command"
metadata='<absolute metadata path for the selected Desktop ID>'
desktop_id='<selected Desktop sessionId>'
cli_id='<verified cliSessionId>'
workspace='<verified absolute cwd and originCwd>'
socket='<actual current address from the accepted selected-chat receipt>'
record="$connector_data/$cli_id.json"
binding_args=(--metadata "$metadata" --desktop-id "$desktop_id" --cli-id "$cli_id"
  --workspace "$workspace" --session-record "$record" --data "$connector_data")
```

The launcher accepts these explicit flags and invokes the existing onboarding
implementation. Calling it without arguments starts an interactive combined
receipt wizard whose native acceptance is unverified. Do not use that wizard
as an automatic fallback.

Before any write, query installation-wide status through the existing local
admin CLI. Inspect channel statuses and IDs; do not repeat unrelated task bodies
in a reply. A failed query or any pending/active channel means stop. This check
is a snapshot; stop if relevant state changes, and the installer checks again.

```sh
"$connector_root/.venv/bin/python" -m local_ai_connector.cli --data "$connector_data" status
```

## Capture, preview and apply

First preview the selected chat's address capture. Review the exact identity,
workspace and address source. Within the user's authorized scope, capture it:

```sh
"$register" "${binding_args[@]}" --capture-socket "$socket"
"$register" "${binding_args[@]}" --capture-socket "$socket" --apply
```

Next preview the binding. For an installation with an existing Claude binding:

```sh
"$register" "${binding_args[@]}"
```

For the first binding to an already registered endpoint, this initial preview
must also include `--create-binding`:

```sh
"$register" "${binding_args[@]}" --create-binding
```

Each invocation validates its current metadata and address. Separate capture
preview/apply commands do not pin address identity across invocations. If the
chat restarts, any relevant state changes or an output mismatches the reviewed
target, stop and require fresh target evidence. Do not assume a prior preview
still applies.

Review `binding_changes`, `previous_binding_sha256`,
`wake_compatibility_issues` and `service_running`. The bridge requires
`enabled=true`, `send_when_unknown=true` and `restore=false`; an incompatible
configuration must be reported rather than silently changed.

For an existing binding replacement, retain the exact digest from this preview,
preview that same replacement and then apply the reviewed candidate:

```sh
previous_binding_sha256='<exact digest from this preview>'
"$register" "${binding_args[@]}" --replace-binding "$previous_binding_sha256"
"$register" "${binding_args[@]}" --replace-binding "$previous_binding_sha256" --apply
```

For a first binding to an already registered endpoint, use `--create-binding`
in both preview and apply instead of `--replace-binding`. For an unchanged
binding, ordinary `--apply` verifies a no-op. Show the target and concrete
changes. Preserve already explicit user authorization; ask only for a change
outside that scope. Required host terminal/file approvals still apply. Never
manufacture a native approval or automate an interactive confirmation response.

The installer checks the actual candidate, preserves credentials and other
bindings, retains a rollback backup, and verifies the installed configuration
and service. Report an actual error and inspect retained state before retrying;
do not replace stale digests, repeat uncertain writes or claim success from a
chat title. Do not remove backups while acceptance remains pending.

## Message acceptance

After binding, use normal connector delegation and its initiating-host approval
to ask the selected Claude chat to calculate `47 * 19` in English. Verify the
real answer `893`, then use `connector_continue` on that same channel to add
`7` and verify `900`. Reuse request keys on retries. Local binding checks and
MCP Connected do not prove a message roundtrip. Verify the selected native chat
received both rounds. Claude archive remains a human operation.
