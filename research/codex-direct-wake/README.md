# Codex direct wake-up test

These scripts test that a task wakes the exact Codex Desktop chat it was pinned to at approval,
without a relay chat. Everything runs in one disposable folder:

- `setup codex --wake` runs under a throwaway home inside the folder, so global configuration is not written.
- The folder's project config (`.codex/config.toml`) loads the test server and turns the live
  `local_ai_connector` server off, for chats in this folder only. Codex applies it once the folder
  is trusted.
- Setup's wake-up binding (`"target": "pinned"`) wakes the chat each task was pinned to, and the Codex
  bridge keeps its dispatch records inside the profile.

| Script | Role |
| --- | --- |
| `prepare.py` | Creates the profile with wake-up and the folder's project config. |
| `initiator.py` | Terminal stand-in for an initiating desktop. Only a typed `yes` approves a task. `status` shows the binding, recent wake dispatches and incidents. |

```sh
.venv/bin/python research/codex-direct-wake/prepare.py ~/Developer/codex-wake-test
.venv/bin/python research/codex-direct-wake/initiator.py status
.venv/bin/python research/codex-direct-wake/initiator.py delegate wake-1 "What is 19 * 23? Reply with the number." --chat "Wake test A" --revision 1
```

Run these with the Python environment of the checkout under test: the MCP entry and the wake
bridge use it.

## Procedure

1. In Codex Desktop, open the test folder as a project and trust it.
2. Start chat A there and ask which connector tools it has; only `local_ai_connector_cwtest`
   should appear. Then say "Use this chat for connector tasks. Label it Wake test A." and approve.
3. Start chat B in the same folder. Do not bind it. Leave both chats idle and open in Codex.
4. Run `initiator.py delegate …` with the label and revision from `status`, and type `yes`.
   Chat A should start a turn by itself, collect the task and answer; the initiator prints the answer.
5. In chat B, say "Check for connector tasks." It must not receive chat A's task.
6. Bind chat B, then delegate a new task with its label and revision. Chat B should wake; chat A should not.

Remove the test with `rm -rf` on the folder and, if Codex recorded it, the folder's trust entry in
Codex settings. The live installation is never touched.
