# claude-code-unpoison

One command to recover a Claude Code session killed by the Opus 4.7/4.8 bug:

> **The model's tool call could not be parsed (retry also failed).**

No dependencies. Pure Python 3 stdlib. It rewinds the session transcript past the
failed turns and lets you `--resume` cleanly — keeping all your prior context.

> ⚠️ This is a **stopgap**, not a fix. The bug is server-side and unfixed as of
> 2026-06. The durable workaround is to run tool-heavy work on a clean model
> (`/model claude-opus-4-6[1m]` or Sonnet 4.6). See [Limitations](#limitations).

## The bug

When extended/adaptive thinking is active, Opus 4.7 and 4.8 intermittently serialize a
tool call as **malformed legacy XML** (a bare `<invoke>` missing the namespace prefix, or
a stray token before it) instead of a structured `tool_use` block. The harness can't parse
it, injects a retry, and when the retry also fails the turn dies with the error above.

In the `.jsonl` transcript the failed turns look like this and pile up at the tail:

- an assistant turn with `stop_reason: "tool_use"` but **no `tool_use` block** (only an
  empty thinking block),
- a synthetic `"Your tool call was malformed and could not be parsed. Please retry."`,
- finally a synthetic `"...retry also failed."`

`--resume` replays that poisoned tail, so the session keeps failing. Once one malformed
call is in history, later calls tend to reproduce it (few-shot self-poisoning).

Reported widely on the Claude Code tracker — e.g.
[#62123](https://github.com/anthropics/claude-code/issues/62123) (canonical),
[#63583](https://github.com/anthropics/claude-code/issues/63583) (same signature),
[#64235](https://github.com/anthropics/claude-code/issues/64235) (regression dated 2026-05-29),
[#63481](https://github.com/anthropics/claude-code/issues/63481) (Opus 4.8 + extended thinking).

Reported severity by model generation: Opus 4.6 clean · 4.7 intermittent · **4.8 worst** ·
Sonnet 4.6 clean.

## What it does

Walks the transcript backwards from the end, drops the contiguous **bad tail** (the
empty-thinking `tool_use` turns, retry hints, synthetic errors, and trailing local-only
lines), and rewinds the leaf to the last **clean node** — a user message, a `tool_result`,
or a normally-ended assistant turn. It backs the file up first, then truncates.

Everything before the failure is preserved **verbatim** — strictly more context than
`/compact`, which summarizes the history away.

## Install

```bash
git clone https://github.com/GGGODLIN/claude-code-unpoison.git
cd claude-code-unpoison
chmod +x cc-unpoison.py

# optional alias — drop into ~/.zshrc or ~/.bashrc
alias ccfix='python3 /path/to/claude-code-unpoison/cc-unpoison.py'
```

## Usage

```bash
ccfix              # detect + back up + truncate the current cwd's newest session, print resume cmd
ccfix -r           # ...then chdir to the session's cwd and run `claude --resume` automatically
ccfix -n           # dry run: show what would be dropped, write nothing
ccfix <session-id> # operate on a specific session from any directory (globs ~/.claude/projects)
ccfix <path.jsonl> # operate on an explicit transcript file
```

Typical loop: you hit the error → exit Claude Code → `ccfix -r` → you're back in a clean
resumed session.

### How it picks the session

- **A session-id or path** → used directly. The id is looked up across all of
  `~/.claude/projects/**`, so it works from **any** working directory.
- **No argument** → the most recently modified `.jsonl` in the project dir for your current
  `cwd`. Since you just exited the broken session, that's the freshest one.

With `-r` it reads the session's original `cwd` from the transcript and `chdir`s there
before resuming, so the resumed session lands in the right project context regardless of
where you invoked the command.

## Safety

- **Always backs up** to `<session>.jsonl.bak.<timestamp>` before writing; the restore
  command is printed.
- **Refuses to touch healthy sessions** — if the tail shows no parse poisoning it does
  nothing.
- **Avoids dangling tool calls** — never leaves the leaf on an assistant turn whose
  `tool_use` would lose its `tool_result`.
- **`-n` dry-run** previews the exact cut without writing.

## Limitations

- Handles **tail** cascades (the common case). If the poison is buried mid-conversation
  with good content after it, splicing + re-linking the `parentUuid` chain is required —
  use `/compact` instead. The tool detects "no clean leaf" and tells you so.
- Resume reconstruction is **Claude Code version-specific**. The truncated output is
  validated as well-formed JSON, but verify your first run with `ccfix` (no `-r`) and a
  manual `claude --resume` before trusting `-r`.
- **The real fix is upstream.** This only recovers a stuck session. If you keep working on
  `opus-4-8[1m]` at high effort, the same session can re-poison. Switch to
  `claude-opus-4-6[1m]` or Sonnet 4.6 for tool-heavy / long sessions.

## License

MIT — see [LICENSE](LICENSE).
