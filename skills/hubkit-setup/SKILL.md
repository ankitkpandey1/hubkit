---
name: hubkit-setup
description: Set up hubkit for the user end to end: check the install, write the config from facts found on the machine, register the hooks in Claude Code settings, bind the hub session and prove it works. Use right after `hubkit skills --install`, or for "set up hubkit", "install the hub", "hubkit doctor shows problems". Ask the user only for what cannot be found out.
---

# hubkit-setup

Goal: a working hub in one pass, unattended. Each step is one command, then the output that means success, then what means stop. Find out; do not ask. Ask the user once, batched, and only for what no command or file can tell you. Never print tokens or secrets. Never edit a file without a dated backup beside it.

`hubkit` below is the command on the PATH (from `uv tool install hubkit-cli`), or `python3 <skills dir>/hub/scripts/hubkit` when the tool is not on the PATH.

## Exit codes you will meet

`hubkit` exits 0 ok, 2 bad usage, 3 refused, 4 not found; every error ends with `NEXT: <command>`. `hubkit doctor` exits 1 when it finds a secret-shaped string in state or (with `--check-clean`) a private term. Anything not listed as success below: stop, show the user the line, run its `NEXT:`.

## 1. Check the install

| Step | Command | Success | Stop when |
|---|---|---|---|
| 1 | `python3 --version` (the interpreter hubkit runs under) | `Python 3.14` or newer | older: say hubkit needs 3.14 and stop |
| 2 | `hubkit --help` | usage text, exit 0 | command not found: use `python3 <skills dir>/hub/scripts/hubkit` |
| 3 | `test -f <skills dir>/hub/SKILL.md` | exit 0 | missing: `hubkit skills --install <skills dir>` prints `hub: installed in ...`, `hub-mep: installed ...`, `hubkit-setup: installed ...` (Claude Code user level is `~/.claude/skills`) |
| 4 | `hubkit doctor` | exit 0; on a fresh install `config: ... (defaults)` and `bind: none or dead` | exit 1: a secret-shaped string is in state; stop and tell the user. Keep the output as the baseline |

## 2. Find the facts (do not ask for these)

| Fact | How to find it |
|---|---|
| The user's name | `git config user.name`, else the OS account's full name |
| Time zone | `readlink /etc/localtime` or `timedatectl show -p Timezone` (macOS and Linux); on Windows `tzutil /g` |
| GitHub login | `gh api user -q .login` when `gh` is logged in, else leave it empty |
| Notification command | Linux `notify-send`, macOS `osascript`: leave `notify_cmd` empty to use the platform default; on Windows set `tools.notify_cmd` to a command the user already has, else leave it empty |
| Chat-send command | the command the user's agents run to send a chat message; set `tools.send_regex` to a regex that matches it (empty means `mep send`) |
| Sessions directory | `~/.claude/sessions` exists and holds `<pid>.json` files |
| `mep` | `command -v mep`; set `[mep] enabled = false` when absent |

Ask the user only for what stays empty after this table and is needed (for example the time zone when none can be read).

## 3. Write the config

1. `test -f ~/.config/hubkit/config.toml` (or `$XDG_CONFIG_HOME/hubkit/config.toml`). Missing: `mkdir -p ~/.config/hubkit && cp <skills dir>/hub/config.example.toml ~/.config/hubkit/config.toml`. Present: edit only missing keys and say what you changed.
2. Fill the facts from section 2. Back up an existing file first: `cp config.toml config.toml.bak-<date>`.
3. Success: `hubkit doctor` prints `config: <path> (found)`. Stop when it still says `(defaults)`: the file is not where hubkit looks.
4. Add the user's real names, handles and project words to `[private] terms` only if the user wants a clean check; `hubkit doctor --check-clean` then prints `private terms in skill files ...: none` and exits 0, or exits 1 listing file names.
5. Copy `intents.example.toml` to `~/.config/hubkit/intents.toml` only when the user wants to change order words or clocks; the example is used by default.

## 4. Register the hooks

1. `hubkit hooks --skills-dir <skills dir>` prints a JSON document with a `hooks` key (SessionStart, UserPromptSubmit, PostToolUse for questions and routed work, Stop); exit 0. Add `--merge-guard` for the warn-only PreToolUse guard.
2. Back up `~/.claude/settings.json` to `settings.json.bak-<date>`, merge the block in (keep every existing hook; a hubkit entry already there is replaced, not duplicated) and write valid JSON.
3. Success: `python3 -c "import json,sys; json.load(open(sys.argv[1]))" ~/.claude/settings.json` exits 0, and `hubkit doctor` prints no `WARNING settings.json runs hubkit hook more than once`.
4. Tell the user that hooks load at the next session start.

## 5. Bind and prove it

1. In the session that will be the hub: `hubkit bind`. Success: `bound <name> (pid <N>)`, exit 0. Exit 2 with `no Claude session found` or `only interactive sessions` means this is not the hub session: stop. Exit 2 with `live hub already bound` means another hub is live: tell the user.
2. `hubkit doctor`. Success, exit 0 with `bind: live <name>`, `hook-errors.jsonl: 0 rows` and `secret-shaped strings in state (count only): 0`. Anything else: stop.
3. Prove the loop with a throwaway order: `hubkit order open --kind merge --quote "test order"` prints `<id> merge [open] ...`; `hubkit order ls` lists it; `hubkit order cancel <id> --quote "setup test"` exits 0. Exit 4 means the id was wrong.
4. `hubkit next` prints a line ending `NEXT: ...` (after the cancel: `nothing is due`, `NEXT: hubkit mine`), exit 0.
5. Optional watchdog: on Linux or macOS add the crontab line from the README (name the Python 3.14 interpreter); on Windows create a Task Scheduler task that runs `hubkit tick --if-bound` every 5 minutes. After its first run `hubkit doctor` prints `cron: ok`; `cron: STALE` means the line does not run hubkit.

## 6. Report

At most 6 lines: what you found, what you wrote (paths), what the user must do (restart the session; nothing else), and the one command to check later: `hubkit doctor` (`hubkit doctor --json` for a machine-readable result).
