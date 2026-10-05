# Example configurations

Placeholder-only examples for the files that the CLI does not generate. Replace every
`/absolute/path/to/...` and every all-zero ID with your own values. None of these files
contain credentials; endpoint tokens and approval tokens are created by
`local-ai-connector init`, `peer-add` and `enable-chat-approval` and stay in the private
data directory (mode 0600).

| File | Where it goes | Notes |
| --- | --- | --- |
| `wakeup.example.json` | `<data>/wakeup.json` | Optional wake-up adapters. Keep only the bindings you use; every key must be a registered endpoint. The file names a command to run, so it must not be group/world writable. |
| `server-conversations.example.json` | merge the `conversations` object into `<data>/server.json` | Optional native-conversation providers (`codex_ingress`, `antigravity_sidecar`). Normally written by `deployment/enable-native-conversations.command` and `deployment/enable-antigravity-native-conversations.command`. |
| `mcp-stdio-source-checkout.example.json` | the host's MCP settings | stdio launcher for a source checkout. `local-ai-connector client-config --peer NAME` prints the equivalent entry for your actual paths. |
| `antigravity-sidecar.example.json` | `~/.gemini/config/sidecars/<name>/sidecar.json` | Antigravity sidecar that hosts the socket bridge for the `gemini` wake binding. |

Generated rather than shipped as examples:

- Endpoint files and `server.json`: `local-ai-connector --data <data> init --peers ...`
- MCP client entries (generic JSON, Codex TOML, ZCode): `local-ai-connector --data <data> client-config --peer NAME [--client codex|zcode] [--transport streamable-http]`
- macOS LaunchAgent: `local-ai-connector --data <data> service-config`

## Endpoint names expected by the integration helpers

The core service accepts any endpoint names. The host integration and maintenance helpers
under `integrations/` and `deployment/` currently look for these fixed names, so the
examples use them:

| Endpoint | Host role |
| --- | --- |
| `gpt` | Codex requester (global Codex MCP entry) |
| `codex_desktop` | dedicated Codex Desktop worker chat |
| `gemini` | Antigravity worker |
| `claude_code` | Claude Desktop Code worker |

Those helpers also assume the data directory `~/.local/share/local-ai-connector` and the
LaunchAgent label `dev.local-ai-connector.service`, and they run from a source checkout
prepared with `uv sync` (they call `<checkout>/.venv/bin/python`).

## Wake-up options worth knowing

- `send_when_unknown: true` is required when a bridge cannot report idle/busy. The
  Antigravity and Claude bridges report `unknown`; with `false` they are never woken.
- `restore: true` is accepted only on command bindings and is implemented only by the
  Codex Desktop bridge. It allows a one-shot navigation to the registered chat when Codex
  Desktop has no loaded owner for it (Codex may come to the foreground). The Claude bridge
  requires `restore: false`.
- The socket adapter needs an absolute socket path whose parent directory is owned by the
  current user with mode 0700.
