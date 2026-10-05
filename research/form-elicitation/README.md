# Native form diagnostic

This standalone stdio MCP server compares one required string field with one required Boolean field. It does not import the connector, use credentials, send tasks, or grant permissions. Its only tool is `probe_form(field_type, probe_id)`; the host must handle any actual form. The JSONL trace records protocol, schema, response action and timing, excluding entered values and authenticated request state.

Run from the package directory:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q research/form-elicitation/test_probe.py -p no:cacheprovider
.venv/bin/python research/form-elicitation/probe.py --evidence /absolute/path/to/form-probe.jsonl
```

Use the second command as a temporary MCP server command with an explicit evidence path. Remove that server entry and refresh the host after testing. This diagnostic is separate from the production connector's tool profiles.

On 2026-09-22, the 8 simulated cases passed. Antigravity 2.15.1 advertised form support over MCP 2026-07-28, but the text and Boolean probes returned `cancel` after 3.346 ms and 1.451 ms respectively. No form was observed by Codex. Both configurations and production source were preserved, and the temporary server was removed. This reproduces the issue outside the connector's task logic; it does not identify the exact host cancellation path. See live evidence (`deployment/form-probe-20260922.json`, maintainer-local record, not published).

## Correlation diagnosis (2026-09-27–28)

`wire_probe.py` implements the diagnostic with standard-library JSON-RPC, independent of the MCP SDK. Choose `--mode modern` or `--mode legacy`, supply `--evidence /absolute/path/to/trace.jsonl`, and optionally add `--forward-progress-token`. That option copies the tool call's original progress token into elicitation metadata; it is routing metadata, never approval authority.

On Antigravity 2.17.0, all four baseline combinations canceled immediately. Forwarding the token made the modern text form visible; that call subsequently timed out after 180 seconds without a recorded submission. The correlated legacy Boolean form returned `accept` after 13512.481 ms. Read-only inspection of the installed handler corroborated a callback lookup keyed by `progressToken`, with immediate cancel on missing metadata or callback.

The expanded SDK/raw-wire/correlated-wire suite passed 24 cases in 3.56 seconds. Production delegation source was not changed. Both temporary server entries were removed, and a host refresh confirmed the original connector's five tools remained. See diagnosis evidence (`deployment/wire-probe-20260927.json`, maintainer-local record, not published).


## Native tool-permission diagnostic (2026-09-28)

`permission_probe.py --evidence /absolute/path/events.jsonl` is a separate stdio server with one `probe_permission` tool carrying `_meta["anthropic/requiresUserInteraction"]: true`. It only records synthetic target/task text and request/protocol metadata; it imports no connector code and holds no credentials. Local wire smoke verification confirmed the flag is advertised and one direct call creates one diagnostic record. A direct test-client call is not evidence of a human permission gate.

Live project: a separate Claude Desktop Code test project and conversation, current Auto mode. The existing worker project's connector configuration was preserved. First live call `claude-native-accept-20260928-a` executed once under protocol 2026-07-28; declared capabilities include form and URL elicitation. Claude reported native approval, but the root did not observe the prompt and awaits the owner's confirmation. Do not count this as a passed human-consent trial until correlated. Live evidence: `deployment/claude-permission-probe-20260928.jsonl`. Deny, cancel and repeated-call trials are pending. Keep this temporary MCP entry only while the live diagnostic is active; remove after the diagnostic finishes.

Live follow-up: owner confirmed full text and Allow once on the first call. Deny on the second call produced no execution. Repeating the exact first arguments displayed a fresh Deny / Allow once prompt. Total executions remained one. Stop/cancel was not verified (owner found only accept/deny). The temporary project MCP entry has been removed; the idle session and evidence are retained. See the matching `-summary.json` for limits and cleanup state.
