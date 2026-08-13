# State matrix

| Event | Codex | Claude Code | Display |
|---|---|---|---|
| SessionStart | official Hook | official Hook | working for Codex first turn; ready for Claude |
| UserPromptSubmit | official Hook | official Hook | working |
| PreToolUse | official Hook | official Hook | working |
| PermissionRequest | official Hook | official Hook | action |
| Stop | official Hook | official Hook | ready, verified by Codex rollout at next snapshot |
| StopFailure | not configured | official Hook | error |
| SessionEnd | official Hook | official Hook | disconnected |

Codex 0.147.0 source was checked for these Hook events. Claude Code is treated as a closed
component; only documented Hook JSON is consumed. The bridge never changes agent behavior
and always returns success after telemetry errors.
