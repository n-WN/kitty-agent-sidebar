# Privacy and storage

Session IDs and manual tab labels can be sensitive.

On macOS, snapshots are stored in:

`~/Library/Application Support/kitty-agent-status.noindex/snapshots/`

The directory mode is `0700`; files are `0600`. `.metadata_never_index` is created. A
snapshot does not contain screen text, prompt text, tool input/output, cwd, or environment
values. The Hook diagnostic log does not contain Session IDs.

Default retention is 30 days, with a 128 MiB total cap. Closed daily files are compressed
after 24 hours. Each JSONL record is appended through `O_APPEND`, followed by `fsync`.
Readers must ignore a truncated final line after an unclean shutdown.

The installer does not delete snapshots on uninstall. Removal is an explicit user action.
