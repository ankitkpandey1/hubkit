# Security

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private vulnerability reporting (Security tab, "Report a vulnerability") on this repository. Include what you did, what you expected and what happened, and the hubkit commit. Reports are read on a best-effort basis; there is no service-level promise.

## What hubkit stores

State lives under `$XDG_STATE_HOME/hubkit` (default `~/.local/state/hubkit`): orders, work items, alerts, learnings and a status file, all as plain files with your user's permissions. Quotes from your prompts and messages are stored in them. hubkit never sends data anywhere; it runs `gh` and an optional notification command on your machine.

## What redaction does and does not catch

Every write to state passes through `scripts/hubcore/redact.py`. It masks these shapes:

- AWS access key ids, GitHub tokens (`ghp_`, `gho_`, ...), Slack tokens, Google API keys, GitLab tokens, `sk-` and `sk-ant-` keys
- JSON web tokens, PEM private key headers
- credentials in URLs (`scheme://user:password@host`)
- `Bearer ...` and `Authorization:` values
- `name=value` or `name: value` pairs whose name contains password, secret, token, api key, auth, credential or private key
- long (32 or more characters) runs of letters and digits that mix both

It does **not** catch:

- secrets written as natural language ("the password is hunter2")
- short secrets and PINs, and secrets without a recognisable name or shape
- personal data that is not a secret: names, emails, internal hostnames, ticket text. Those are stored as typed.
- anything outside the state directory: your shell history, Claude Code transcripts, `gh` output you paste into a prompt

A 40-hex git sha is kept unredacted only under head and commit fields, so PR heads can be compared. `hubkit doctor` reports a count of secret-shaped strings found in state, never the strings. Treat the state directory as sensitive and do not put it in a git repository (hubkit refuses to).
