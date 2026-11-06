# Contributing

Thanks for looking. hubkit is small on purpose: standard library only, Python 3.14. Linux is tested; macOS and Windows code exists but is unproven (see the README).

## Set up and run the checks

```bash
git clone https://github.com/ankitkpandey1/hubkit && cd hubkit
pip install pytest ruff mypy coverage
python3 -m pytest tests -q                       # about 4 minutes: real subprocesses
ruff check .
mypy scripts/hubcore scripts/learn.py scripts/hubkit
python3 tests/run_reverts.py                     # slow; run it when you touch a guarded behaviour
```

Tests and experiments use scratch state: set `HUBKIT_STATE` and `HUBKIT_CONFIG` to a temporary directory, or use the `env` fixture. Never point them at a live state directory or a live bind.

## Rules

- Every behaviour gets a test, and a guard gets a `tests/run_reverts.py` entry `(name, file, old, new, test)`: `old` occurs exactly once in `file`, `new` breaks the behaviour, and `test` fails with it.
- A test must be able to fail. Do not mock hubkit's own code. Only `gh` may be a local stand-in, because tests cannot reach GitHub; say so in the test. Use real 40-hex shas for heads.
- Hooks always exit 0 and print nothing outside the bound hub.
- A new config key needs a default in `scripts/hubcore/config.py` (`DEFAULTS`), an entry in `config.example.toml` and a row in `docs/configuration.md`.
- Type annotations on functions, a docstring on public ones, standard library only. `except A, B:` without parentheses (PEP 758) is the required form.
- Keep `SKILL.md`, `README.md` and `docs/` true: change them in the same edit as the behaviour, and check each command and path against the code.
- No personal names, hosts, companies or product terms in any file. Personal rules and names belong in the user's own `~/.config/hubkit/`. Tests use neutral names such as `peer-a`.

## Pull requests

Keep a change to one concern. Say what you ran. Add a line to `CHANGELOG.md` under Unreleased. By contributing you agree that your contribution is licensed under the Apache License 2.0, the same as the project.
