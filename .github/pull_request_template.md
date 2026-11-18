## What and why

<!-- What does this change and which problem does it solve? Link the issue if there is one. -->

## Checklist

- [ ] `ruff check .`, `ruff format --check .`, `mypy` and `pytest` pass
- [ ] New behaviour has tests (the fake proxy in `tests/conftest.py`, no network)
- [ ] No password can reach the output unless `--show-credentials` is given
- [ ] `CHANGELOG.md` updated under "Unreleased"
