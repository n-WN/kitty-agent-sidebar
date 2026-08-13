# Recovery

Use the newest complete snapshot. Match an attachment by Session ID and manual tab label.
Do not restore identity from a title alone.

Create the Kitty tab with the user's interactive shell as the root process. Then send the
agent resume command to that shell. Do not launch the agent as Kitty's root child. This
keeps `/quit` behavior correct: after the agent exits, the user returns to the shell.

A Session ID can appear in more than one pane. Preserve all locations; do not deduplicate
only by Session ID.
