# Review record

External model reviews are advisory. They do not override source or runtime evidence.
Raw transcripts are kept only in ignored `.local/model-reviews/` because they can contain
local implementation context.

Release changes accepted from review:

- Use OSC `SetUserVar` for the Hook hot path.
- Move seen watermarks into pane-local Kitty user variables. Selected, visible rendering makes
  at most one idempotent Kitty user-variable write per new sequence and has no file I/O.
- Use Kitty 0.30 as the collector RC client version.
- Add process start time to multi-instance identity.
- Append snapshot records with `O_APPEND` and `fsync`.
- Separate optional GPL Kitty patch from the stock-Kitty integration.
- Keep direct runtime fallback in a separate user variable; it cannot overwrite Hook state
  or advance unread sequence.
- Use deterministic same-session Hook priority and live different-session runtime priority.
- Add transactional managed-file backups, exact marked Hook ownership, and safe uninstall.
- Preserve the established sidebar palette while keeping status and runtime fixes separate.
- Use local receipt time for cross-source ordering and expire unverified Hook state after the
  direct-runtime freshness window.

Two additional reviews were run through Pi with `claude-fable-5`; a second independent
check used `kimi-k3`. Raw outputs remain ignored under `.local/model-reviews/`. Findings
that contradicted inspected Kitty/Codex runtime behavior were not accepted.
