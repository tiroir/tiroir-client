# tiroir

`tiroir work` takes an agent session that runs on a tiroir hub and gives you your own copy of it
on your laptop, in Claude Code, with the whole conversation so far. You carry on from where it
stands, under your own name, with your own tools.

It works with a **tiroir hub**: a server that shows shared markdown documents ("sheaves") and
keeps the transcripts of the agent sessions working on them. Without such a hub this command has
nothing to do.

## What you need

- macOS, Linux or Windows;
- [uv](https://docs.astral.sh/uv/) (or pipx), to run the command;
- [Claude Code](https://docs.claude.com/en/docs/claude-code), with the `claude` command on your
  `PATH`;
- an account on the hub's front, and a sheaf that is open to you.

## Run it

Copy the command from the sheaf's page (☰ menu, **Work on this**, **Fork to your machine**), or
type it with the sheaf's address:

```bash
uvx tiroir work https://hub.example.com/list/acme/site/plan.md
uvx tiroir work https://hub.example.com/list/acme/site/plan.md --cwd ~/work/acme-site
uvx tiroir work https://hub.example.com/list/acme/site/plan.md --dry-run
```

To keep it installed instead: `uv tool install tiroir`, then `tiroir work <address>`.

## What it does, on your laptop only

1. Downloads the transcript of the sheaf's live session from the hub, as you. Only a sheaf that
   is open to you works.
2. Stores it in `~/.claude/projects/<your folder>/<session id>.jsonl`, readable by you alone. It
   replaces an older copy when the new transcript extends it (the session grew since your last
   fork). Claude Code needs exactly one copy of a session, so any other copy of it, in this
   folder or another one (sheaves of one project can share one session), is kept aside once the
   new one is written, as `<session id>.jsonl.kept-<UTC time>` in its own folder, and named in one
   line. No other copy is deleted, and a fork you made earlier keeps working: it holds its own
   copy of the conversation. Claude Code no longer reads a kept file; delete it yourself when you
   no longer want it.
3. Writes on the sheaf that you forked it, from which machine, with the new session's id.
4. Starts `claude --resume <id> --fork-session --session-id <new id>` in the folder you gave with
   `--cwd` (by default the folder you are in). That is a new session: your messages go to your
   copy, never back to the hub's. On macOS and Linux `tiroir work` becomes Claude Code; on Windows
   it starts Claude Code and waits until you leave it.

What it does not do: it never runs anything on the hub's machine, never changes the session it
copied, and never sends your messages anywhere but your own Claude Code. `--dry-run` downloads and
stores the transcript, records nothing on the sheaf, and prints the `claude` command instead of
starting it.

The transcript holds everything the session saw, including tool results: keep it as private as
the sheaf itself.

## Your sign-in token

`tiroir work` signs in to the hub with the session token of your account on the front the hub
sits behind. It reads it from the file `~/.config/tiroir/front-token`, or from the variable
`TIROIR_FRONT_TOKEN` when that is set. It sends it only to the sheaf's own host, and never prints
it.

To get it, sign in to the front in your browser, open the developer tools, and copy the value of
the cookie `token` for the front's address (Chrome: Application > Cookies). Then, on a Mac:

```bash
mkdir -p ~/.config/tiroir
pbpaste > ~/.config/tiroir/front-token     # with the token on the clipboard
chmod 600 ~/.config/tiroir/front-token
```

On Linux, use `xclip -selection clipboard -o` instead of `pbpaste`. The file must be readable by
you alone (`chmod 600`); `tiroir work` refuses it otherwise.

On Windows, in PowerShell, with the token on the clipboard:

```powershell
New-Item -ItemType Directory -Force "$HOME\.config\tiroir" | Out-Null
Get-Clipboard | Set-Content -NoNewline -Encoding ascii "$HOME\.config\tiroir\front-token"
```

The file then sits in your own user folder, which other users of the computer cannot read; there
is no `chmod` to run.

The token expires with your session on the front: when `tiroir work` says you are not signed in,
copy a fresh one the same way.

## When something is refused

`tiroir work` says why in one sentence and exits with code 2. The reasons:
- you are not signed in;
- the sheaf is not open to you, or has no session on the hub;
- the address is not a sheaf's;
- the token file is readable by others (macOS and Linux);
- a link sits under the session's file name.

When the hub refuses to record the fork, `tiroir work` gives the hub's reason. The transcript is
then already on your laptop, and Claude Code is not started: run the same command again once the
reason is fixed.

## Development

```bash
uv run --extra dev pytest
uv run --extra dev ruff check . && uv run --extra dev ruff format --check .
```

The tests run the command against a fake hub on 127.0.0.1 and never touch your real
`~/.claude`.

## License

MIT, see `LICENSE`.
