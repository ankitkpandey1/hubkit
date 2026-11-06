# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Install like miseenplace: `uv tool install hubkit-cli` (wheel built and installed in a scratch venv; not published), `hubkit skills --install DIR` (hub, hub-mep, hubkit-setup), `hubkit hooks [--skills-dir DIR] [--merge-guard]` prints the settings.json hooks block, and a `hubkit-setup` skill.
- macOS and Windows support through one platform module (`hubcore/compat.py`) and a CI matrix; only Linux has been run so far.
- Blockers (`order block|result`), watcher fixes, peer-report closing, `asks ls`, and warn-only merge guard.
- Apache-2.0 license, `README.md` rewrite, `docs/` (architecture, configuration, commands, troubleshooting), `CONTRIBUTING.md`, `SECURITY.md`.
- GitHub Actions workflow running ruff, mypy and the test suite.
- `pyproject.toml` with package metadata and ruff, mypy and pytest settings.
- Tests for `learn.py promote-candidates`, `review` and the `address` error path, the entry-point import-failure path, and `doctor --check-clean`.

### Fixed
- Stop gate and review close: on a PR written by `user.github_login`, a COMMENT review at the head counts (verdict check, item close, queue advance). Other authors' PRs keep the non-author rule.
- A brief is a review ask only when its first action on the PR is a review; "fix the findings in his review of #N" is not. A head moved by an owner asked to fix the PR raises no re-check alert; a review item still does.
- PR numbers in examples, quotes and code spans of a brief are not tracked. Acknowledgements that name a PR are not tracked.
- "waiting on the verdict" and similar awaiting wording no longer trips the verdict check.
- `order open` never fires from a question, urgent word or not; the prompt hook says so and shows `hubkit order open` for doing it by hand.
- `order close` requires evidence for every PR named in the order and names the missing ones.
- A completed agent's task notification closes its routed item. Work ids are never reused after archive (`work.seq`).
- `doctor` flags a stale or missing `seen/cron` when the crontab runs hubkit (`watchdog.cron_stale_min`).

### Changed
- `mep: UNVERIFIED` now says that the optional `mep` CLI was not found and how to turn the check off (`[mep] enabled = false`).
- `doctor --check-clean` no longer scans `.git/` and also scans `.yml` and `.yaml` files.
- Docstrings and type annotations on public functions; `SKILL.md` and `skills/` reworded for clarity.
- The learnings file preamble says promoted rules go to the overlay.

### Removed
- The unused `tools.mep_cursor` config key.

## [0.1.0] - 2026-11-04

Initial private version: order clock, routed-work tracking, watchdog, stop gate, status block, end-of-day review and the learn loop.
