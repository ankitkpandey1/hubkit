---
name: hubkit-setup
description: Set up hubkit for the user end to end: check the install, write the config from facts found on the machine, register the hooks in Claude Code settings, bind the hub session and prove it works. Use right after `hubkit skills --install`, or for "set up hubkit", "install the hub", "hubkit doctor shows problems". Ask the user only for what cannot be found out.
---

# hubkit-setup

Goal: a working hub in one pass. Find out; do not ask. Ask once, batched, and only for what no command or file can tell you. Never print tokens or secrets. Never edit a file without a dated backup beside it.

`hubkit` below is the command on the PATH (from `uv tool install hubkit-cli`), or `python3 <skills dir>/hub/scripts/hubkit` when the tool is not on the PATH.

## 1. Check the install

1. `hubkit --help` works and `python3 --version` (or the interpreter hubkit runs under) is 3.14 or newer. If not, say so and stop: hubkit needs 3.14.
2. The hub skill is in the skills directory: `<skills dir>/hub/SKILL.md` exists. If not, run `hubkit skills --install <skills dir>` (Claude Code user level is `~/.claude/skills`).
3. `hubkit doctor` runs. Note what it says; it is the baseline.

## 2. Find the facts (do not ask for these)

| Fact | How to find it |
|---|---|
| The user's name | `git config user.name`, else the OS account's full name |
| Time zone | `/etc/timezone`, `timedatectl show -p Timezone`, or `python3 -c "import tzlocal"` is not needed: `readlink /etc/localtime` (macOS and Linux); on Windows `tzutil /g` |
| GitHub login | `gh api user -q .login` when `gh` is logged in, else leave it empty |
| Notification command | Linux `notify-send`, macOS `osascript`: leave `notify_cmd` empty to use the platform default; on Windows set `tools.notify_cmd` to a command the user already has, else leave it empty |
| Sessions directory | `~/.claude/sessions` exists and holds `<pid>.json` files |
| `mep` | `command -v mep`; set `[mep] enabled = false` when absent |

## 3. Write the config

Copy `<skills dir>/hub/config.example.toml` to `~/.config/hubkit/config.toml` (the directory `$XDG_CONFIG_HOME/hubkit` when set) unless one exists; if one exists, edit only missing keys and say what you changed. Fill the facts from section 2. Add the user's real names, handles and project words to `[private] terms` only if the user wants a clean check; `hubkit doctor --check-clean` lists files that contain them.

Copy `intents.example.toml` to `intents.toml` only when the user wants to change order words or clocks; the example is used by default.

## 4. Register the hooks

1. `hubkit hooks --skills-dir <skills dir>` prints the `hooks` block for `~/.claude/settings.json` (SessionStart, UserPromptSubmit, PostToolUse for questions and routed work, Stop). Add `--merge-guard` for the warn-only PreToolUse merge check.
2. Back up `~/.claude/settings.json` to `settings.json.bak-<date>`, merge the block in (keep every existing hook; a hubkit entry already there is replaced, not duplicated), and write valid JSON.
3. Tell the user that hooks load at the next session start.

## 5. Bind and prove it

1. In the session that will be the hub: `hubkit bind`. It binds only an interactive session.
2. `hubkit doctor` must show `bind: live`, `hook-errors.jsonl: 0 rows`, and no `secret-shaped strings`.
3. Open a throwaway order and close it to prove the loop: `hubkit order open --kind merge --quote "test order"`, then `hubkit order ls`, then `hubkit order cancel <id> --quote "setup test"`.
4. Optional watchdog: on Linux or macOS add the crontab line from the README (name the Python 3.14 interpreter); on Windows create a Task Scheduler task that runs `hubkit tick --if-bound` every 5 minutes. `hubkit doctor` then reports `cron: ok` after the first run.

## 6. Report

At most 6 lines: what you found, what you wrote (paths), what the user must do (restart the session; nothing else), and the one command to check later: `hubkit doctor`.
