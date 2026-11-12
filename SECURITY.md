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
- `name=value` or `name: value` pairs whose name is, or has a segment that is, password, secret, token, api key, auth, credential or private key (`api_token`, `AUTH_TOKEN`, `x-auth-token`); words that merely contain one (`author`, `tokenizer`, `authentication flow:`) are left alone
- long (32 or more characters) runs of letters and digits that mix both, except a run of pure lowercase hex of exactly 40 characters (a git commit id) or 32 (a dashless uuid / md5), which are kept so notes stay readable. A mixed-case, base64-like or longer run is masked.

A match is replaced in place by `[REDACTED]`; the text before and after it is kept.

It does **not** catch:

- secrets written as natural language ("the password is hunter2")
- short secrets and PINs, and secrets without a recognisable name or shape
- personal data that is not a secret: names, emails, internal hostnames, ticket text. Those are stored as typed.
- anything outside the state directory: your shell history, Claude Code transcripts, `gh` output you paste into a prompt

A 40-hex git sha is kept wherever it appears, so PR heads can be compared and quotes stay readable (a secret that happens to be exactly 40 lowercase hex characters is the accepted trade-off). `hubkit doctor` reports a count of secret-shaped strings found in state, never the strings. Treat the state directory as sensitive and do not put it in a git repository (hubkit refuses to).
